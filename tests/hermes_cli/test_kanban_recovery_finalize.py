"""Direct completion: native engine, mocked GitHub transport, disposable boards."""
import json
import os
import sqlite3
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb, kanban_db_recovery as recovery
from tests.hermes_cli.test_kanban_recovery_release import board, block, request


@pytest.fixture
def transport(monkeypatch):
    from hermes_cli import kanban_pr_acceptance as acceptance
    state = {"calls": [], "fault": None, "pr_reads": 0, "race_calls": 0}
    sha = "a" * 40
    def api(endpoint, **kwargs):
        state["calls"].append(endpoint)
        fault = state["fault"]
        if endpoint == "repos/acme/repo/pulls/7":
            assert not kwargs.get("paginate")
            state["pr_reads"] += 1
            final = state["pr_reads"] == 2
            return {"head": {"sha": "b" * 40 if final and fault == "head" else sha},
                    "base": {"ref": "release" if final and fault == "base" else "main"},
                    "state": "open", "merged": False}
        if endpoint == f"repos/acme/repo/commits/{sha}/check-runs?per_page=100&filter=latest":
            assert kwargs == {"paginate": True}
            if state.get("race"):
                state["race_calls"] += 1
                state["race"]()
            runs = [] if fault == "missing" else [{"id": 1, "name": "required",
                "head_sha": "b" * 40 if fault == "stale" else sha, "app": {"id": 1},
                "check_suite": {"id": 10}, "status": "completed",
                "conclusion": "failure" if fault == "failure" else "success"}]
            runs = state.get("runs", runs)
            return [{"total_count": len(runs), "check_runs": runs}]
        if endpoint == f"repos/acme/repo/commits/{sha}/statuses?per_page=100":
            assert kwargs == {"paginate": True}
            return [[]]
        pytest.fail(f"Unexpected transport or paid-policy request: {endpoint}")
    monkeypatch.setattr(acceptance, "_api", api)
    return state


def held(conn, contract="https://github.com/acme/repo/pull/7"):
    tid = kb.create_task(conn, title="accepted PR", assignee="worker", completion_contract=contract)
    block(conn, tid)
    assert kb.unblock_task(conn, tid)
    block(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    args = request(conn, tid)
    args.pop("action")
    return tid, args


@pytest.mark.linux_only
def test_direct_completion_uses_native_acceptance_once(board, transport, monkeypatch):
    from hermes_cli.kanban_db_recovery_finalize import finalize_task
    tid, args = held(board)
    old_runs = [tuple(r) for r in board.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))]
    hooks = []
    monkeypatch.setattr(kb, "claim_task", lambda *a, **k: pytest.fail("execution replay"))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **k: hooks.append((a, board.in_transaction)))
    board.execute("CREATE TRIGGER no_cycle BEFORE UPDATE OF status ON tasks WHEN NEW.status NOT IN ('triage','done') BEGIN SELECT RAISE(ABORT,'status cycling'); END")
    assert not kb.complete_task(board, tid)
    assert finalize_task(board, tid, **args)["ok"]
    assert kb.get_task(board, tid).status == "done"
    kinds = [r[0] for r in board.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
    assert kinds.count("completed") == kinds.count("blocker_cycle_closed") == 1
    assert kinds[kinds.index("completed") + 1] == "blocker_cycle_closed"
    assert kinds.count("pr_acceptance") == 1
    assert hooks[0][0][0] == "kanban_task_completed" and len(hooks) == 1 and not hooks[0][1]
    assert recovery.get_recovery_state(board, tid)["closed_cycles"]
    assert transport["calls"]
    assert [tuple(r) for r in board.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))] == old_runs
    before = list(board.iterdump())
    assert not finalize_task(board, tid, **args)["ok"]
    assert list(board.iterdump()) == before


@pytest.mark.linux_only
@pytest.mark.parametrize("fault", ["missing", "stale", "failure", "head", "local-only", "absent", "repo", "worker", "delegate", "pid", "token", "parent", "contract_race", "parent_race", "token_race", "event_fault", "closure_fault"])
def test_denial_and_sql_fault_preserve_full_state(board, transport, monkeypatch, fault):
    from hermes_cli.kanban_db_recovery_finalize import finalize_task
    from hermes_cli.kanban_db_connect import connect
    from agent.delegation_context import delegated_child_context
    contract = {"local-only": "local-only", "absent": None, "repo": "acme/repo"}.get(fault, "https://github.com/acme/repo/pull/7")
    tid, args = held(board, contract)
    hooks = []
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **k: hooks.append(a))
    transport["fault"] = fault
    if fault == "token":
        args["observed_token"] = "stale"
    if fault == "pid":
        kb._append_event(board, tid, "spawned", {"pid": os.getpid()})
        args = request(board, tid); args.pop("action")
    if fault == "parent":
        parent = kb.create_task(board, title="unfinished")
        kb.link_tasks(board, parent, tid)
        args = request(board, tid); args.pop("action")
        hooks.clear()
    if fault.endswith("_fault"):
        sql = "BEFORE INSERT ON task_events WHEN NEW.kind='completed'" if fault == "event_fault" else "BEFORE UPDATE ON task_recovery"
        board.execute(f"CREATE TRIGGER refuse {sql} BEGIN SELECT RAISE(ABORT,'rollback proof'); END")
    baseline = [list(board.iterdump())]
    if fault in {"contract_race", "parent_race", "token_race"}:
        def race():
            with connect(Path(board.execute("PRAGMA database_list").fetchone()[2])) as rival:
                if fault == "contract_race":
                    rival.execute("UPDATE tasks SET completion_contract='local-only' WHERE id=?", (tid,))
                elif fault == "parent_race":
                    parent = kb.create_task(rival, title="late parent")
                    kb.link_tasks(rival, parent, tid)
                    hooks.clear()
                else:
                    kb._append_event(rival, tid, "comment", {"text": "competing writer"})
                baseline[0] = list(rival.iterdump())
        transport["race"] = race
    if fault == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        with pytest.raises(PermissionError): finalize_task(board, tid, **args)
    elif fault == "delegate":
        with delegated_child_context("child"):
            with pytest.raises(PermissionError): finalize_task(board, tid, **args)
    elif fault.endswith("_fault"):
        with pytest.raises(sqlite3.IntegrityError, match="rollback proof"): finalize_task(board, tid, **args)
    else:
        assert not finalize_task(board, tid, **args)["ok"]
    assert list(board.iterdump()) == baseline[0]
    assert not hooks
    if fault in {"contract_race", "parent_race", "token_race"}:
        assert transport["race_calls"] == 1
    if fault.endswith("_fault") or fault in {"missing", "failure", "head"}:
        assert transport["pr_reads"] == 2
    if fault == "stale":
        assert any("/statuses" in call for call in transport["calls"])
    if fault in {"worker", "delegate", "pid", "token", "parent", "local-only", "absent", "repo"}:
        assert not transport["calls"]


@pytest.mark.linux_only
@pytest.mark.parametrize("case,accepted", [
    ("optional_failure", False), ("skipped", True), ("neutral", True),
    ("zero", False), ("all_skipped", False), ("all_neutral", False),
    ("bad_check", False), ("bad_suite", False), ("head", False), ("base", False),
])
def test_reported_ci_composes_with_native_finalization(board, transport, monkeypatch, case, accepted):
    from hermes_cli.kanban_db_recovery_finalize import finalize_task
    tid, _ = held(board)
    parent = kb.create_task(board, title="accepted prerequisite", assignee="worker", completion_contract="local-only")
    assert kb.complete_task(board, parent)
    kb.link_tasks(board, parent, tid)
    args = request(board, tid); args.pop("action")
    old_task = kb.get_task(board, tid)
    old_runs = kb.list_runs(board, tid)
    old_events = kb.list_events(board, tid)
    old_deps = [tuple(r) for r in board.execute("SELECT * FROM task_links")]
    original = {"id": 1, "name": "build", "app": {"id": 1}, "check_suite": {"id": 10},
                "head_sha": "a" * 40, "status": "completed", "conclusion": "success"}
    runs = [original]
    if case in {"optional_failure", "skipped", "neutral"}:
        runs.append({**original, "id": 2, "name": "optional", "check_suite": {"id": 20},
                     "conclusion": "failure" if case == "optional_failure" else case})
    elif case == "zero":
        runs = []
    elif case in {"all_skipped", "all_neutral"}:
        runs = [{**original, "conclusion": case.removeprefix("all_")}]
    elif case == "bad_check":
        runs = [{**original, "id": True}]
    elif case == "bad_suite":
        runs = [{**original, "check_suite": {"id": 0}}]
    transport.update(runs=runs, fault=case)
    monkeypatch.setattr(kb, "claim_task", lambda *a, **k: pytest.fail("worker replay"))
    board.execute("CREATE TRIGGER no_ready BEFORE UPDATE OF status ON tasks WHEN NEW.id='" + tid + "' AND NEW.status NOT IN ('triage','done') BEGIN SELECT RAISE(ABORT,'status cycling'); END")
    before = list(board.iterdump())
    result = finalize_task(board, tid, **args)
    assert result["ok"] is accepted
    prefix = ["repos/acme/repo/pulls/7",
              "repos/acme/repo/commits/" + "a" * 40 + "/check-runs?per_page=100&filter=latest",
              "repos/acme/repo/commits/" + "a" * 40 + "/statuses?per_page=100"]
    assert transport["calls"] == prefix + ([] if case in {"bad_check", "bad_suite"} else [prefix[0]])
    assert kb.list_runs(board, tid) == old_runs
    assert [tuple(r) for r in board.execute("SELECT * FROM task_links")] == old_deps
    if not accepted:
        assert list(board.iterdump()) == before
        assert result["reason"] == "current PR acceptance not established"
        return
    task = kb.get_task(board, tid)
    assert task.status == "done" and task.current_run_id is None
    assert task.assignee == old_task.assignee and task.completion_contract == old_task.completion_contract
    events = kb.list_events(board, tid)
    assert events[:len(old_events)] == old_events
    kinds = [e.kind for e in events]
    assert kinds.count("completed") == kinds.count("blocker_cycle_closed") == kinds.count("triage_finalized") == 1
    receipts = [e.payload for e in events if e.kind == "pr_acceptance"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["ok"] and receipt["acceptance_basis"] == "reported-ci"
    assert (receipt["head_sha"], receipt["base_ref"], receipt["pr_url"]) == ("a" * 40, "main", old_task.completion_contract)
    assert {c["classification"] for c in receipt["checks"]} == {"success", "non_blocking"}
    assert recovery.get_recovery_state(board, tid)["closed_cycles"]
    after = list(board.iterdump()); calls = list(transport["calls"])
    assert not finalize_task(board, tid, **args)["ok"]
    assert list(board.iterdump()) == after and transport["calls"] == calls
