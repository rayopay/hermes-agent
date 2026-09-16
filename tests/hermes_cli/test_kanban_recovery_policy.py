"""Causal native regressions for active budgets, review rework and legacy repair."""
import json
import sqlite3
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_recovery as accounting
from hermes_cli.kanban_db_recovery_release import recover_task
from tests.hermes_cli.test_kanban_recovery_release import board, block, request

# Every case builds settlement evidence using the native Linux /proc boot ID.
pytestmark = pytest.mark.linux_only


def legacy(tmp_path, monkeypatch, status):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / '.hermes'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as c:
        c.executescript((Path(__file__).parent / 'fixtures/recovery' / f'legacy_{status}.sql').read_text())
    c = kbc.connect(path)
    return c, c.execute('SELECT id FROM tasks').fetchone()[0]


def classify(c, t, identity=None):
    v = accounting.get_recovery_state(c, t)
    r = accounting.classify_blocker(c, t, observed_token=v['observed_token'],
        block_event_id=v['active_event_id'], existing_blocker_id=identity,
        rationale='actual distinct cause', evidence_refs=['evidence:classification'])
    assert r['ok'], r
    return r['state']


@pytest.mark.parametrize('action', ['retry', 'resolved_resume'])
def test_active_budget_not_retained_other_budget(tmp_path, monkeypatch, action):
    c, t = legacy(tmp_path, monkeypatch, 'high_count')
    try:
        a = accounting.get_recovery_state(c, t)['active_blocker_id']
        state = classify(c, t)
        assert state['blockers'][a]['count'] == 2
        old_a = state['blockers'][a].copy()
        result = recover_task(c, t, **request(c, t, action))
        assert result['released'], result
        assert result['state']['blockers'][a] == old_a
        assert not accounting.recovery_hold(c, t)
        from plugins.kanban.dashboard.plugin_api import _set_status_direct
        assert _set_status_direct(c, t, 'todo')
        assert kb.recompute_ready(c) == 1
        assert _set_status_direct(c, t, 'ready')
        # Actual claim, not merely a successful release response.
        block(c, t)
        classify(c, t, a)
        before = list(c.iterdump())
        assert not _set_status_direct(c, t, 'ready')
        assert not kb.recompute_ready(c)
        assert not kb.claim_task(c, t)
        assert not recover_task(c, t, **request(c, t, 'retry'))['ok']
        assert list(c.iterdump()) == before
        assert recover_task(c, t, **request(c, t, ref='evidence:A-repaired'))['released']
        assert kb.claim_task(c, t)
    finally:
        c.close()


@pytest.mark.parametrize('action', ['retry', 'resolved_resume'])
@pytest.mark.parametrize('completed_before', [False, True])
@pytest.mark.parametrize('parents', [False, True])
@pytest.mark.parametrize('omitted_reviewer,review_assignment', [(False, None), (True, None), (True, 'alternate'), (False, 'alternate')])
def test_authorized_review_rework(board, action, completed_before, parents, omitted_reviewer, review_assignment):
    from tests.hermes_cli.test_kanban_recovery_completion import reopen
    c = board
    t = kb.create_task(c, title='implementation', assignee='worker')
    other = kb.create_task(c, title='unrelated', assignee='worker')
    untouched = kb.get_task(c, other)
    if completed_before:
        claim = kb.claim_task(c, t)
        assert kb.complete_task(c, t, expected_run_id=claim.current_run_id)
        reopen(c, t)
    claim = kb.claim_task(c, t)
    kwargs = {} if omitted_reviewer else {'reviewer': 'reviewer'}
    assert kb.request_review(c, t, summary='review delta', expected_run_id=claim.current_run_id, **kwargs)
    submitted = c.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested'", (t,)).fetchone()
    assert json.loads(submitted['payload'])['reviewer'] == (None if omitted_reviewer else 'reviewer')
    assert kb.get_task(c, t).assignee == ('worker' if omitted_reviewer else 'reviewer')
    if review_assignment:
        assert kb.assign_task(c, t, review_assignment)
    review = kb.claim_review_task(c, t)
    assert review
    assert review.assignee == (review_assignment or ('worker' if omitted_reviewer else 'reviewer'))
    assert kb.request_changes(c, t, reason='revise implementation', expected_run_id=review.current_run_id)[0]
    assert kb.get_task(c, t).assignee == 'worker'
    block(c, t)
    parent = kb.create_task(c, title='parent', assignee='worker') if parents else None
    if parent:
        kb.link_tasks(c, parent, t)
    runs = kb.list_runs(c, t)
    failures = kb.get_task(c, t).consecutive_failures
    events = kb.list_events(c, t)
    result = recover_task(c, t, **request(c, t, action))
    assert result['released'], result
    assert result['status'] == ('todo' if parent else 'ready')
    assert kb.list_runs(c, t) == runs
    assert kb.list_events(c, t)[:len(events)] == events
    assert kb.get_task(c, t).consecutive_failures == failures
    assert kb.get_task(c, other) == untouched
    if parent:
        assert kb.complete_task(c, parent)
        assert kb.get_task(c, t).status == 'ready'
    continued = kb.claim_task(c, t)
    assert continued and continued.assignee == 'worker'
    assert continued.consecutive_failures == failures
    assert [r for r in kb.list_runs(c, t) if r.id != continued.current_run_id] == runs


@pytest.mark.parametrize('status', ['blocked', 'triage'])
@pytest.mark.parametrize('action', ['retry', 'resolved_resume'])
def test_same_cause_legacy_recovery(tmp_path, monkeypatch, status, action):
    c, t = legacy(tmp_path, monkeypatch, status)
    try:
        before = list(c.iterdump())
        v = accounting.get_recovery_state(c, t)
        assert list(c.iterdump()) == before
        identity = v['active_blocker_id']
        count = v['blockers'][identity]['count']
        events = kb.list_events(c, t)
        runs = kb.list_runs(c, t)
        result = recover_task(c, t, **request(c, t, action))
        if action == 'retry' and count >= kb.BLOCK_RECURRENCE_LIMIT:
            assert not result['released']
            assert list(c.iterdump()) == before
            return
        assert result['released'], result
        assert kb.list_runs(c, t) == runs
        assert kb.list_events(c, t)[:len(events)] == events
        current = result['state']
        assert current['active_blocker_id'] == identity
        assert current['corrections'] == []
        assert current['blockers'][identity]['count'] == (count if action == 'retry' else 0)
        assert current['blockers'][identity]['cycle'] == (1 if action == 'retry' else 2)
        if action == 'resolved_resume':
            assert current['resolutions'][0]['count'] == count
        assert any(e.kind == 'blocker_legacy_enrolled' for e in kb.list_events(c, t))
        if action == 'resolved_resume':
            block(c, t)
            repeated = accounting.get_recovery_state(c, t)
            assert repeated['active_blocker_id'] == identity
            assert repeated['blockers'][identity]['cycle'] == 2
            assert repeated['blockers'][identity]['count'] == 1
            assert recover_task(c, t, **request(c, t, ref='evidence:second-cycle'))['released']
        claim = kb.claim_task(c, t)
        assert claim and kb.complete_task(c, t, expected_run_id=claim.current_run_id)
        closed = accounting.get_recovery_state(c, t)
        kb.gc_events(c, older_than_seconds=-1)
        assert accounting.get_recovery_state(c, t)['closed_cycles'] == closed['closed_cycles']
        assert any(e.kind == 'blocker_legacy_enrolled' for e in kb.list_events(c, t))
    finally:
        c.close()


@pytest.mark.parametrize('fault', ['stale', 'settlement', 'worker', 'delegate', 'rollback', 'lineage'])
def test_legacy_denials_do_not_enroll(tmp_path, monkeypatch, fault):
    from agent.delegation_context import delegated_child_context
    c, t = legacy(tmp_path, monkeypatch, 'blocked')
    try:
        args = request(c, t)
        if fault == 'stale':
            args['observed_token'] = 'stale'
        if fault == 'settlement':
            args['settlement_refs'] = []
        if fault == 'rollback':
            c.execute("CREATE TRIGGER refuse BEFORE UPDATE OF status ON tasks BEGIN SELECT RAISE(ABORT, 'rollback'); END")
        if fault == 'lineage':
            c.execute("UPDATE tasks SET block_kind='ambiguous' WHERE id=?", (t,))
        before = list(c.iterdump())
        if fault == 'worker':
            monkeypatch.setenv('HERMES_KANBAN_TASK', t)
            with pytest.raises(PermissionError):
                recover_task(c, t, **args)
        elif fault == 'delegate':
            with delegated_child_context('child'):
                with pytest.raises(PermissionError):
                    recover_task(c, t, **args)
        elif fault in ('rollback', 'lineage'):
            with pytest.raises(sqlite3.IntegrityError if fault == 'rollback' else ValueError):
                recover_task(c, t, **args)
        else:
            assert not recover_task(c, t, **args)['ok']
        assert list(c.iterdump()) == before
    finally:
        c.close()


@pytest.mark.parametrize('fault', ['outcome', 'review_claim', 'implementer', 'event_run', 'late_event'])
def test_rework_requires_native_provenance(board, fault):
    c = board
    t = kb.create_task(c, title='rework', assignee='worker')
    claim = kb.claim_task(c, t)
    assert kb.request_review(c, t, reviewer='reviewer', summary='delta', expected_run_id=claim.current_run_id)
    reviewer = kb.claim_review_task(c, t)
    assert kb.request_changes(c, t, reason='revise', expected_run_id=reviewer.current_run_id)[0]
    block(c, t)
    change = c.execute("SELECT * FROM task_events WHERE task_id=? AND kind='changes_requested'", (t,)).fetchone()
    if fault == 'outcome':
        c.execute("UPDATE task_runs SET outcome='blocked' WHERE id=?", (reviewer.current_run_id,))
    elif fault == 'review_claim':
        c.execute("UPDATE task_events SET payload='{}' WHERE run_id=? AND kind='claimed'", (reviewer.current_run_id,))
    elif fault == 'implementer':
        data = json.loads(change['payload']); data['implementer'] = 'other'
        c.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), change['id']))
    elif fault == 'event_run':
        c.execute('UPDATE task_events SET run_id=? WHERE id=?', (claim.current_run_id, change['id']))
    else:
        c.execute('DELETE FROM task_events WHERE id=?', (change['id'],))
        kb._append_event(c, t, 'changes_requested', json.loads(change['payload']), run_id=reviewer.current_run_id)
    before = list(c.iterdump())
    result = recover_task(c, t, **request(c, t))
    assert not result['released'], result
    assert list(c.iterdump()) == before


@pytest.mark.parametrize('omitted_reviewer', [False, True])
@pytest.mark.parametrize('fault', ['explicit_contradiction', 'missing_reviewer', 'malformed_reviewer',
    'claim_profile', 'changes_reviewer', 'missing_assignment', 'malformed_assignment',
    'superseded_missing_reviewer', 'superseded_malformed_reviewer', 'superseded_blank_reviewer'])
def test_reviewer_provenance_cannot_be_skipped(board, omitted_reviewer, fault):
    c = board
    t = kb.create_task(c, title='reviewer provenance', assignee='worker')
    claim = kb.claim_task(c, t)
    kwargs = {} if omitted_reviewer else {'reviewer': 'reviewer'}
    assert kb.request_review(c, t, summary='delta', expected_run_id=claim.current_run_id, **kwargs)
    if fault in ('missing_assignment', 'malformed_assignment') or fault.startswith('superseded_'):
        assert kb.assign_task(c, t, 'alternate')
    review = kb.claim_review_task(c, t)
    assert review
    assert kb.request_changes(c, t, reason='revise', expected_run_id=review.current_run_id)[0]
    block(c, t)
    # Prove the native setup is authorized before corrupting its evidence.
    from hermes_cli.kanban_db_recovery_provenance import authorized_rework
    task = dict(c.execute('SELECT * FROM tasks WHERE id=?', (t,)).fetchone())
    runs = [dict(r) for r in c.execute('SELECT * FROM task_runs WHERE task_id=?', (t,))]
    block_event_id = accounting.get_recovery_state(c, t)['active_event_id']
    assert authorized_rework(c, task, runs, 0, block_event_id)
    if fault == 'claim_profile':
        c.execute("UPDATE task_runs SET profile='contradiction' WHERE id=?", (review.current_run_id,))
    else:
        kind = ('assigned' if fault.endswith('assignment') else
                'changes_requested' if fault == 'changes_reviewer' else 'review_requested')
        event = c.execute('SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC', (t, kind)).fetchone()
        data = json.loads(event['payload'])
        if fault == 'missing_assignment':
            c.execute('DELETE FROM task_events WHERE id=?', (event['id'],))
        else:
            if fault in ('missing_reviewer', 'superseded_missing_reviewer'):
                del data['reviewer']
            elif fault == 'malformed_assignment':
                del data['assignee']
            else:
                data['reviewer'] = ([] if fault in ('malformed_reviewer', 'superseded_malformed_reviewer')
                                    else '  ' if fault == 'superseded_blank_reviewer' else 'contradiction')
            c.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), event['id']))
    before = list(c.iterdump())
    result = recover_task(c, t, **request(c, t))
    assert not result['released'], result
    assert list(c.iterdump()) == before


@pytest.mark.parametrize('fault', ['origin', 'seed', 'projection', 'missing_enrollment'])
def test_enrollment_audit_rejects_corruption(tmp_path, monkeypatch, fault):
    c, t = legacy(tmp_path, monkeypatch, 'triage')
    try:
        args = request(c, t)
        assert recover_task(c, t, **args)['released']
        if fault == 'origin':
            c.execute("UPDATE task_events SET payload='{}' WHERE id=?", (args['block_event_id'],))
        elif fault == 'missing_enrollment':
            c.execute("DELETE FROM task_events WHERE kind='blocker_legacy_enrolled'")
        elif fault == 'seed':
            event = c.execute("SELECT * FROM task_events WHERE kind='blocker_legacy_enrolled'").fetchone()
            payload = json.loads(event['payload']); payload['legacy_seed']['lower_bound'] += 1
            c.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event['id']))
        else:
            state = json.loads(c.execute('SELECT state_json FROM task_recovery').fetchone()[0])
            state['blockers']['f' * 32] = {'count': 0, 'cycle': 1, 'legacy_lower_bound': 0}
            c.execute('UPDATE task_recovery SET state_json=?', (json.dumps(state),))
        before = list(c.iterdump())
        with pytest.raises(ValueError):
            accounting.get_recovery_state(c, t)
        with pytest.raises(ValueError):
            kb.claim_task(c, t)
        assert list(c.iterdump()) == before
    finally:
        c.close()
