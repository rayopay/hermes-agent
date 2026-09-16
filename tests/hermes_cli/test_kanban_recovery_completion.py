"""Native completion closes accounting, not ordinary unblock or status edits."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_recovery import get_recovery_state, classify_blocker


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conn = kbc.connect(tmp_path / "board.db")
    yield conn
    conn.close()


def block(conn, task):
    claim = kb.claim_task(conn, task, claimer="worker")
    assert claim
    assert kb.block_task(conn, task, kind="capability", reason="observed",
                         expected_run_id=claim.current_run_id)
    return claim.current_run_id


def reopen(conn, task):
    spec = importlib.util.spec_from_file_location(
        "completion_dashboard", Path(__file__).resolve().parents[2] /
        "plugins/kanban/dashboard/plugin_api.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._set_status_direct(conn, task, "ready")


def test_native_completion_reopen_starts_fresh_accounting(board):
    task = kb.create_task(board, title="original regression", assignee="worker")
    block(board, task)
    assert kb.complete_task(board, task)
    reopen(board, task)
    block(board, task)
    assert (kb.get_task(board, task).status, kb.get_task(board, task).block_recurrences) == ("blocked", 1)


def test_repeated_completion_reopen_retains_closed_identity_audit(board, monkeypatch):
    task = kb.create_task(board, title="cycles", assignee="worker")
    archived = []
    for index in range(3):
        block(board, task)
        assert (kb.get_task(board, task).status, kb.get_task(board, task).block_recurrences) == ("blocked", 1)
        view = get_recovery_state(board, task)
        # Distinct identity history is retained, including original attribution.
        result = classify_blocker(board, task, observed_token=view["observed_token"],
            block_event_id=view["active_event_id"], rationale="distinct cause",
            evidence_refs=["fixture:observed"])
        assert result["ok"]
        prior = result["state"]
        assert kb.complete_task(board, task)
        before = list(board.iterdump())
        assert not kb.complete_task(board, task)  # duplicate cannot append a closure
        assert list(board.iterdump()) == before
        closed = get_recovery_state(board, task)
        assert closed["blockers"] == {} and closed["reports"] == {}
        assert closed["active_blocker_id"] is None
        assert len(closed["closed_cycles"]) == index + 1
        cycle = closed["closed_cycles"][-1]
        assert cycle["state"]["blockers"] == prior["blockers"]
        assert cycle["state"]["reports"] == prior["reports"]
        assert cycle["state"]["corrections"] == prior["corrections"]
        archived.append(cycle)
        assert closed["closed_cycles"] == archived
        completed = board.execute("SELECT kind FROM task_events WHERE id=?", (cycle["completed_event_id"],)).fetchone()
        assert completed[0] == "completed"
        now = kb.time.time()
        monkeypatch.setattr(kb.time, "time", lambda: now + 100)
        kb.gc_events(board, older_than_seconds=0)
        assert get_recovery_state(board, task)["closed_cycles"] == archived
        reopen(board, task)
    block(board, task)
    assert kb.get_task(board, task).status == "blocked"
    assert kb.get_task(board, task).block_recurrences == 1
    assert get_recovery_state(board, task)["closed_cycles"] == archived


@pytest.mark.parametrize("control", ["unblock", "drag", "stale", "triage", "parent", "acceptance", "closure_fault", "event_fault", "untracked"])
def test_only_committed_native_completion_closes(board, monkeypatch, control):
    task = kb.create_task(board, title="control", assignee="worker")
    if control == "untracked":
        assert kb.complete_task(board, task)
        assert not board.execute("SELECT * FROM task_recovery WHERE task_id=?", (task,)).fetchall()
        return
    old_run = block(board, task)
    if control in ("unblock", "drag", "triage"):
        if control == "drag":
            reopen(board, task)
        else:
            assert kb.unblock_task(board, task)
        block(board, task)
        assert kb.get_task(board, task).status == "triage"
        assert kb.get_task(board, task).block_recurrences == 2
    if control == "stale":
        assert kb.complete_task(board, task)
        reopen(board, task)
        block(board, task)
        assert kb.get_task(board, task).status == "blocked"
        assert kb.get_task(board, task).block_recurrences == 1
    if control == "parent":
        parent = kb.create_task(board, title="unfinished parent", assignee="worker")
        kb.link_tasks(board, parent, task)
    if control == "acceptance":
        from hermes_cli import kanban_pr_acceptance_store as acceptance
        monkeypatch.setattr(acceptance, "prepare_acceptance", lambda *a: False)
    if control in ("closure_fault", "event_fault"):
        if control == "closure_fault":
            board.execute("CREATE TRIGGER refuse_close BEFORE UPDATE ON task_recovery BEGIN SELECT RAISE(ABORT, 'closure failure'); END")
        else:
            board.execute("CREATE TRIGGER refuse_complete BEFORE INSERT ON task_events WHEN NEW.kind='completed' BEGIN SELECT RAISE(ABORT, 'closure failure'); END")
    before = list(board.iterdump())
    if control in ("closure_fault", "event_fault"):
        with pytest.raises(sqlite3.IntegrityError, match="closure failure"):
            kb.complete_task(board, task)
    else:
        assert not kb.complete_task(board, task, expected_run_id=old_run if control == "stale" else None)
    assert list(board.iterdump()) == before


@pytest.mark.parametrize("damage", ["snapshot", "completion_missing", "closure_missing", "completion_foreign", "completion_payload", "revision", "old_report", "old_correction"])
def test_closed_audit_remains_required_after_reopen(board, damage):
    task = kb.create_task(board, title="audited", assignee="worker")
    other = kb.create_task(board, title="foreign", assignee="worker")
    block(board, task)
    view = get_recovery_state(board, task)
    result = classify_blocker(board, task, observed_token=view["observed_token"],
        block_event_id=view["active_event_id"], rationale="observed distinction",
        evidence_refs=["fixture:audit"])
    assert result["ok"] and kb.complete_task(board, task)
    state = json.loads(board.execute("SELECT state_json FROM task_recovery WHERE task_id=?", (task,)).fetchone()[0])
    cycle = state["closed_cycles"][0]
    reopen(board, task)
    claim = kb.claim_task(board, task, claimer="worker")
    assert claim
    # Deliberate disposable corruption, never a supported mutation API.
    if damage == "snapshot":
        identity = cycle["state"]["active_blocker_id"]
        cycle["state"]["blockers"][identity]["count"] += 1
        cycle["state"]["blockers"][identity]["legacy_lower_bound"] += 1
        board.execute("UPDATE task_recovery SET state_json=? WHERE task_id=?", (json.dumps(state), task))
    elif damage == "revision":
        board.execute("UPDATE task_recovery SET revision=? WHERE task_id=?", (cycle["prior_revision"], task))
    elif damage == "completion_foreign":
        board.execute("UPDATE task_events SET task_id=? WHERE id=?", (other, cycle["completed_event_id"]))
    elif damage == "completion_payload":
        board.execute("UPDATE task_events SET payload='{}' WHERE id=?", (cycle["completed_event_id"],))
    else:
        event = {"completion_missing": cycle["completed_event_id"],
                 "closure_missing": cycle["closure_event_id"],
                 "old_report": view["active_event_id"],
                 "old_correction": result["correction_event_id"]}[damage]
        board.execute("DELETE FROM task_events WHERE id=?", (event,))
    before = list(board.iterdump())
    with pytest.raises(ValueError):
        get_recovery_state(board, task)
    with pytest.raises(ValueError):
        kb.block_task(board, task, reason="new observation", expected_run_id=claim.current_run_id)
    with pytest.raises(ValueError):
        kb.complete_task(board, task, expected_run_id=claim.current_run_id)
    assert list(board.iterdump()) == before
