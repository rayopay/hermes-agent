"""Fail-closed settlement shared by release and direct PR finalization."""
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_recovery_release as release
from hermes_cli.kanban_db_recovery_finalize import finalize_task
from tests.hermes_cli.test_kanban_recovery_finalize import held, transport
from tests.hermes_cli.test_kanban_recovery_release import board, request


def invoke(conn, tid, consumer):
    args = request(conn, tid)
    if consumer == "finalize":
        args.pop("action")
        return finalize_task(conn, tid, **args)
    return release.recover_task(conn, tid, **args)


@pytest.mark.linux_only
@pytest.mark.parametrize("consumer", ["release", "finalize"])
@pytest.mark.parametrize("case", [
    "live", "absent", "access_denied", "psutil_error", "zombie_error",
    "no_such_process_error", "permission", "os_error", "value_error",
    "type_error", "none_result", "zero_result", "missing_boot",
    "bool_pid", "zero_pid", "negative_pid", "string_pid", "float_pid", "null_pid",
])
def test_only_authoritative_absence_allows_settlement(board, transport, monkeypatch, tmp_path, consumer, case):
    tid, _ = held(board)
    invalid = {"bool_pid": True, "zero_pid": 0, "negative_pid": -1,
               "string_pid": "123", "float_pid": 123.0, "null_pid": None}
    pid = invalid.get(case, 123)
    kb._append_event(board, tid, "spawned", {"pid": pid})
    args = request(board, tid)
    if consumer == "finalize":
        args.pop("action")
    errors = {"access_denied": psutil.AccessDenied(pid), "psutil_error": psutil.Error("uncertain"),
              "zombie_error": psutil.ZombieProcess(pid), "no_such_process_error": psutil.NoSuchProcess(pid),
              "permission": PermissionError("uncertain"), "os_error": OSError("uncertain"),
              "value_error": ValueError("uncertain"), "type_error": TypeError("uncertain")}
    calls = []
    def probe(value):
        calls.append(value)
        assert type(value) is int and value > 0
        if case in errors:
            raise errors[case]
        return {"absent": False, "none_result": None, "zero_result": 0}.get(case, True)
    monkeypatch.setattr(psutil, "pid_exists", probe)
    # With the library seam controlled, any direct signal probe is a defect.
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("direct os.kill probe"))
    if case == "missing_boot":
        original = Path.open
        missing = tmp_path / "absent-boot-id"
        def boot_open(path, *a, **kw):
            return original(missing if str(path) == "/proc/sys/kernel/random/boot_id" else path, *a, **kw)
        monkeypatch.setattr(Path, "open", boot_open)
    before = list(board.iterdump())
    result = (finalize_task if consumer == "finalize" else release.recover_task)(board, tid, **args)
    assert calls == ([] if case in invalid or case == "missing_boot" else [pid] * len(calls))
    if case not in invalid and case != "missing_boot":
        assert calls
    if case == "absent":
        assert result["ok"] and not result["held"]
        assert kb.get_task(board, tid).status == ("done" if consumer == "finalize" else "ready")
    else:
        assert not result["ok"] and result["held"]
        assert "settlement unresolved" in result["reason"]
        assert list(board.iterdump()) == before
        assert not transport["calls"]


@pytest.mark.linux_only
@pytest.mark.parametrize("consumer", ["release", "finalize"])
def test_owned_process_live_zombie_then_reaped(board, transport, tmp_path, consumer):
    tid, _ = held(board)
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE)
    receipt = {"pid": proc.pid, "consumer": consumer}
    try:
        kb._append_event(board, tid, "spawned", {"pid": proc.pid})
        before = list(board.iterdump())
        assert proc.poll() is None
        assert not invoke(board, tid, consumer)["ok"]
        assert list(board.iterdump()) == before and not transport["calls"]
        assert proc.poll() is None
        receipt["live_held"] = True
        proc.stdin.close()
        proc.stdin = None
        # Wait for exit without reaping: a zombie is still an existing PID.
        exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        assert exited.si_pid == proc.pid
        assert psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE
        assert not invoke(board, tid, consumer)["ok"]
        assert list(board.iterdump()) == before and not transport["calls"]
        receipt["zombie_held"] = True
        assert proc.wait(timeout=10) == 0
        assert not psutil.pid_exists(proc.pid)
        assert invoke(board, tid, consumer)["ok"]
        receipt["reaped_absent_accepted"] = True
    finally:
        proc.communicate(timeout=10)
        receipt["returncode"] = proc.returncode
        (tmp_path / "owned-process-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
