"""Real registered tools and parsed CLI on disposable native boards; no spawn."""
import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_recovery import get_recovery_state


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets: [kanban]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect_closing() as conn:
        yield conn


def block(conn, tid):
    claim = kb.claim_task(conn, tid, claimer="worker")
    assert claim
    assert kb.block_task(conn, tid, reason="observed cause", kind="capability",
                         expected_run_id=claim.current_run_id)


def cli(capsys, *words):
    from hermes_cli.kanban import kanban_command
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    rc = kanban_command(parser.parse_args(["kanban", *words]))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def tool(name, **args):
    import tools.kanban_tools  # registers real handlers
    from tools.registry import registry
    entry = next(e for e in registry.get_all_entries() if e.name == name)
    return json.loads(entry.handler(args, run_context={}))


def request(view, **extra):
    return {"action": "classify", "observed_token": view["observed_token"],
            "block_event_id": view["active_event_id"], "rationale": "Verified distinct cause",
            "evidence_refs": ["test:root-cause"], **extra}


@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("held", ["blocked", "triage"])
def test_classification_is_audited_not_release(board, capsys, surface, held):
    tid = kb.create_task(board, title="held", assignee="worker")
    block(board, tid)
    a = get_recovery_state(board, tid)["active_blocker_id"]
    # Conventional no-recovery unblock remains supported.
    if surface == "tool":
        assert tool("kanban_unblock", task_id=tid)["ok"]
    else:
        assert cli(capsys, "unblock", tid)[0] == 0
    if held == "triage":
        block(board, tid)
    else:
        # Fresh independent native hold (not a counter reset).
        tid = kb.create_task(board, title="single hold", assignee="worker")
        block(board, tid)
        a = get_recovery_state(board, tid)["active_blocker_id"]
    assert kb.get_task(board, tid).status == held
    before = list(board.iterdump())
    if surface == "tool":
        shown = tool("kanban_show", task_id=tid)
    else:
        rc, out, _ = cli(capsys, "show", tid, "--json")
        assert rc == 0
        shown = json.loads(out)
        rc, plain, _ = cli(capsys, "show", tid)
        assert rc == 0
        assert shown["recovery"]["observed_token"] in plain
        assert shown["recovery"]["active_blocker_id"] in plain
    view = shown["recovery"]
    assert view == get_recovery_state(board, tid)
    assert list(board.iterdump()) == before
    original = kb.list_events(board, tid)
    original_task = kb.get_task(board, tid)
    original_runs = kb.list_runs(board, tid)
    original_count = view["blockers"][a]["count"]
    b = None
    for identity in (None, a):
        payload = request(view, **({"existing_blocker_id": identity} if identity else {}))
        if surface == "tool":
            from tools.registry import registry
            schema = registry.get_schema("kanban_unblock")["parameters"]
            assert "recovery" not in schema["required"]
            shape = schema["properties"]["recovery"]
            assert shape["properties"]["action"]["enum"] == [payload["action"]]
            assert set(shape["required"]) <= payload.keys() <= shape["properties"].keys()
            result = tool("kanban_unblock", task_id=tid, recovery=payload)
        else:
            rc, out, _ = cli(capsys, "unblock", tid, "--recovery-json", json.dumps(payload))
            assert rc == 0
            result = json.loads(out)
        assert result["ok"] and result["classified"] and result["released"] is False
        assert result["status"] == held
        assert kb.get_task(board, tid).status == held
        assert kb.list_runs(board, tid) == original_runs
        current_task = kb.get_task(board, tid)
        assert {k: v for k, v in vars(current_task).items() if k != "block_recurrences"} == {
            k: v for k, v in vars(original_task).items() if k != "block_recurrences"}
        assert kb.get_task(board, tid).claim_lock is None
        view = get_recovery_state(board, tid)
        assert result["state"] == view
        assert kb.list_events(board, tid)[:len(original)] == original
        if identity is None:
            b = view["active_blocker_id"]
            assert a != b
            assert view["blockers"][a]["count"] == original_count - 1
            assert view["blockers"][b]["count"] == 1
        else:
            assert view["blockers"][a]["count"] == original_count
            assert view["blockers"][b]["count"] == 0
    audit = kb.list_events(board, tid)[-1].payload
    assert audit["rationale"] == "Verified distinct cause"
    assert audit["evidence_refs"] == ["test:root-cause"]
    assert "profile" in audit["actor"]


@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_refusals_and_reader_fences_leave_exact_database(board, capsys, monkeypatch, surface):
    tid = kb.create_task(board, title="held", assignee="worker")
    block(board, tid)
    view = get_recovery_state(board, tid)
    payload = request(view)
    kb.add_comment(board, tid, author="owner", body="invalidates observation")
    fresh = request(get_recovery_state(board, tid))
    invalid = [payload, None, [], {}, {**fresh, "action": "retry"},
               {**fresh, "author": "owner"}, {**fresh, "profile": "owner"},
               {**fresh, "block_event_id": True}, {**fresh, "evidence_refs": []},
               {**fresh, "existing_blocker_id": "unknown"}]
    for recovery in invalid:
        before = list(board.iterdump())
        if surface == "tool":
            result = tool("kanban_unblock", task_id=tid, recovery=recovery)
            assert not result.get("ok")
        else:
            assert cli(capsys, "unblock", tid, "--recovery-json", json.dumps(recovery))[0] != 0
        assert list(board.iterdump()) == before
    if surface == "cli":
        for words in [(tid, "--recovery-json", json.dumps(fresh), "--reason", "must not comment"),
                      (tid, "--recovery-json", "{"),
                      (tid, tid, "--recovery-json", json.dumps(payload))]:
            before = list(board.iterdump())
            assert cli(capsys, "unblock", *words)[0] != 0
            assert list(board.iterdump()) == before
    # Preserve inherited identity markers, including worker descendants.
    for marker in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        with monkeypatch.context() as context:
            context.setenv(marker, tid)
            before = list(board.iterdump())
            payload = request(get_recovery_state(board, tid))
            if surface == "tool":
                assert tool("kanban_show", task_id=tid)["recovery"] == get_recovery_state(board, tid)
                assert not tool("kanban_unblock", task_id=tid, recovery=payload).get("ok")
            else:
                assert cli(capsys, "show", tid, "--json")[0] == 0
                assert cli(capsys, "unblock", tid, "--recovery-json", json.dumps(payload))[0] != 0
            assert list(board.iterdump()) == before
