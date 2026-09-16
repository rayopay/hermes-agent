"""Actual registered recovery handlers and parsed CLI; real acceptance, inert transport."""
import json
import os

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_recovery as accounting
from tests.hermes_cli.test_kanban_recovery_surfaces import board, block, cli, tool
from tests.hermes_cli.test_kanban_recovery_finalize import transport
from tests.hermes_cli.test_kanban_recovery_release_surfaces import show, request


def setup_task(conn, contract="https://github.com/acme/repo/pull/7"):
    tid = kb.create_task(conn, title="accepted work", assignee="worker", completion_contract=contract)
    block(conn, tid)
    assert kb.unblock_task(conn, tid)
    block(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def invoke(capsys, surface, tid, payload):
    if surface == "tool":
        return tool("kanban_unblock", task_id=tid, recovery=payload)
    rc, out, err = cli(capsys, "unblock", tid, "--recovery-json", json.dumps(payload))
    if out.strip().startswith("{"):
        result = json.loads(out)
        assert rc == (0 if result["ok"] else 1)
        return result
    assert rc != 0
    return {"ok": False, "error": out + err}


def payload(capsys, surface, tid, mode="autonomous"):
    result = request(show(capsys, surface, tid), mode, "finalize")
    if mode == "autonomous":
        result.pop("instruction_ref", None)
    return result


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("mode", ["user_directed", "autonomous"])
def test_finalization_is_done_not_release(board, capsys, transport, monkeypatch, surface, mode):
    tid = setup_task(board)
    args = payload(capsys, surface, tid, mode)
    actor = accounting._actor()
    old_runs = kb.list_runs(board, tid)
    hooks = []
    monkeypatch.setattr(kb, "claim_task", lambda *a, **k: pytest.fail("claim replay"))
    monkeypatch.setattr(kb, "unblock_task", lambda *a, **k: pytest.fail("ordinary unblock fallback"))
    monkeypatch.setattr(kb, "_fire_task_hook", lambda *a, **k: hooks.append(a[0]))
    board.execute("CREATE TRIGGER no_cycle BEFORE UPDATE OF status ON tasks WHEN NEW.status NOT IN ('triage','done') BEGIN SELECT RAISE(ABORT,'status cycling'); END")
    result = invoke(capsys, surface, tid, args)
    assert result == {"ok": True, "completed": True, "held": False, "status": "done", "task_id": tid}
    assert kb.get_task(board, tid).status == "done"
    assert kb.get_task(board, tid).current_run_id is None
    assert kb.list_runs(board, tid) == old_runs
    events = kb.list_events(board, tid)
    kinds = [event.kind for event in events]
    assert kinds.count("completed") == kinds.count("blocker_cycle_closed") == kinds.count("pr_acceptance") == 1
    assert kinds[kinds.index("completed") + 1] == "blocker_cycle_closed"
    finalizations = [event.payload for event in events if event.kind == "triage_finalized"]
    assert finalizations == [{**{k: v for k, v in args.items() if k != "action"},
                              "instruction_ref": args.get("instruction_ref"), "actor": actor,
                              "settlement_basis": "OWNER ATTESTATIONS plus recorded local PID probes"}]
    assert hooks == ["kanban_task_completed"]
    assert transport["calls"]
    before = list(board.iterdump())
    assert not invoke(capsys, surface, tid, args)["ok"]
    assert list(board.iterdump()) == before
    assert hooks == ["kanban_task_completed"]


@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_malformed_finalize_never_enters_native_or_comments(board, capsys, monkeypatch, surface):
    from hermes_cli import kanban_db_recovery_finalize as native
    tid = setup_task(board)
    monkeypatch.setattr(native, "finalize_task", lambda *a, **k: pytest.fail("invalid request entered native"))
    view = accounting.get_recovery_state(board, tid)
    ref = dict(observed_token=view["observed_token"], task_id=tid,
               block_event_id=view["active_event_id"], blocker_id=view["active_blocker_id"],
               board_path="test:board", host="test:host", boot_id="test:boot", run_ids=[1],
               scopes=["workspace"], observed_at=1, assertion="settled", reference="test:assessment")
    base = dict(action="finalize", observed_token=view["observed_token"],
                block_event_id=view["active_event_id"], blocker_id=view["active_blocker_id"],
                rationale="test", evidence_refs=["test:attribution"], initiation_mode="autonomous",
                settlement_refs=[ref])
    invalid = [None, [], {}, {**base, "action": []}, {**base, "action": "unsupported"}]
    invalid += [{**base, key: value} for key, value in [
        ("actor", {"profile": "owner"}), ("profile", "owner"), ("author", "owner"),
        ("existing_blocker_id", "other"), ("metadata", {}), ("summary", "done"),
        ("block_event_id", True), ("observed_token", None), ("blocker_id", []),
        ("rationale", []), ("evidence_refs", [1]), ("initiation_mode", {}),
        ("instruction_ref", None), ("instruction_ref", []), ("settlement_refs", None)]]
    for key, value in [("actor", "forged"), ("observed_at", True), ("run_ids", [True]),
                       ("reference", None), ("assertion", []), ("scopes", "workspace")]:
        invalid.append({**base, "settlement_refs": [{**ref, key: value}]})
    invalid.append({**base, "settlement_refs": [{k: v for k, v in ref.items() if k != "reference"}]})
    for args in invalid:
        before = list(board.iterdump())
        assert not invoke(capsys, surface, tid, args).get("ok")
        assert list(board.iterdump()) == before
    if surface == "cli":
        for words in [(tid, "--recovery-json", json.dumps(base), "--reason", "no comment"),
                      (tid, "--recovery-json", "{"), (tid, tid, "--recovery-json", json.dumps(base))]:
            before = list(board.iterdump())
            assert cli(capsys, "unblock", *words)[0] != 0
            assert list(board.iterdump()) == before


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("fault", ["missing", "stale", "failure", "head", "absent", "local-only", "repo",
                                       "pid", "worker", "delegate", "contract_race", "instruction", "template"])
def test_native_refusals_preserve_full_board(board, capsys, transport, monkeypatch, surface, fault):
    from agent.delegation_context import delegated_child_context
    from contextlib import nullcontext
    contract = {"absent": None, "local-only": "local-only", "repo": "acme/repo"}.get(fault, "https://github.com/acme/repo/pull/7")
    tid = setup_task(board, contract)
    if fault == "pid":
        kb._append_event(board, tid, "spawned", {"pid": os.getpid()})
    args = payload(capsys, surface, tid)
    if fault == "instruction":
        args["initiation_mode"] = "user_directed"  # Otherwise valid settlement; only instruction absent.
    if fault == "template":
        args["settlement_refs"][0].pop("assertion")
    transport["fault"] = fault
    baseline = [list(board.iterdump())]
    if fault == "contract_race":
        def race():
            with kbc.connect_closing(kbc.connection_db_path(board)) as other:
                other.execute("UPDATE tasks SET completion_contract='local-only' WHERE id=?", (tid,))
                baseline[0] = list(other.iterdump())
        transport["race"] = race
    if fault == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setattr(kb, "unblock_task", lambda *a, **k: pytest.fail("fallback mutation"))
    with delegated_child_context("test-child") if fault == "delegate" else nullcontext():
        result = invoke(capsys, surface, tid, args)
    assert not result.get("ok")
    if "held" in result:
        assert result["held"] and not result["completed"] and result["status"] == "triage"
    assert list(board.iterdump()) == baseline[0]
    assert kb.get_task(board, tid).status == "triage"
    if fault in {"missing", "stale", "failure", "head"}:
        assert transport["calls"] and result["reason"] == "current PR acceptance not established"
    if fault == "contract_race":
        assert transport["calls"] and result["reason"] == "stale report or observation"
    if fault == "instruction":
        assert result["reason"] == "invalid finalization request"


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_early_postcommit_error_reports_durable_done_without_replay(board, capsys, transport, monkeypatch, surface):
    tid = setup_task(board)
    args = payload(capsys, surface, tid)
    calls = []
    def fail(conn, task_id, *a, **kw):
        assert not conn.in_transaction and kb.get_task(conn, tid).status == "done"
        calls.append(task_id)
        raise OSError("test early postcommit failure")
    monkeypatch.setattr(kb, "_flag_phantom_prose_refs", fail)
    result = invoke(capsys, surface, tid, args)
    assert not result.get("ok") and "test early postcommit failure" in result["error"]
    assert "status=done" in result["error"] and not result.get("held", False)
    assert kb.get_task(board, tid).status == "done"
    with kbc._dispatch_tick_lock(kbc.connection_db_path(board), strict=True) as locked:
        assert locked
    before = list(board.iterdump())
    assert not invoke(capsys, surface, tid, args)["ok"]
    assert list(board.iterdump()) == before and calls == [tid]
    kinds = [e.kind for e in kb.list_events(board, tid)]
    assert kinds.count("completed") == kinds.count("blocker_cycle_closed") == kinds.count("triage_finalized") == 1


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_authoritative_finalize_result_needs_no_status_read(board, capsys, transport, monkeypatch, surface):
    from hermes_cli import kanban_db_recovery_finalize as native
    tid = setup_task(board)
    args = payload(capsys, surface, tid)
    old_runs = kb.list_runs(board, tid)
    finalize = native.finalize_task
    get_task = kb.get_task
    returned = []
    reads = []

    def unexpected_read(*a, **kw):
        reads.append(True)
        raise OSError("unnecessary post-return status read")

    def finish(*a, **kw):
        result = finalize(*a, **kw)  # Let native postcommit reads finish first.
        assert result["status"] == "done"
        returned.append((result, list(board.iterdump())))
        monkeypatch.setattr(kb, "get_task", unexpected_read)
        return result

    monkeypatch.setattr(native, "finalize_task", finish)
    result = invoke(capsys, surface, tid, args)
    assert len(returned) == 1 and reads == []
    assert result == {"task_id": tid, **returned[0][0]}
    assert get_task(board, tid).status == "done"
    assert kb.list_runs(board, tid) == old_runs
    assert list(board.iterdump()) == returned[0][1]
    kinds = [e.kind for e in kb.list_events(board, tid)]
    assert kinds.count("completed") == kinds.count("blocker_cycle_closed") == kinds.count("triage_finalized") == 1
    assert transport["calls"]


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli", "raw"])
def test_failed_status_observation_preserves_original_error(board, capsys, transport, monkeypatch, surface):
    from hermes_cli.kanban_recovery import apply_recovery
    tid = setup_task(board)
    args = payload(capsys, "tool" if surface == "raw" else surface, tid)
    old_runs = kb.list_runs(board, tid)
    get_task = kb.get_task
    original = OSError("test original early postcommit failure")
    calls = []
    observations = []
    committed = []

    def failed_observation(*a, **kw):
        observations.append(True)
        raise LookupError("test diagnostic observation failure")

    def fail(conn, task_id, *a, **kw):
        assert not conn.in_transaction and get_task(conn, tid).status == "done"
        calls.append(task_id)
        committed.append(list(board.iterdump()))
        monkeypatch.setattr(kb, "get_task", failed_observation)
        raise original

    monkeypatch.setattr(kb, "_flag_phantom_prose_refs", fail)
    expected = "finalize: OSError: test original early postcommit failure"
    if surface == "raw":
        with pytest.raises(RuntimeError) as exc:
            apply_recovery(board, tid, args)
        assert str(exc.value) == expected
        assert exc.value.__cause__ is original
    else:
        result = invoke(capsys, surface, tid, args)
        assert not result.get("ok") and expected in result["error"]
        assert not any(result.get(key) for key in ("held", "completed", "status"))
        assert "observation failure" not in result["error"]
        assert "observed committed status" not in result["error"]
        assert "rollback" not in result["error"]
    assert calls == [tid] and observations == [True]
    assert get_task(board, tid).status == "done"
    assert kb.list_runs(board, tid) == old_runs
    assert list(board.iterdump()) == committed[0]
    kinds = [e.kind for e in kb.list_events(board, tid)]
    assert kinds.count("completed") == kinds.count("blocker_cycle_closed") == kinds.count("triage_finalized") == 1
    assert transport["calls"]


def test_schema_and_parsed_help_describe_completion_not_acceptance_by_reference(capsys):
    import argparse
    import tools.kanban_tools
    from tools.registry import registry
    from hermes_cli.kanban_parser import build_parser
    schema = registry.get_schema("kanban_unblock")
    recovery = schema["parameters"]["properties"]["recovery"]
    assert "finalize" in recovery["properties"]["action"]["enum"]
    assert "acceptance" in recovery["description"].lower()
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["kanban", "unblock", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "finalize" in text and "done" in text and "acceptance" in text
