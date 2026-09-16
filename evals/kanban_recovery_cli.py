"""Offline candidate CLI qualification. No worker/gateway/dispatcher is launched.

Run with candidate .venv/bin/python evals/kanban_recovery_cli.py
--expected-head <literal-40-hex-commit> refusal|owner.
Owner requires genuinely authorized context, never scrubbed authority markers.
This fleet-specific evaluation deliberately pins its common Git directory.
`tests FILE...` runs only explicit files via the canonical runner. Scratch roots
are private /tmp/kq-* and retained as evidence; all subprocesses are waited.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv/bin/python"

EXPECTED_COMMON = Path("/root/.hermes/workspace/hermes-agent-maintenance/.git")


def authority(env):
    return {k: v for k, v in env.items() if k.startswith(("HERMES_DELEGATED", "HERMES_KANBAN"))}


def expected_head(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("expected HEAD must be a literal 40-character lowercase hex SHA; no normalization")
    return value


def check_repo(head):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()
    actual = git("rev-parse", "HEAD")
    if actual != head:
        raise ValueError(f"unexpected HEAD: expected {head}, actual {actual}")
    common = (REPO / git("rev-parse", "--git-common-dir")).resolve()
    assert common == EXPECTED_COMMON, "unexpected repository"
    assert not (REPO / ".env").exists(), "candidate dotenv would defeat credential isolation"
    assert tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]["hermes"] == "hermes_cli.main:main"
    assert PYTHON.is_file(), "candidate venv missing; no fallback"
    assert Path(sys.prefix).resolve() == (REPO / ".venv").resolve(), "candidate interpreter required"


def hashes():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO).decode().split("\0")
    paths += ["evals/kanban_recovery_cli.py", "tests/agent/test_kanban_recovery_provider_schemas.py",
              "tests/evals/test_kanban_recovery_cli.py"]
    return {p: hashlib.sha256((REPO / p).read_bytes()).hexdigest()
            for p in sorted(set(paths)) if p and (REPO / p).is_file()}


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def lifecycle(root, mode):
    # Imports happen only after the launcher has isolated HOME and credentials.
    import pytest
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from tests.hermes_cli.test_kanban_pr_acceptance import github, PR
    from tests.hermes_cli.test_kanban_recovery_surfaces import block, request as classify
    from tests.hermes_cli.test_kanban_recovery_release_surfaces import request
    assert Path(kb.__file__).resolve().is_relative_to(REPO)
    assert Path(sys.prefix).resolve() == (REPO / ".venv").resolve()
    write(root / "distributions.json", sorted([
        {"name": d.metadata["Name"], "version": d.version, "location": str(d.locate_file(""))}
        for d in importlib.metadata.distributions()], key=lambda d: d["name"].lower()))
    records = []
    def cli(*words, extra=None):
        env = dict(os.environ)
        if extra:
            assert not (set(extra) & set(authority(env))), "cannot replace inherited authority"
            env.update(extra)
        command = [str(PYTHON), "-m", "hermes_cli.main", "kanban", *words]
        child = subprocess.run(command, cwd=REPO, env=env, text=True, capture_output=True, timeout=35)
        records.append({"argv": command, "exit": child.returncode, "stdout": child.stdout, "stderr": child.stderr,
                        "authority_keys": sorted(authority(env))})
        write(root / "cli.json", records)
        return child
    def show(tid):
        out = cli("show", tid, "--json")
        assert out.returncode == 0, out.stderr + out.stdout
        return json.loads(out.stdout)
    def recover(tid, payload, ok=True):
        result = cli("unblock", tid, "--recovery-json", json.dumps(payload))
        assert (result.returncode == 0) is ok, result.stdout + result.stderr
        return json.loads(result.stdout) if ok else result
    if mode == "refusal":
        assert authority(os.environ), "refusal mode requires inherited authority"
        # A delegated process may not even initialize native recovery storage.
        # Do not fabricate an owner by clearing markers to prepare a board.
        result = cli("init")
        assert result.returncode != 0, result.stdout + result.stderr
        assert "delegate_task child contexts cannot mutate" in result.stdout + result.stderr
        for action in ("classify", "retry", "resolved_resume", "finalize"):
            denied = cli("unblock", "qualification-uncreated", "--recovery-json", json.dumps({"action": action}))
            assert denied.returncode == 1
            assert "delegate_task child contexts cannot mutate" in denied.stdout + denied.stderr
        assert not list((root / "home").glob("*.db"))
        return {"delegated_cli_entry_refused": True,
                "recovery_action_native_refusal": "UNEXECUTED: CLI authority fence precedes parsing/native board",
                "owner_positive": "BLOCKED: parent context required"}
    patch = pytest.MonkeyPatch()
    fixture = github.__wrapped__(root, patch)
    state = next(fixture)
    try:
        with connect_closing() as conn:
            assert Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve().is_relative_to(root)
            tid = kb.create_task(conn, title="candidate executable recovery", assignee="worker", completion_contract=PR)
            block(conn, tid)
            shown = show(tid)
            before = list(conn.iterdump())
            # Both malformed transport and inherited authority denial leave exact state.
            recover(tid, None, False)
            assert list(conn.iterdump()) == before
            runs = kb.list_runs(conn, tid)
            denied = recover(tid, classify(shown["recovery"], existing_blocker_id=shown["recovery"]["active_blocker_id"]), False)
            assert "report already attributed to this blocker" in denied.stdout + denied.stderr
            assert list(conn.iterdump()) == before
            result = recover(tid, classify(shown["recovery"]))
            assert result["classified"] and not result["released"]
            assert kb.list_runs(conn, tid) == runs
            first = show(tid)["recovery"]
            assert recover(tid, request(show(tid), action="retry"))["status"] == "ready"
            block(conn, tid)
            assert kb.get_task(conn, tid).status == "triage"
            payload = request(show(tid), action="retry")
            before = list(conn.iterdump())
            recover(tid, payload, False)
            assert list(conn.iterdump()) == before
            payload["action"] = "resolved_resume"
            assert recover(tid, payload)["status"] == "ready"
            before = list(conn.iterdump())
            recover(tid, payload, False)
            assert list(conn.iterdump()) == before
            block(conn, tid)
            after = show(tid)["recovery"]
            identity = after["active_blocker_id"]
            assert after["blockers"][identity]["count"] == 1
            assert after["blockers"][identity]["cycle"] == first["blockers"][identity]["cycle"] + 1
            parent = kb.create_task(conn, title="unfinished dependency", assignee="worker")
            kb.link_tasks(conn, parent, tid)
            runs = kb.list_runs(conn, tid)
            before = list(conn.iterdump())
            denied = recover(tid, request(show(tid)), False)
            assert "resolution evidence already consumed" in denied.stdout + denied.stderr
            assert list(conn.iterdump()) == before
            # This below-limit scheduling probe is a retry, not a second repair.
            assert recover(tid, request(show(tid), action="retry"))["status"] == "todo"
            assert kb.list_runs(conn, tid) == runs and kb.get_task(conn, tid).current_run_id is None
            final = kb.create_task(conn, title="exact bound tracked finalization", assignee="worker", completion_contract=PR)
            block(conn, final)
            assert cli("unblock", final).returncode == 0
            block(conn, final)
            assert kb.get_task(conn, final).status == "triage"
            final_payload = request(show(final), action="finalize")
            for marker in ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT"):
                before = list(conn.iterdump())
                denied = cli("unblock", final, "--recovery-json", json.dumps(final_payload), extra={marker: "qualification"})
                assert denied.returncode != 0 and list(conn.iterdump()) == before
            # The native engine, not the transport fixture, decides completion.
            conn.execute("CREATE TRIGGER no_ready BEFORE UPDATE OF status ON tasks WHEN OLD.id='" + final + "' AND NEW.status NOT IN ('triage','done') BEGIN SELECT RAISE(ABORT,'status cycling'); END")
            runs = kb.list_runs(conn, final)
            result = recover(final, final_payload)
            assert result["completed"] and result["status"] == "done" and "released" not in result
            assert kb.list_runs(conn, final) == runs and kb.get_task(conn, final).current_run_id is None
            events = kb.list_events(conn, final)
            kinds = [e.kind for e in events]
            assert all(kinds.count(k) == 1 for k in ("completed", "blocker_cycle_closed", "triage_finalized", "pr_acceptance"))
            receipt = next(e.payload for e in events if e.kind == "pr_acceptance")
            assert receipt["ok"] and receipt["acceptance_basis"] == "reported-ci" and receipt["pr_url"] == PR
            assert any(c["classification"] == "success" for c in receipt["checks"])
            assert all(c["classification"] in ("success", "non_blocking") for c in receipt["checks"])
            before = list(conn.iterdump())
            recover(final, final_payload, False)
            assert list(conn.iterdump()) == before
            write(root / "native-receipt.json", receipt)
            (root / "native.sql").write_text("\n".join(conn.iterdump()))
            return {"owner_positive": True, "http_requests": state["requests"], "native_events": kinds}
    finally:
        fixture.close()  # joins the exact loopback server thread
        patch.undo()
        write(root / "transport.json", state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("refusal", "owner", "tests"))
    parser.add_argument("files", nargs="*")
    parser.add_argument("--expected-head", required=True, type=expected_head)
    parser.add_argument("--child-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mode == "tests":
        if not args.files or any(not f.startswith("tests/") or not f.endswith(".py") or not (REPO / f).is_file() or ".." in Path(f).parts for f in args.files):
            parser.error("tests mode requires explicit existing test .py files; no implicit suite")
    elif args.files:
        parser.error("lifecycle modes do not accept files")
    markers = authority(os.environ)
    if args.mode == "owner" and markers:
        parser.error("owner proof blocked by inherited authority; parent must launch without modifying its own markers")
    if args.mode != "tests" and any(k.startswith("HERMES_KANBAN") for k in markers):
        parser.error("inherited Kanban routing pins cannot be safely redirected or cleared")
    # Portable refusals grant no execution authority; accepted paths retain every pin.
    try:
        check_repo(args.expected_head)
    except (ValueError, AssertionError) as exc:
        parser.error(str(exc))
    if args.child_root:
        root = args.child_root.resolve()
        assert root.parent == Path("/tmp") and root.name.startswith("kq-")
        assert os.environ["HOME"] == str(root / "user")
        result = lifecycle(root, args.mode)
        write(root / "result.json", result)
        print(json.dumps(result))
        return 0
    root = Path(tempfile.mkdtemp(prefix="kq-", dir="/tmp"))
    os.chmod(root, 0o700)
    for name in ("user", "home", "tmp"):
        (root / name).mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root / "user"), "HERMES_HOME": str(root / "home"),
           "TMPDIR": str(root / "tmp"), "TMP": str(root / "tmp"), "TEMP": str(root / "tmp"),
           "LANG": "C.UTF-8", "TZ": "UTC", "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1",
           "HERMES_DISABLE_LAZY_INSTALL": "1", **markers}
    before = hashes()
    distributions = lambda: sorted((d.metadata["Name"], d.version, str(d.locate_file("")))
                                    for d in importlib.metadata.distributions())
    packages = distributions()
    write(root / "distributions-before.json", packages)
    write(root / "source-before.json", before)
    command = (["bash", "scripts/run_tests.sh", "-j", "2", "--file-retries", "0", *sorted(set(args.files))]
               if args.mode == "tests" else [str(PYTHON), str(Path(__file__).resolve()), "--expected-head", args.expected_head, args.mode, "--child-root", str(root)])
    write(root / "command.json", {"argv": command, "expected_head": args.expected_head, "authority_keys": sorted(markers)})
    print(str(root), flush=True)
    with (root / "output.log").open("w") as log:
        child = subprocess.run(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=300)
    write(root / "distributions-after.json", distributions())
    write(root / "exit.json", {"child_exit": child.returncode, "source_unchanged": before == hashes(), "distributions_unchanged": packages == distributions(), "distribution_count": len(packages)})
    write(root / "source-after.json", hashes())
    print(json.dumps({"evidence": str(root), "child_exit": child.returncode}))
    return child.returncode


if __name__ == "__main__":
    sys.exit(main())
