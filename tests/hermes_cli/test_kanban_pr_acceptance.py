"""Reported-CI invariants through native SQLite and paginated local HTTP/gh.

The shim implements only read requests and Link pagination, not acceptance logic.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect

SHA = "a" * 40
PR = "https://github.com/acme/repo/pull/7"


def check_run(conclusion="success", **overrides):
    return {"id": 42, "name": "required", "head_sha": SHA,
            "app": {"id": 1}, "check_suite": {"id": 10},
            "status": "completed", "conclusion": conclusion,
            "html_url": "https://github.com/acme/repo/actions/runs/42", **overrides}


def status(state="success", **overrides):
    return {"id": 12, "context": "legacy", "state": state,
            "target_url": "https://ci.example.test/12", **overrides}


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"head": SHA, "base": "main", "pr_state": "open", "merged": False,
             "reads": 0, "requests": [], "commands": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            state["commands"].append(json.loads(self.headers["X-Arguments"]))
            parts = urlsplit(self.path)
            page = int(parse_qs(parts.query).get("page", ["1"])[0])
            failure = state.get("api_failure")
            if failure and failure in self.path:
                self.send_error(403, "synthetic-private-stderr")
                return
            if parts.path == "/graphql":
                # Baseline compatibility only: candidate must never request policy.
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": state["head"], "baseRefName": state["base"], "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in parts.path:
                value = []
            elif "/check-runs" in parts.path:
                runs = state.get("runs", [check_run(state.get("conclusion", "success"))])
                pages = state.get("run_pages", [runs])
                value = {"total_count": state.get("total_count", sum(map(len, pages))),
                         "check_runs": pages[page - 1]}
                if state.get("race") and page == 1:
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
                if state.get("base_change"):
                    state["base"] = "release"
                if state.get("close"):
                    state["pr_state"] = "closed"
            elif "/statuses" in parts.path:
                pages = state.get("status_pages", [state.get("statuses", [])])
                value = pages[page - 1]
            elif "/pulls/" in parts.path:
                state["reads"] += 1
                value = {"head": {"sha": state["head"]}, "base": {"ref": state["base"]},
                         "state": state["pr_state"], "merged": state["merged"]}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            if ("/check-runs" in parts.path or "/statuses" in parts.path) and page < len(pages):
                self.send_header("Link", f'<http://127.0.0.1:{self.server.server_port}'
                                 f'{parts.path}?per_page=100&page={page + 1}>; rel="next"')
            self.end_headers()
            data = "not-json" if state.get("malformed") in (parts.path,) else json.dumps(value)
            self.wfile.write(data.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\n"
                  "import json,sys,urllib.request\n"
                  "assert sys.argv[1] == 'api'\n"
                  "assert '--method' not in sys.argv or sys.argv[sys.argv.index('--method')+1] == 'GET'\n"
                  f"url='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "pages=[]\n"
                  "while url:\n"
                  " request=urllib.request.Request(url,headers={'X-Arguments':json.dumps(sys.argv[1:])})\n"
                  " with urllib.request.urlopen(request) as response:\n"
                  "  pages.append(json.loads(response.read()))\n"
                  "  link=response.headers.get('Link','')\n"
                  " url=link.split('>')[0][1:] if link and '--paginate' in sys.argv else None\n"
                  "print(json.dumps(pages if '--slurp' in sys.argv else pages[0]))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


CASES = [
    ("success-without-policy", {"api_failure": "graphql"}, True),
    ("rules-unavailable", {"api_failure": "/rules/"}, True),
    ("zero", {"runs": []}, False),
    ("legacy-only", {"runs": [], "statuses": [status()]}, True),
    ("mixed-neutral", {"runs": [check_run(), check_run("neutral", id=43, name="optional")]}, True),
    ("mixed-skipped", {"runs": [check_run(), check_run("skipped", id=43, name="optional")]}, True),
    ("legacy-plus-skipped", {"runs": [check_run("skipped")], "statuses": [status()]}, True),
    ("all-neutral-skipped", {"runs": [check_run("neutral"), check_run("skipped", id=43, name="other")]}, False),
    ("old-status-failure", {"runs": [], "statuses": [status("failure", id=1), status(id=2)]}, True),
    ("new-status-failure", {"statuses": [status(id=1), status("failure", id=2)]}, False),
    ("latest-status-per-context", {"statuses": [status("failure", context="other"), status(id=13)]}, False),
    ("rerun-success", {"runs": [check_run("failure", id=41), check_run()]}, True),
    ("rerun-failure", {"runs": [check_run(id=41), check_run("failure")]}, False),
    ("different-app", {"runs": [check_run("failure", app={"id": 2}), check_run(id=43)]}, False),
    ("different-suite", {"runs": [check_run("failure", check_suite={"id": 11}), check_run(id=43)]}, False),
    ("status-does-not-mask-run", {"runs": [check_run("failure")], "statuses": [status(context="required")]}, False),
    ("run-does-not-mask-status", {"statuses": [status("failure", context="required")]}, False),
    ("wrong-sha", {"runs": [check_run(head_sha="b" * 40)]}, False),
    ("wrong-status-sha", {"statuses": [status(sha="b" * 40)]}, False),
    ("head-move", {"head_change": True}, False),
    ("base-move", {"base_change": True}, False),
    ("closed-during-read", {"close": True}, False),
    ("already-closed", {"pr_state": "closed"}, False),
    ("merged", {"pr_state": "closed", "merged": True}, True),
    ("malformed-head", {"head": "not-a-sha"}, False),
    ("missing-run-page", {"total_count": 2}, False),
    ("check-api-error", {"api_failure": "/check-runs"}, False),
    ("status-api-error", {"api_failure": "/statuses"}, False),
    ("pr-api-error", {"api_failure": "/pulls/"}, False),
    ("malformed-response", {"malformed": f"/repos/acme/repo/commits/{SHA}/check-runs"}, False),
    ("paginated-runs", {"run_pages": [[check_run("skipped", id=i + 1, name=f"skip-{i}") for i in range(100)], [check_run(id=200)]]}, True),
    ("paginated-run-failure", {"run_pages": [[check_run()], [check_run("failure", id=43, name="optional")]]}, False),
    ("paginated-status-history", {"runs": [], "status_pages": [[status(id=200)], [status("failure", id=1)]]}, True),
    ("paginated-status-failure", {"status_pages": [[status()], [status("failure", id=13, context="other")]]}, False),
]
CASES += [(f"run-{outcome}", {"runs": [check_run(outcome)]}, outcome == "success")
          for outcome in ("success", "failure", "cancelled", "timed_out", "action_required", "stale", "pending", "neutral", "skipped", None, "unknown")]
CASES += [(f"optional-{outcome}", {"runs": [check_run(), check_run(outcome, id=43, name="optional")]}, False)
          for outcome in ("failure", "cancelled", "timed_out", "action_required", None, "unknown")]
CASES += [(f"incomplete-{phase}", {"runs": [check_run(status=phase)]}, False)
          for phase in ("queued", "in_progress", "waiting", "pending", "requested", "unknown", None)]
CASES += [(f"status-{outcome}", {"statuses": [status(outcome)]}, False)
          for outcome in ("failure", "error", "pending", "unknown", None)]
# Equal IDs are not status history: overlapping or conflicting pages cannot prove
# a complete, stable snapshot, regardless of which result happens to arrive first.
CASES += [(f"duplicate-status-{order}-{layout}",
           {"status_pages": [[status(order[0])], [status(order[1])]] if layout == "pages"
            else [[status(order[0]), status(order[1])]]}, False)
          for order in (("success", "failure"), ("failure", "success"), ("success", "success"))
          for layout in ("pages", "one-page")]
CASES += [(f"invalid-{field}-{value}",
           {"runs": [check_run(**{field: {"id": value} if field in {"app", "check_suite"} else value})]}, False)
          for field in ("id", "app", "check_suite") for value in (0, -1, True, "42")]
CASES += [(f"invalid-status-id-{value}", {"statuses": [status(id=value)]}, False)
          for value in (0, -1, True, "12")]
CASES += [("inconsistent-open-merged", {"pr_state": "open", "merged": True}, False),
          ("non-boolean-merged", {"merged": 1}, False)]


@pytest.mark.linux_only
@pytest.mark.parametrize("name,changes,accepted", CASES, ids=[case[0] for case in CASES])
def test_reported_ci_completion(github, name, changes, accepted):
    github.update(changes)
    with connect() as conn:
        tid = kb.create_task(conn, title=name, completion_contract="acme/repo")
        ok = kb.complete_task(conn, tid, metadata={"published_pr": PR})
        assert ok is accepted
        task = kb.get_task(conn, tid)
        assert (task.status == "done") is accepted
        assert task.completion_contract == PR
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["ok"] is accepted
        assert receipt["acceptance_basis"] == "reported-ci"
        assert receipt["pr_url"] == PR
        assert not any("graphql" in r or "/rules/" in r or "/protection" in r for r in github["requests"])
        assert "synthetic-private-stderr" not in json.dumps(receipt)
        if accepted:
            assert receipt["head_sha"] == SHA and receipt["base_ref"] == "main"
            assert any(c["classification"] == "success" for c in receipt["checks"])
            assert all(c["classification"] in {"success", "non_blocking"} for c in receipt["checks"])
            assert github["reads"] == 2
            for check in receipt["checks"]:
                assert check["head_sha"] == SHA and check["id"] is not None
                assert check["type"] in {"check_run", "status"}
                if check["type"] == "check_run":
                    assert check["app_id"] == 1 and check["check_suite_id"] == 10
        else:
            assert task.status in {"running", "ready", "blocked", "review"}
            assert "retry" in receipt["recovery"]
        for command in github["commands"]:
            if "/check-runs" in command[1] or "/statuses" in command[1]:
                assert "--paginate" in command and "--slurp" in command
                assert f"/commits/{changes.get('head', SHA)}/" in command[1]


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id

            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id

            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id, metadata={"published_pr": PR})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")
            before = len(github["requests"])
            assert not kb.complete_task(conn, tid, expected_run_id=run_id, metadata={"published_pr": PR})
            assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_publication_binding_validation_and_local_only(github):
    with connect() as conn:
        for invalid in ("acme", "acme/repo/extra", PR + "/", PR + "?x=1", "https://github.com/acme/repo/pull/0"):
            with pytest.raises(ValueError):
                kb.create_task(conn, title="invalid", completion_contract=invalid)
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        assert github["requests"] == []
        github["conclusion"] = "failure"
        assert not kb.complete_task(conn, tid, metadata={"published_pr": PR})
        assert kb.get_task(conn, tid).completion_contract == PR
        before = len(github["requests"])
        github["conclusion"] = "success"
        assert not kb.complete_task(conn, tid, metadata={"published_pr": PR.replace("/7", "/8")})
        assert len(github["requests"]) == before
        assert kb.complete_task(conn, tid, metadata={"published_pr": PR})
        before = len(github["requests"])
        for contract in (None, "local-only"):
            local = kb.create_task(conn, title="local", completion_contract=contract)
            assert kb.complete_task(conn, local, summary=PR, metadata={"published_pr": PR})
        assert len(github["requests"]) == before


@pytest.mark.linux_only
@pytest.mark.parametrize("surface", ["tool", "cli", "review", "dashboard"])
@pytest.mark.parametrize("optional_outcome", ["failure", "neutral"])
def test_completion_surfaces_share_reported_ci(github, surface, optional_outcome, monkeypatch):
    github["runs"] = [check_run(), check_run(optional_outcome, id=43, name="optional")]
    accepted = optional_outcome == "neutral"
    with connect() as conn:
        tid = kb.create_task(conn, title=surface, completion_contract=PR)
        if surface == "review":
            owner = kb.claim_task(conn, tid)
            assert kb.request_review(conn, tid, summary="Ready for review", expected_run_id=owner.current_run_id)
            reviewer = kb.claim_review_task(conn, tid)
            assert kb.complete_task(conn, tid, summary="Reviewed", expected_run_id=reviewer.current_run_id) is accepted
    if surface == "tool":
        import tools.kanban_tools  # noqa: F401 -- registers the real tool handler
        from tools.registry import registry
        response = json.loads(registry.dispatch("kanban_complete", {"task_id": tid, "summary": "Completed"}))
        assert bool(response.get("error")) is not accepted
    elif surface == "cli":
        import argparse
        from hermes_cli.kanban import kanban_command
        from hermes_cli.kanban_parser import build_parser
        parser = argparse.ArgumentParser()
        build_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["kanban", "complete", tid, "--summary", "Completed"])
        assert (kanban_command(args) == 0) is accepted
    elif surface == "dashboard":
        import importlib.util
        from pathlib import Path
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
        spec = importlib.util.spec_from_file_location("pr_acceptance_dashboard_test", path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix="/api/plugins/kanban")
        with TestClient(app) as client:
            response = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "done", "summary": "Completed"})
            assert response.status_code == (200 if accepted else 409), response.text
    with connect() as conn:
        assert (kb.get_task(conn, tid).status == "done") is accepted
        receipts = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchall()
        assert len(receipts) == 1
        receipt = json.loads(receipts[0][0])
        assert receipt["ok"] is accepted
        assert {c["id"] for c in receipt["checks"]} == {42, 43}
        assert receipt["acceptance_basis"] == "reported-ci"
