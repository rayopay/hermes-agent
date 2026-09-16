"""Failure/refusal controls for the native classification-only slice."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_recovery import get_recovery_state, classify_blocker
from pathlib import Path
from hermes_cli import kanban_db_connect as kbc

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conn = kbc.connect(tmp_path / "board.db")
    yield conn
    conn.close()


def block(conn, tid, kind="capability"):
    claim = kb.claim_task(conn, tid, claimer="worker")
    assert claim
    assert kb.block_task(conn, tid, reason="observation", kind=kind,
                         expected_run_id=claim.current_run_id)


def classify(conn, tid, identity=None, view=None):
    from hermes_cli.kanban_db_recovery import get_recovery_state, classify_blocker
    view = view or get_recovery_state(conn, tid)
    return classify_blocker(conn, tid, observed_token=view["observed_token"],
                            block_event_id=view["active_event_id"],
                            existing_blocker_id=identity, rationale="Verified root cause",
                            evidence_refs=["test:root-cause"])


def snapshot(conn):
    return list(conn.iterdump())




def test_actual_delegation_worker_reporting_and_fake_actor(board, monkeypatch):
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import scoped_current_session_id
    tid = kb.create_task(board, title="target", assignee="worker")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    block(board, tid)  # reporting still works in native worker context
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    before = snapshot(board)
    with delegated_child_context("child"):
        with pytest.raises(PermissionError):
            classify(board, tid)
    view = get_recovery_state(board, tid)
    with pytest.raises(TypeError):
        classify_blocker(board, tid, observed_token=view["observed_token"],
                         block_event_id=view["active_event_id"], author="owner",
                         rationale="fake", evidence_refs=["ref"])
    assert snapshot(board) == before
    with scoped_current_session_id("actual-session"):
        result = classify(board, tid)
    assert result["ok"]
    event = board.execute("SELECT payload FROM task_events WHERE id=?", (result["correction_event_id"],)).fetchone()
    assert json.loads(event[0])["actor"]["session_id"] == "actual-session"


def test_corrections_never_erase_observations_and_gc_retains_refs(board):
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    first = get_recovery_state(board, tid)
    a = first["active_blocker_id"]
    b = classify(board, tid)["state"]["active_blocker_id"]
    assert classify(board, tid, a)["ok"]
    assert classify(board, tid, b)["ok"]
    state = get_recovery_state(board, tid)
    assert sum(x["count"] for x in state["blockers"].values()) == 1
    assert state["reports"][str(first["active_event_id"])]["original_blocker_id"] == a
    assert len(state["corrections"]) == 3
    assert kb.archive_task(board, tid)
    kb.gc_events(board, older_than_seconds=-1)
    ids = {row[0] for row in board.execute("SELECT id FROM task_events WHERE task_id=?", (tid,))}
    assert set(state["corrections"]) | {first["active_event_id"]} <= ids
    assert kb.delete_archived_task(board, tid)
    assert not board.execute("SELECT 1 FROM task_recovery WHERE task_id=?", (tid,)).fetchone()


def test_separate_connection_late_event_change_refused(board):
    from hermes_cli import kanban_db_connect as kbc
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    view = get_recovery_state(board, tid)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    with kbc.connect_closing(Path(path)) as other:
        kb.add_comment(other, tid, author="operator", body="new observation")
    before = snapshot(board)
    assert not classify(board, tid, view=view)["ok"]
    assert snapshot(board) == before


def test_classification_failure_rolls_back_audit_and_projection(board):
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    board.execute("CREATE TRIGGER refuse_classification BEFORE UPDATE ON task_recovery BEGIN SELECT RAISE(ABORT, 'injected'); END")
    before = snapshot(board)
    with pytest.raises(Exception, match="injected"):
        classify(board, tid)
    assert snapshot(board) == before


@pytest.mark.parametrize("bad", [{}, {"version": 2}, [], None])
def test_malformed_projection_prevents_native_block(board, bad):
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    assert kb.unblock_task(board, tid)
    claim = kb.claim_task(board, tid, claimer="worker")
    board.execute("UPDATE task_recovery SET state_json=? WHERE task_id=?", (json.dumps(bad), tid))
    board.commit()
    before = snapshot(board)
    with pytest.raises(ValueError):
        kb.block_task(board, tid, reason="must not mutate", expected_run_id=claim.current_run_id)
    assert snapshot(board) == before
