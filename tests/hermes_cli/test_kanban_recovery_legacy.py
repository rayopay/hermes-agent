"""SQL fixtures: actual fad0038 schema/native create-claim-block history.
Generated in credential-free disposable HOME; never a deleted tracked projection.
"""
import json
import sqlite3
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_recovery import get_recovery_state, classify_blocker

@pytest.mark.parametrize("status", ["blocked", "triage"])
def test_legacy_migration_and_direct_held_classification(tmp_path, monkeypatch, status):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path)
    c.executescript((Path(__file__).parent / "fixtures/recovery" / ("legacy_"+status+".sql")).read_text())
    before = {table:list(c.execute("SELECT * FROM "+table)) for table in ("tasks","task_events","task_runs")}
    c.close()
    from agent.delegation_context import delegated_child_context
    with delegated_child_context("child"):
        ro = kbc.connect(path)
        assert not ro.execute("SELECT 1 FROM sqlite_master WHERE name='task_recovery'").fetchone()
        with pytest.raises(PermissionError):
            classify_blocker(ro, "ignored", observed_token="x", block_event_id=1, rationale="no", evidence_refs=["no"])
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE forbidden(x)")
        ro.close()
        with pytest.raises(sqlite3.OperationalError):
            kbc.connect(tmp_path / "absent.db")
        assert not (tmp_path / "absent.db").exists()
    for _ in range(2):
        kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
        c = kbc.connect(path)
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='task_recovery'").fetchone()
        assert {table:[tuple(r) for r in c.execute("SELECT * FROM "+table)] for table in before} == before
        c.close()
    c = kbc.connect(path)
    try:
        t = c.execute("SELECT id FROM tasks").fetchone()[0]
        dump = list(c.iterdump())
        v = get_recovery_state(c,t)
        assert list(c.iterdump()) == dump
        assert v["legacy"] and v["active_event_id"] is not None
        old = kb.get_task(c,t)
        result = classify_blocker(c,t,observed_token=v["observed_token"],block_event_id=v["active_event_id"],rationale="exact latest report",evidence_refs=["fixture:native-history"])
        assert result["ok"]
        assert kb.get_task(c,t).status == old.status == status
        assert kb.get_task(c,t).consecutive_failures == old.consecutive_failures
        assert sum(b["legacy_lower_bound"] for b in result["state"]["blockers"].values()) == old.block_recurrences - 1
        assert sum(b["count"] for b in result["state"]["blockers"].values()) == old.block_recurrences
        assert [tuple(r) for r in c.execute("SELECT * FROM task_events ORDER BY id LIMIT ?", (len(before["task_events"]),))] == before["task_events"]
    finally:
        c.close()


@pytest.fixture
def dependency_first_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as native:
        native.executescript((Path(__file__).parent / "fixtures/recovery/legacy_blocked.sql").read_text())
    c = kbc.connect(path)
    yield c
    c.close()


def dependency_report(c, t):
    kb.recompute_ready(c)
    claim = kb.claim_task(c, t, claimer="worker")
    assert claim and kb.get_task(c, t).status == "running"
    assert kb.block_task(c, t, kind="dependency", reason="waiting",
                         expected_run_id=claim.current_run_id)
    assert kb.get_task(c, t).status == "todo"
    return get_recovery_state(c, t)


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("ending", ["report", "complete"])
def test_dependency_first_preserves_native_budget_and_history(dependency_first_board, legacy, ending):
    c = dependency_first_board
    t = c.execute("SELECT id FROM tasks").fetchone()[0]
    if legacy:
        assert kb.unblock_task(c, t)
    else:
        t = kb.create_task(c, title="fresh dependency", assignee="worker")
    inherited = kb.get_task(c, t).block_recurrences
    assert inherited == (1 if legacy else 0)
    view = dependency_report(c, t)
    identity = view["active_blocker_id"]
    second = dependency_report(c, t)
    assert second["active_blocker_id"] == identity
    assert all(not r["counted"] for r in second["reports"].values())
    assert sum(b["count"] for b in second["blockers"].values()) == inherited
    kb.recompute_ready(c)
    claim = kb.claim_task(c, t, claimer="worker")
    assert claim
    if ending == "report":
        assert kb.block_task(c, t, kind="capability", expected_run_id=claim.current_run_id)
        task = kb.get_task(c, t)
        assert task and task.block_recurrences == inherited + 1
        assert task.status == ("triage" if legacy else "blocked")
        counted = get_recovery_state(c, t)
        if legacy:
            assert counted["active_blocker_id"] == identity
    else:
        assert kb.complete_task(c, t, expected_run_id=claim.current_run_id)
        kb.gc_events(c, older_than_seconds=-1)
        closed = get_recovery_state(c, t)["closed_cycles"][0]["state"]
        for key in ("blockers", "reports", "corrections"):
            assert closed[key] == second[key]


@pytest.mark.parametrize("bound", [0, 3])
@pytest.mark.parametrize("consumer", ["read", "report", "complete"])
def test_dependency_first_projected_seed_corruption_refused(dependency_first_board, bound, consumer):
    c = dependency_first_board
    t = c.execute("SELECT id FROM tasks").fetchone()[0]
    assert kb.get_task(c, t).block_recurrences == 1
    assert kb.unblock_task(c, t)
    view = dependency_report(c, t)
    identity = view["active_blocker_id"]
    assert view["blockers"][identity]["count"] == 1
    assert not view["reports"][str(view["active_event_id"])]["counted"]
    claim = None
    if consumer != "read":
        kb.recompute_ready(c)
        claim = kb.claim_task(c, t, claimer="worker")
        assert claim and kb.get_task(c, t).status == "running"
    state = json.loads(c.execute("SELECT state_json FROM task_recovery WHERE task_id=?", (t,)).fetchone()[0])
    state["blockers"][identity].update(count=bound, legacy_lower_bound=bound)
    c.execute("UPDATE task_recovery SET state_json=? WHERE task_id=?", (json.dumps(state), t))
    before = list(c.iterdump())
    with pytest.raises(ValueError, match="Recovery lower bound disagrees with immutable seed"):
        if consumer == "read":
            get_recovery_state(c, t)
        elif consumer == "report":
            kb.block_task(c, t, kind="capability", expected_run_id=claim.current_run_id)
        else:
            kb.complete_task(c, t, expected_run_id=claim.current_run_id)
    assert list(c.iterdump()) == before
    assert not c.in_transaction
