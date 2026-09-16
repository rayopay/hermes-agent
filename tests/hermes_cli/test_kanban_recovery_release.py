"""Native target recovery on disposable SQLite boards."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_recovery as recovery
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect_closing(tmp_path / "board.db") as conn:
        yield conn


def block(conn, tid):
    claim = kb.claim_task(conn, tid, claimer="worker")
    assert claim
    assert kb.block_task(conn, tid, reason="same cause", kind="capability",
                         expected_run_id=claim.current_run_id)


def request(conn, tid, action="resolved_resume", ref="evidence:repair-1"):
    view = recovery.get_recovery_state(conn, tid)
    from hermes_cli.kanban_db_recovery_release import settlement_scope
    return dict(observed_token=view["observed_token"], block_event_id=view["active_event_id"],
                blocker_id=view["active_blocker_id"], action=action, rationale="Inspected repair",
                evidence_refs=[ref], initiation_mode="autonomous",
                settlement_refs=[{**settlement_scope(conn, tid), "reference": "evidence:settlement",
                                  "assertion": "settled"}])


@pytest.mark.linux_only
def test_resolved_cycle_releases_only_target_and_retains_history(board):
    from hermes_cli.kanban_db_recovery_release import recover_task
    tid = kb.create_task(board, title="target", assignee="worker")
    other = kb.create_task(board, title="other", assignee="worker")
    block(board, other)
    untouched = kb.get_task(board, other)
    block(board, tid)
    assert kb.unblock_task(board, tid)
    block(board, tid)
    old = recovery.get_recovery_state(board, tid)
    args = request(board, tid)
    result = recover_task(board, tid, **args)
    assert result["released"] and result["eligible"] and result["status"] == "ready"
    assert kb.get_task(board, other) == untouched
    assert not recover_task(board, tid, **args)["ok"]
    block(board, tid)
    new = recovery.get_recovery_state(board, tid)
    identity = old["active_blocker_id"]
    assert new["active_blocker_id"] == identity
    assert new["blockers"][identity]["cycle"] == 2
    assert new["blockers"][identity]["count"] == 1
    assert set(old["reports"]) < set(new["reports"])
    assert new["resolutions"][0]["count"] == 2
    payload = json.loads(board.execute("SELECT payload FROM task_events WHERE id=?", (new["active_event_id"],)).fetchone()[0])
    assert payload["blocker_cycle"] == 2
    before = list(board.iterdump())
    assert not recover_task(board, tid, **request(board, tid))["ok"]
    assert list(board.iterdump()) == before


@pytest.mark.linux_only
def test_retry_budget_and_historical_live_pid(board):
    from hermes_cli.kanban_db_recovery_release import recover_task
    tid = kb.create_task(board, title="target", assignee="worker")
    claim = kb.claim_task(board, tid, claimer="worker")
    # Parent-owned pipe keeps the child alive until the refusal is observed.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
    )
    try:
        assert proc.poll() is None
        dispatch._set_worker_pid(board, tid, proc.pid)
        assert kb.block_task(board, tid, reason="cause", kind="capability", expected_run_id=claim.current_run_id)
        before = list(board.iterdump())
        assert proc.poll() is None
        assert not recover_task(board, tid, **request(board, tid, "retry"))["ok"]
        assert proc.poll() is None
        assert list(board.iterdump()) == before
        proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert recover_task(board, tid, **request(board, tid, "retry"))["released"]
        block(board, tid)
        before = list(board.iterdump())
        assert not recover_task(board, tid, **request(board, tid, "retry"))["ok"]
        assert list(board.iterdump()) == before
    finally:
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()  # Only this fixture's owned child, never a board PID.
            proc.communicate(timeout=10)


@pytest.mark.parametrize("status,operation", [
    ("triage", "specify"), ("blocked", "unblock"), ("ready", "claim"),
    ("review", "review_claim"), ("todo", "recompute"), ("blocked", "promote"),
])
def test_exhausted_hold_survives_status_only_changes(board, status, operation):
    tid = kb.create_task(board, title="held", assignee="worker")
    block(board, tid)
    assert kb.unblock_task(board, tid)
    block(board, tid)
    board.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    before = list(board.iterdump())
    calls = {"specify": lambda: kb.specify_triage_task(board, tid),
             "unblock": lambda: kb.unblock_task(board, tid),
             "claim": lambda: kb.claim_task(board, tid),
             "review_claim": lambda: kb.claim_review_task(board, tid),
             "recompute": lambda: kb.recompute_ready(board),
             "promote": lambda: kb.promote_task(board, tid, actor="test")[0]}
    assert not calls[operation]()
    assert list(board.iterdump()) == before


@pytest.mark.linux_only
@pytest.mark.parametrize("fault", ["stale", "settlement", "worker", "delegate", "rollback"])
def test_recovery_denials_are_atomic(board, monkeypatch, fault):
    from hermes_cli.kanban_db_recovery_release import recover_task
    from agent.delegation_context import delegated_child_context
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    args = request(board, tid)
    if fault == "stale":
        args["observed_token"] = "stale"
    if fault == "settlement":
        args["settlement_refs"][0]["scopes"] = []
    if fault == "rollback":
        board.execute("CREATE TRIGGER deny_release BEFORE UPDATE OF status ON tasks BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
    before = list(board.iterdump())
    if fault == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        with pytest.raises(PermissionError):
            recover_task(board, tid, **args)
    elif fault == "delegate":
        with delegated_child_context("child"):
            with pytest.raises(PermissionError):
                recover_task(board, tid, **args)
    elif fault == "rollback":
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            recover_task(board, tid, **args)
    else:
        assert not recover_task(board, tid, **args)["ok"]
    assert list(board.iterdump()) == before


@pytest.mark.linux_only
def test_other_blocker_count_survives_resolution(board):
    from hermes_cli.kanban_db_recovery_release import recover_task
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    a = recovery.get_recovery_state(board, tid)["active_blocker_id"]
    assert kb.unblock_task(board, tid)
    block(board, tid)
    view = recovery.get_recovery_state(board, tid)
    result = recovery.classify_blocker(board, tid, observed_token=view["observed_token"],
        block_event_id=view["active_event_id"], rationale="distinct B", evidence_refs=["evidence:B"])
    b = result["state"]["active_blocker_id"]
    assert recover_task(board, tid, **request(board, tid, "retry"))["released"]
    block(board, tid)
    view = recovery.get_recovery_state(board, tid)
    assert recovery.classify_blocker(board, tid, observed_token=view["observed_token"],
        block_event_id=view["active_event_id"], existing_blocker_id=a,
        rationale="A recurred", evidence_refs=["evidence:A"])["ok"]
    assert recover_task(board, tid, **request(board, tid))["released"]
    state = recovery.get_recovery_state(board, tid)
    assert state["blockers"][b]["count"] == 1
    assert state["blockers"][a]["cycle"] == 2


@pytest.mark.linux_only
@pytest.mark.parametrize("tracked", [False, True])
def test_completed_execution_reopened_is_recoverable(board, tracked):
    from hermes_cli.kanban_db_recovery_release import recover_task
    from tests.hermes_cli.test_kanban_recovery_completion import reopen
    tid = kb.create_task(board, title="fresh execution", assignee="worker")
    if tracked:
        block(board, tid)
        assert recover_task(board, tid, **request(board, tid))["released"]
    claim = kb.claim_task(board, tid, claimer="worker")
    assert kb.complete_task(board, tid, expected_run_id=claim.current_run_id)
    assert any(r.outcome == "completed" for r in kb.list_runs(board, tid))
    reopen(board, tid)
    block(board, tid)
    result = recover_task(board, tid, **request(board, tid, ref="evidence:fresh"))
    assert result["released"], result
    assert result["status"] == "ready"


@pytest.mark.linux_only
@pytest.mark.parametrize("phase", ["ready", "review"])
def test_parent_gated_resolution_preserves_phase(board, phase):
    from hermes_cli.kanban_db_recovery_release import recover_task
    tid = kb.create_task(board, title="phase", assignee="worker")
    other = kb.create_task(board, title="unrelated", assignee="worker")
    block(board, other)
    untouched = kb.get_task(board, other)
    if phase == "review":
        claim = kb.claim_task(board, tid, claimer="worker")
        assert kb.request_review(board, tid, reviewer="reviewer", summary="accepted execution", expected_run_id=claim.current_run_id)
        claim = kb.claim_review_task(board, tid)
        assert kb.block_task(board, tid, reason="review blocked", kind="capability", expected_run_id=claim.current_run_id)
    else:
        block(board, tid)
    parent = kb.create_task(board, title="parent", assignee="worker")
    kb.link_tasks(board, parent, tid)
    result = recover_task(board, tid, **request(board, tid))
    assert result["released"] and result["status"] == "todo" and not result["eligible"]
    assert kb.get_task(board, other) == untouched
    assert kb.complete_task(board, parent)
    assert kb.get_task(board, tid).status == phase
    assert kb.get_task(board, other) == untouched


@pytest.mark.linux_only
def test_repeated_resolutions_completion_gc(board, monkeypatch):
    from hermes_cli.kanban_db_recovery_release import recover_task
    tid = kb.create_task(board, title="cycles", assignee="worker")
    identity = None
    for index in range(3):
        block(board, tid)
        view = recovery.get_recovery_state(board, tid)
        identity = identity or view["active_blocker_id"]
        assert view["active_blocker_id"] == identity
        assert view["blockers"][identity]["cycle"] == index + 1
        assert recover_task(board, tid, **request(board, tid, ref=f"evidence:cycle-{index}"))["released"]
    prior = recovery.get_recovery_state(board, tid)
    claim = kb.claim_task(board, tid, claimer="worker")
    assert kb.complete_task(board, tid, expected_run_id=claim.current_run_id)
    now = kb.time.time()
    monkeypatch.setattr(kb.time, "time", lambda: now + 100)
    kb.gc_events(board, older_than_seconds=0)
    closed = recovery.get_recovery_state(board, tid)
    assert closed["closed_cycles"][-1]["state"]["resolutions"] == prior["resolutions"]
    assert closed["blockers"] == {}
    from tests.hermes_cli.test_kanban_recovery_completion import reopen
    reopen(board, tid)
    block(board, tid)
    assert recover_task(board, tid, **request(board, tid, ref="evidence:after-gc"))["released"]
    assert recovery.get_recovery_state(board, tid)["closed_cycles"] == closed["closed_cycles"]


@pytest.mark.linux_only
@pytest.mark.parametrize("fault", ["lock_open", "second_connection", "cycle"])
def test_native_release_boundary_refuses_unchanged(board, monkeypatch, fault):
    from contextlib import contextmanager
    from hermes_cli.kanban_db_recovery_release import recover_task
    tid = kb.create_task(board, title="boundary", assignee="worker")
    block(board, tid)
    args = request(board, tid)
    baseline = []
    if fault == "lock_open":
        original = Path.open
        def denied(self, *a, **kw):
            if self.name.endswith(".dispatch.lock"):
                raise PermissionError("test lock refusal")
            return original(self, *a, **kw)
        monkeypatch.setattr(Path, "open", denied)
    elif fault == "cycle":
        state = json.loads(board.execute("SELECT state_json FROM task_recovery WHERE task_id=?", (tid,)).fetchone()[0])
        state["blockers"][state["active_blocker_id"]]["cycle"] += 1
        board.execute("UPDATE task_recovery SET state_json=? WHERE task_id=?", (json.dumps(state), tid))
    else:
        original = kbc._dispatch_tick_lock
        @contextmanager
        def raced(path, **kw):
            with kbc.connect_closing(path) as second:
                assert second is not board
                second.execute("UPDATE tasks SET title='competing mutation' WHERE id=?", (tid,))
            baseline[:] = list(board.iterdump())
            with original(path, **kw) as held:
                yield held
        monkeypatch.setattr(kbc, "_dispatch_tick_lock", raced)
    before = list(board.iterdump())
    if fault == "cycle":
        with pytest.raises(ValueError):
            recover_task(board, tid, **args)
    else:
        assert not recover_task(board, tid, **args)["released"]
    assert list(board.iterdump()) == (baseline or before)


@pytest.mark.linux_only
@pytest.mark.parametrize("completed_before", [False, True])
def test_current_accepted_execution_with_lost_phase_stays_held(board, completed_before):
    from hermes_cli.kanban_db_recovery_release import recover_task
    from tests.hermes_cli.test_kanban_recovery_completion import reopen
    tid = kb.create_task(board, title="accepted", assignee="worker")
    if completed_before:
        claim = kb.claim_task(board, tid)
        assert claim and kb.complete_task(board, tid, expected_run_id=claim.current_run_id)
        reopen(board, tid)
    claim = kb.claim_task(board, tid)
    assert claim and kb.request_review(board, tid, reviewer="reviewer", summary="accepted", expected_run_id=claim.current_run_id)
    claim = kb.claim_review_task(board, tid)
    assert claim and kb.block_task(board, tid, kind="capability", reason="review hold", expected_run_id=claim.current_run_id)
    # Deliberately lost routing metadata, not a supported phase transition.
    kb._append_event(board, tid, "status", {"status": "blocked"})
    before = list(board.iterdump())
    result = recover_task(board, tid, **request(board, tid))
    assert not result["released"] and "accepted execution" in result["reason"]
    assert list(board.iterdump()) == before


@pytest.mark.linux_only
def test_corrections_across_resolved_cycles_preserve_counts(board):
    from hermes_cli.kanban_db_recovery_release import recover_task
    tid = kb.create_task(board, title="correction cycles", assignee="worker")
    block(board, tid)
    a = recovery.get_recovery_state(board, tid)["active_blocker_id"]
    assert recover_task(board, tid, **request(board, tid, ref="evidence:a1"))["released"]
    block(board, tid)
    view = recovery.get_recovery_state(board, tid)
    result = recovery.classify_blocker(board, tid, observed_token=view["observed_token"], block_event_id=view["active_event_id"], rationale="different cause", evidence_refs=["evidence:classify-b"])
    assert result["ok"]
    b = result["state"]["active_blocker_id"]
    assert recover_task(board, tid, **request(board, tid, ref="evidence:b1"))["released"]
    block(board, tid)
    view = recovery.get_recovery_state(board, tid)
    result = recovery.classify_blocker(board, tid, observed_token=view["observed_token"], block_event_id=view["active_event_id"], existing_blocker_id=a, rationale="original recurred", evidence_refs=["evidence:classify-a"])
    assert result["ok"]
    assert result["state"]["blockers"][a]["count"] == 1
    assert result["state"]["blockers"][b]["count"] == 0
    assert recover_task(board, tid, **request(board, tid, ref="evidence:a2"))["released"]
    final = recovery.get_recovery_state(board, tid)
    assert final["blockers"][a]["cycle"] == 3
    assert final["blockers"][b]["cycle"] == 2


def test_dispatch_uses_actual_connection_and_strict_open_failure(board, monkeypatch):
    path = Path(board.execute("PRAGMA database_list").fetchone()[2])
    kb.create_task(board, title="ready", assignee="worker")
    before = list(board.iterdump())
    with kbc._dispatch_tick_lock(path) as held:
        assert held
        assert dispatch.dispatch_once(board, dry_run=True).skipped_locked
    original = Path.open
    def denied(self, *args, **kwargs):
        if self.name.endswith(".dispatch.lock"):
            raise PermissionError("test lock refusal")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    assert dispatch.dispatch_once(board, dry_run=True).skipped_locked
    assert list(board.iterdump()) == before
