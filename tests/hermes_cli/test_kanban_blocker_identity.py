"""RP-HERMES-002 native identity/classification; no recovery release API."""
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
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


def test_same_category_aba_preserves_counts_and_hold(board):
    from hermes_cli.kanban_db_recovery import get_recovery_state
    tid = kb.create_task(board, title="target", assignee="worker")
    other = kb.create_task(board, title="untouched", assignee="worker")
    untouched = kb.get_task(board, other)
    block(board, tid)
    a = get_recovery_state(board, tid)["active_blocker_id"]
    assert kb.unblock_task(board, tid)
    block(board, tid)
    original = kb.list_events(board, tid)
    assert kb.get_task(board, tid).status == "triage"
    result = classify(board, tid)
    assert result["ok"]
    b = result["state"]["active_blocker_id"]
    assert a != b
    assert kb.get_task(board, tid).status == "triage"  # classification NEVER releases
    assert kb.get_task(board, tid).block_recurrences == 1
    assert kb.list_events(board, tid)[:len(original)] == original
    # Deliberate pre-existing specify is test setup, NOT a recovery API.
    assert kb.specify_triage_task(board, tid)
    block(board, tid, "needs_input")
    assert get_recovery_state(board, tid)["active_blocker_id"] == b
    assert classify(board, tid, a)["ok"]
    state = get_recovery_state(board, tid)
    assert state["blockers"][a]["count"] == 2
    assert state["blockers"][b]["count"] == 1
    assert kb.get_task(board, tid).status == "triage"
    assert kb.get_task(board, other) == untouched
    assert len(state["reports"]) == 3


def test_stale_exact_report_and_block_cas_are_noops(board):
    from hermes_cli.kanban_db_recovery import get_recovery_state
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    old = get_recovery_state(board, tid)
    assert classify(board, tid, view=old)["ok"]
    before = snapshot(board)
    assert not classify(board, tid, view=old)["ok"]
    assert snapshot(board) == before
    assert kb.unblock_task(board, tid)
    claim = kb.claim_task(board, tid, claimer="worker")
    before = snapshot(board)
    assert not kb.block_task(board, tid, expected_run_id=claim.current_run_id + 1)
    assert snapshot(board) == before


def test_readonly_legacy_and_malformed_fail_closed(board):
    from hermes_cli.kanban_db_recovery import get_recovery_state
    tid = kb.create_task(board, title="legacy", assignee="worker")
    before = snapshot(board)
    assert get_recovery_state(board, tid)["legacy"]
    assert snapshot(board) == before
    block(board, tid)
    # Explicit corruption: deleting tracked projection is NOT legacy history.
    board.execute("DELETE FROM task_recovery WHERE task_id=?", (tid,))
    board.commit()
    before = snapshot(board)
    with pytest.raises(ValueError, match="projection missing"):
        get_recovery_state(board, tid)
    assert snapshot(board) == before
    board.execute("INSERT INTO task_recovery VALUES (?,1,'{}')", (tid,))
    board.commit()
    before = snapshot(board)
    with pytest.raises(ValueError):
        get_recovery_state(board, tid)
    assert snapshot(board) == before


@pytest.mark.parametrize("marker", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_DELEGATED_CHILD_CONTEXT"])
def test_native_worker_markers_deny_classification(board, monkeypatch, marker):
    tid = kb.create_task(board, title="target", assignee="worker")
    block(board, tid)
    before = snapshot(board)
    monkeypatch.setenv(marker, "worker")
    with pytest.raises(PermissionError):
        classify(board, tid)
    assert snapshot(board) == before


def test_projection_failure_rolls_back_native_report(board):
    tid = kb.create_task(board, title="target", assignee="worker")
    claim = kb.claim_task(board, tid, claimer="worker")
    board.execute("CREATE TRIGGER refuse_recovery BEFORE INSERT ON task_recovery BEGIN SELECT RAISE(ABORT, 'injected'); END")
    before = snapshot(board)
    with pytest.raises(Exception, match="injected"):
        kb.block_task(board, tid, reason="failure", expected_run_id=claim.current_run_id)
    assert snapshot(board) == before
