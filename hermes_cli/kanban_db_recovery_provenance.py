"""Immutable native provenance for recovery enrollment and review rework."""
import hashlib
import json
import uuid
from hermes_cli import kanban_db_recovery as accounting


def legacy_enrollment(conn, task, state, view, actor):
    """Append only after every release refusal check, in its owning transaction."""
    from hermes_cli import kanban_db as kb
    if not conn.in_transaction or not view['legacy'] or view['revision'] != 0:
        raise ValueError('Legacy enrollment requires the recovery transaction')
    original, _ = accounting._legacy_report(conn, task)
    identity = state['active_blocker_id']
    return kb._append_event(conn, task['id'], 'blocker_legacy_enrolled', {
        'block_event_id': original['id'], 'blocker_id': identity,
        'observed_token': view['observed_token'], 'actor': actor,
        'prior_revision': 0, 'revision': 1,
        'legacy_seed': {'payload_sha256': hashlib.sha256(original['payload'].encode()).hexdigest(),
                        'lower_bound': state['blockers'][identity]['legacy_lower_bound']}})


def enrollment_report(conn, task_id, event, payload, after):
    original = conn.execute('SELECT * FROM task_events WHERE task_id=? AND id=?',
                            (task_id, payload['block_event_id'])).fetchone()
    seed = payload['legacy_seed']
    if not original or not after < original['id'] < event or original['kind'] not in ('blocked', 'block_loop_detected'):
        raise ValueError('Legacy enrollment report unavailable')
    native = json.loads(original['payload'])
    identity = uuid.uuid5(uuid.NAMESPACE_URL, accounting._encode([task_id, original['id'], native])).hex
    if (payload['blocker_id'] != identity or 'blocker_id' in native
            or type(seed['lower_bound']) is not int or seed['lower_bound'] < 0
            or native.get('recurrences') != seed['lower_bound'] + 1
            or seed['payload_sha256'] != hashlib.sha256(original['payload'].encode()).hexdigest()
            or payload['prior_revision'] != 0 or payload['revision'] != 1
            or not isinstance(payload['actor'], dict) or not payload['observed_token']):
        raise ValueError('Legacy enrollment disagrees with immutable origin')
    return str(original['id']), (original['kind'], {
        'blocker_id': identity, 'blocker_cycle': 1, 'blocker_seed': seed['lower_bound']})


def authorized_rework(conn, task, runs, completed_run, block_event_id):
    """A native reviewer handback followed by this implementer's claimed delta.

    A comment/status or a bare changes_requested event grants no continuation.
    Event ordering, task/run joins, outcomes and recorded profiles must agree.
    """
    current = [r for r in runs if r['id'] > completed_run]
    if any(r['outcome'] == 'completed' for r in current):
        return False
    submissions = [r for r in current if r['outcome'] == 'review_requested']
    if not submissions:
        return True
    by_id = {r['id']: r for r in current}
    try:
        events = [dict(e) for e in conn.execute(
            'SELECT * FROM task_events WHERE task_id=? AND id<=? ORDER BY id',
            (task['id'], block_event_id))]
        for e in events:
            e['data'] = json.loads(e['payload'] or '{}')
        submission = max(submissions, key=lambda r: r['id'])
        requested = next(e for e in reversed(events) if e['kind'] == 'review_requested' and e['run_id'] == submission['id'])
        changes = next(e for e in reversed(events) if e['kind'] == 'changes_requested' and e['id'] > requested['id'])
        reviewer = by_id[changes['run_id']]
        claimed = next(e for e in events if e['kind'] == 'claimed' and e['run_id'] == reviewer['id'])
        blocked = next(e for e in events if e['id'] == block_event_id)
        implementation = by_id[blocked['run_id']]
        resumed = next(e for e in events if e['kind'] == 'claimed' and e['run_id'] == implementation['id'])
        implementer = requested['data']['implementer']
        requested_reviewer = requested['data']['reviewer']
        if requested_reviewer is not None and not (isinstance(requested_reviewer, str) and requested_reviewer.strip()):
            return False
        # Native first submission records null and retains the implementer;
        # an intervening native assignment may select the actual review worker.
        assigned_reviewer = implementer if requested_reviewer is None else requested_reviewer
        for event in events:
            if event['kind'] == 'assigned' and requested['id'] < event['id'] < claimed['id']:
                assigned_reviewer = event['data']['assignee']
        return (isinstance(implementer, str) and bool(implementer.strip())
            and requested['id'] < claimed['id'] < changes['id'] < resumed['id'] < blocked['id']
            and submission['id'] < reviewer['id'] < implementation['id']
            and submission['status'] == 'review'
            and reviewer['outcome'] == 'changes_requested' and reviewer['status'] in ('ready', 'todo')
            and changes['data']['status'] == reviewer['status']
            and claimed['data'].get('source_status') == 'review'
            and isinstance(assigned_reviewer, str) and bool(assigned_reviewer.strip())
            and assigned_reviewer == changes['data']['reviewer'] == reviewer['profile']
            and changes['data']['implementer'] == task['assignee'] == implementation['profile'] == implementer
            and resumed['data'].get('source_status', 'ready') == 'ready'
            and blocked['kind'] in ('blocked', 'block_loop_detected')
            and blocked['data'].get('source_status') == 'ready'
            and implementation['outcome'] == 'blocked' and implementation['status'] == 'blocked')
    except (KeyError, TypeError, ValueError, StopIteration):
        return False
