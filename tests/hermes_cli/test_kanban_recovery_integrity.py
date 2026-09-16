"""Native history first; SQL below deliberately corrupts disposable fixtures."""
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_recovery import get_recovery_state, classify_blocker

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    c = kbc.connect(tmp_path / "board.db")
    yield c
    c.close()

def block(c, t):
    claim = kb.claim_task(c, t, claimer="worker")
    assert claim
    assert kb.block_task(c, t, reason="observed", kind="capability", expected_run_id=claim.current_run_id)

def classify(c, t, v):
    return classify_blocker(c, t, observed_token=v["observed_token"], block_event_id=v["active_event_id"], rationale="checked", evidence_refs=["fixture:proof"])

@pytest.mark.parametrize("damage", ["pointer", "missing_report", "foreign_report", "missing_correction", "foreign_correction", "deleted_projection", "original_payload", "correction_chain"])
@pytest.mark.parametrize("phase", ["held", "running"])
def test_corrupt_audit_refuses_reads_classification_and_reports(board, damage, phase):
    t = kb.create_task(board, title="target", assignee="worker")
    other = kb.create_task(board, title="other", assignee="worker")
    block(board, t)
    v = get_recovery_state(board, t)
    a = v["active_blocker_id"]
    result = classify(board, t, v)
    assert result["ok"]
    v = result["state"]
    row = board.execute("SELECT state_json FROM task_recovery WHERE task_id=?", (t,)).fetchone()
    s = json.loads(row[0])
    report, correction = v["active_event_id"], result["correction_event_id"]
    if phase == "running":
        assert kb.unblock_task(board, t)
        claim = kb.claim_task(board, t, claimer="worker")
    if damage == "pointer":
        s["active_blocker_id"] = a
        board.execute("UPDATE task_recovery SET state_json=? WHERE task_id=?", (json.dumps(s), t))
    elif damage == "deleted_projection":
        board.execute("DELETE FROM task_recovery WHERE task_id=?", (t,))
    elif damage in ("missing_report", "missing_correction"):
        board.execute("DELETE FROM task_events WHERE id=?", (report if damage == "missing_report" else correction,))
    elif damage in ("foreign_report", "foreign_correction"):
        board.execute("UPDATE task_events SET task_id=? WHERE id=?", (other, report if damage == "foreign_report" else correction))
    else:
        event = report if damage == "original_payload" else correction
        p = json.loads(board.execute("SELECT payload FROM task_events WHERE id=?", (event,)).fetchone()[0])
        p["blocker_id" if damage == "original_payload" else "from_blocker_id"] = v["active_blocker_id"]
        board.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(p), event))
    before = list(board.iterdump())
    with pytest.raises(ValueError):
        get_recovery_state(board, t)
    with pytest.raises(ValueError):
        classify(board, t, v)
    assert list(board.iterdump()) == before
    if phase == "held":
        with pytest.raises(ValueError):
            kb.unblock_task(board, t)
        with pytest.raises(ValueError):
            kb.claim_task(board, t, claimer="worker")
    else:
        with pytest.raises(ValueError):
            kb.block_task(board, t, reason="refuse", expected_run_id=claim.current_run_id)
    assert list(board.iterdump()) == before

@pytest.mark.parametrize("lower_bound", [0, 3])
@pytest.mark.parametrize("consumer", ["read", "classify_new", "classify_existing", "report", "complete"])
def test_projection_only_identity_refuses_native_consumers(board, lower_bound, consumer):
    t = kb.create_task(board, title="origin required", assignee="worker")
    kb.create_task(board, title="unrelated state must survive", assignee="worker")
    block(board, t)
    view = get_recovery_state(board, t)
    claim = None
    if consumer in ("report", "complete"):
        assert kb.unblock_task(board, t)
        claim = kb.claim_task(board, t, claimer="worker")
        task = kb.get_task(board, t)
        assert claim and task and task.status == "running"
    state = json.loads(board.execute(
        "SELECT state_json FROM task_recovery WHERE task_id=?", (t,)).fetchone()[0])
    invented = "f" * 32
    assert invented not in state["blockers"]
    state["blockers"][invented] = {
        "cycle": 1, "count": lower_bound, "legacy_lower_bound": lower_bound,
    }
    # Only the disposable projection is damaged; immutable events stay untouched.
    board.execute("UPDATE task_recovery SET state_json=? WHERE task_id=?", (json.dumps(state), t))
    before = list(board.iterdump())
    if consumer.startswith("classify"):
        # Before the fix, show supplies a fresh token for the corrupt state, so
        # classification cannot hide behind stale-token refusal. After the fix,
        # use the earlier token and require the audit exception, not a denial.
        try:
            view = get_recovery_state(board, t)
        except ValueError as exc:
            assert "Recovery blocker identities lack audited origin" in str(exc)
        assert list(board.iterdump()) == before
    try:
        with pytest.raises(ValueError, match="Recovery blocker identities lack audited origin"):
            if consumer == "read":
                get_recovery_state(board, t)
            elif consumer.startswith("classify"):
                classify_blocker(board, t, observed_token=view["observed_token"],
                    block_event_id=view["active_event_id"],
                    existing_blocker_id=invented if consumer == "classify_existing" else None,
                    rationale="checked origin", evidence_refs=["fixture:origin"])
            elif consumer == "report":
                assert claim is not None
                kb.block_task(board, t, reason="next report", kind="capability",
                              expected_run_id=claim.current_run_id)
            else:
                assert claim is not None
                kb.complete_task(board, t, expected_run_id=claim.current_run_id)
    finally:
        assert list(board.iterdump()) == before
        assert not board.in_transaction


def test_audited_zero_count_identities_survive_classification_and_completion(board):
    t = kb.create_task(board, title="retained history", assignee="worker")
    block(board, t)
    original = get_recovery_state(board, t)
    a = original["active_blocker_id"]
    event = str(original["active_event_id"])
    first = classify(board, t, original)
    assert first["ok"]
    b = first["state"]["active_blocker_id"]
    second = classify(board, t, get_recovery_state(board, t))
    assert second["ok"]
    state = get_recovery_state(board, t)
    # A survives by original attribution; B only by correction endpoints.
    for identity in (a, b):
        assert state["blockers"][identity]["count"] == 0
        assert all(r["blocker_id"] != identity for r in state["reports"].values())
    assert state["reports"][event]["original_blocker_id"] == a
    native_before = [tuple(row) for row in board.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (t,))]
    assert kb.complete_task(board, t)
    closed = get_recovery_state(board, t)["closed_cycles"][0]["state"]
    for key in ("blockers", "reports", "corrections"):
        assert closed[key] == state[key]
    native_after = [tuple(row) for row in board.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (t,))]
    assert native_after[:len(native_before)] == native_before


@pytest.mark.parametrize("lower_bound", [0, 3])
def test_projection_only_identity_in_closed_snapshot_refuses_read(board, lower_bound):
    t = kb.create_task(board, title="closed origin", assignee="worker")
    block(board, t)
    assert kb.complete_task(board, t)
    state = json.loads(board.execute(
        "SELECT state_json FROM task_recovery WHERE task_id=?", (t,)).fetchone()[0])
    blockers = state["closed_cycles"][0]["state"]["blockers"]
    invented = "f" * 32
    assert invented not in blockers
    blockers[invented] = {"cycle": 1, "count": lower_bound, "legacy_lower_bound": lower_bound}
    board.execute("UPDATE task_recovery SET state_json=? WHERE task_id=?", (json.dumps(state), t))
    before = list(board.iterdump())
    with pytest.raises(ValueError, match="closed epoch disagrees with immutable audit"):
        get_recovery_state(board, t)
    assert list(board.iterdump()) == before


@pytest.mark.parametrize("change", ["event", "assignment", "parent", "claim"])
def test_second_connection_at_begin_boundary(board, monkeypatch, change):
    t = kb.create_task(board, title="target", assignee="worker")
    p = kb.create_task(board, title="parent", assignee="worker")
    block(board, t)
    v = get_recovery_state(board, t)
    path = Path(board.execute("PRAGMA database_list").fetchone()[2])
    boundary = kbc._execute_boundary_with_retry
    changed = []
    def intercept(conn, statement):
        if conn is board and statement == "BEGIN IMMEDIATE" and not changed:
            assert not conn.in_transaction
            changed.append(True)  # synchronous barrier before native BEGIN
            with kbc.connect_closing(path) as other:
                assert other is not board
                if change == "event":
                    kb.add_comment(other, t, author="operator", body="new evidence")
                elif change == "assignment":
                    assert kb.assign_task(other, t, "other")
                elif change == "parent":
                    kb.link_tasks(other, p, t)
                else:
                    assert kb.unblock_task(other, t)
                    assert kb.claim_task(other, t, claimer="worker")
                changed.append(list(other.iterdump()))
        return boundary(conn, statement)
    monkeypatch.setattr(kbc, "_execute_boundary_with_retry", intercept)
    assert not classify(board, t, v)["ok"]
    assert len(changed) == 2
    assert list(board.iterdump()) == changed[1]
