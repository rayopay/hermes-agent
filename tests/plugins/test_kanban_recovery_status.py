"""Dashboard final event must describe the committed native recovery state."""
import importlib.util
from pathlib import Path
import sys

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.mark.parametrize("scenario", ["hold", "ordinary", "review"])
def test_dashboard_reports_final_status_and_native_unblock(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("recovery_status_dashboard", path)
    api = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, api)
    spec.loader.exec_module(api)
    terminations = []
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *args: terminations.append(args))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Dashboard recovery", assignee="worker")
        task = kb.claim_task(conn, tid)
        if scenario == "review":
            assert kb.request_review(conn, tid, reviewer="reviewer", expected_run_id=task.current_run_id)
            task = kb.claim_review_task(conn, tid)
        if scenario != "ordinary":
            kb.add_comment(conn, tid, "worker", "https://github.com/disposable/test/pull/1")
        assert api._set_status_direct(conn, tid, "ready")
        expected = {"hold": "blocked", "ordinary": "ready", "review": "review"}[scenario]
        assert kb.get_task(conn, tid).status == expected
        event = [e for e in kb.list_events(conn, tid) if e.kind == "status"][-1]
        assert event.payload == {"status": expected, "requested_status": "ready"}
        assert event.run_id == task.current_run_id
        assert terminations == [(task.worker_pid, task.claim_lock)]
        if scenario == "hold":
            assert kb.unblock_task(conn, tid)
            assert kb.get_task(conn, tid).status == "ready"
            assert kb.claim_task(conn, tid) is not None
