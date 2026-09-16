"""Registered handlers (run_context) and parsed CLI; real disposable SQLite, no launch."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_recovery import get_recovery_state
from tests.hermes_cli.test_kanban_recovery_surfaces import board, block, cli, tool


def show(capsys, surface, tid):
    if surface == "tool":
        return tool("kanban_show", task_id=tid)
    rc, out, _ = cli(capsys, "show", tid, "--json")
    assert rc == 0
    return json.loads(out)


def release(capsys, surface, tid, payload):
    if surface == "tool":
        return tool("kanban_unblock", task_id=tid, recovery=payload)
    rc, out, err = cli(capsys, "unblock", tid, "--recovery-json", json.dumps(payload))
    if rc:
        return {"ok": False, "error": out + err}
    return json.loads(out)


def request(shown, mode="autonomous", action="resolved_resume"):
    view = shown["recovery"]
    scope = shown["settlement_scope"]
    assert scope["available"] and scope["read_only"]
    template = scope["template"]
    assert template["observed_token"] == view["observed_token"]
    assert "assertion" not in template and "reference" not in template
    # Explicit TEST owner assessment, not a claim about any real workspace.
    return dict(action=action, observed_token=view["observed_token"],
                block_event_id=view["active_event_id"], blocker_id=view["active_blocker_id"],
                rationale="Disposable test owner assessed all settlement scopes",
                evidence_refs=["test:synthetic-repair"], initiation_mode=mode,
                instruction_ref="test:directed-instruction" if mode == "user_directed" else None,
                settlement_refs=[{**template, "assertion": "settled", "reference": "test:synthetic-assessment"}])


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("mode", ["user_directed", "autonomous"])
def test_real_release_cycle_and_retry(board, capsys, surface, mode):
    tid = kb.create_task(board, title="cycle", assignee="worker")
    block(board, tid)
    first = get_recovery_state(board, tid)
    result = release(capsys, surface, tid, request(show(capsys, surface, tid), mode, "retry"))
    assert result["ok"] and result["released"] and result["status"] == "ready", result
    block(board, tid)
    assert kb.get_task(board, tid).status == "triage"
    before = list(board.iterdump())
    shown = show(capsys, surface, tid)
    assert list(board.iterdump()) == before
    if surface == "cli":
        rc, out, _ = cli(capsys, "show", tid)
        assert rc == 0 and shown["recovery"]["observed_token"] in out
        assert "settlement_scope" in out and "OWNER" in out
    payload = request(shown, mode, "retry")
    result = release(capsys, surface, tid, payload)
    assert not result["ok"] and not result.get("released", False)
    assert list(board.iterdump()) == before
    payload["action"] = "resolved_resume"
    result = release(capsys, surface, tid, payload)
    assert result["released"] and result["eligible"] and not result["held"]
    assert result["status"] == kb.get_task(board, tid).status == "ready"
    assert kb.get_task(board, tid).current_run_id is None
    block(board, tid)  # Genuine subsequent native claim, not a surface worker launch.
    after = get_recovery_state(board, tid)
    identity = first["active_blocker_id"]
    assert after["active_blocker_id"] == identity
    assert after["blockers"][identity]["count"] == 1
    assert after["blockers"][identity]["cycle"] == first["blockers"][identity]["cycle"] + 1


@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_release_validation_and_read_only_template(board, capsys, monkeypatch, surface):
    tid = kb.create_task(board, title="held", assignee="worker")
    block(board, tid)
    # Portable validation does not need Linux probes to reject malformed payloads.
    v = get_recovery_state(board, tid)
    base = dict(action="resolved_resume", observed_token=v["observed_token"],
                block_event_id=v["active_event_id"], blocker_id=v["active_blocker_id"],
                rationale="test", evidence_refs=["test:repair"], initiation_mode="user_directed",
                settlement_refs=[], instruction_ref="test:instruction")
    invalid = [None, [], {}, {**base, "action": "unsupported"}, {**base, "action": []},
               {**base, "author": "owner"}, {**base, "profile": "owner"},
               {**base, "block_event_id": True}, {**base, "instruction_ref": []},
               {**base, "initiation_mode": {}}, {**base, "settlement_refs": "junk"},
               {**base, "existing_blocker_id": "forbidden"}, {**base, "instruction_ref": None}]
    for payload in invalid:
        before = list(board.iterdump())
        assert not release(capsys, surface, tid, payload).get("ok")
        assert list(board.iterdump()) == before
    from hermes_cli import kanban_db_recovery_release as native
    def unavailable(*args):
        raise OSError("test identity probe unavailable")
    monkeypatch.setattr(native, "settlement_scope", unavailable)
    before = list(board.iterdump())
    shown = show(capsys, surface, tid)
    assert shown["recovery"] == v
    assert shown["settlement_scope"]["available"] is False
    assert shown["settlement_scope"]["reason"]
    assert "template" not in shown["settlement_scope"]
    assert list(board.iterdump()) == before


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_scope_staleness_authority_and_parent_hold(board, capsys, monkeypatch, surface):
    from agent.delegation_context import delegated_child_context
    tid = kb.create_task(board, title="held", assignee="worker")
    block(board, tid)
    payload = request(show(capsys, surface, tid))
    for field, value in [("scopes", []), ("task_id", "wrong"), ("observed_token", "stale"), ("run_ids", [])]:
        bad = copy.deepcopy(payload)
        bad["settlement_refs"][0][field] = value
        before = list(board.iterdump())
        assert not release(capsys, surface, tid, bad)["ok"]
        assert list(board.iterdump()) == before
    bad = copy.deepcopy(payload)
    bad["settlement_refs"] = [show(capsys, surface, tid)["settlement_scope"]["template"]]
    before = list(board.iterdump())
    assert not release(capsys, surface, tid, bad)["ok"]
    assert list(board.iterdump()) == before
    for marker in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT"):
        with monkeypatch.context() as context:
            context.setenv(marker, tid)
            assert show(capsys, surface, tid)["settlement_scope"]["read_only"]
            assert not release(capsys, surface, tid, payload).get("ok")
            assert list(board.iterdump()) == before
    with delegated_child_context("child"):
        assert show(capsys, surface, tid)["settlement_scope"]["read_only"]
        assert not release(capsys, surface, tid, payload).get("ok")
        assert list(board.iterdump()) == before
    kb.add_comment(board, tid, author="owner", body="invalidate observation")
    before = list(board.iterdump())
    assert not release(capsys, surface, tid, payload)["ok"]
    assert list(board.iterdump()) == before
    parent = kb.create_task(board, title="parent", assignee="worker")
    kb.link_tasks(board, parent, tid)
    result = release(capsys, surface, tid, request(show(capsys, surface, tid)))
    assert result["released"] and not result["eligible"] and not result["held"]
    assert result["status"] == kb.get_task(board, tid).status == "todo"


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_legacy_same_identity_through_surfaces(tmp_path, monkeypatch, capsys, surface):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets: [kanban]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript((Path(__file__).parent / "fixtures/recovery/legacy_triage.sql").read_text())
    # Native board override shared by the real CLI and registered handler.
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    with kbc.connect_closing(path) as conn:
        tid = conn.execute("SELECT id FROM tasks").fetchone()[0]
        shown = show(capsys, surface, tid)
        assert shown["recovery"]["legacy"]
        identity = shown["recovery"]["active_blocker_id"]
        result = release(capsys, surface, tid, request(shown))
        assert result["released"], result
        assert result["state"]["active_blocker_id"] == identity
        block(conn, tid)
        state = get_recovery_state(conn, tid)
        assert state["active_blocker_id"] == identity
        assert state["blockers"][identity]["count"] == 1
