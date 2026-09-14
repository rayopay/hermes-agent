"""Native SQLite/dispatch recovery contracts; spawn and signals are inert.

A comment is a possible publication reference, not proof of a PR or its owner.
No provider, LLM worker, or remote publication is exercised here.
"""
from dataclasses import asdict
import importlib.util
import json
import logging
from pathlib import Path
import sys
import time

import pytest

from hermes_cli import config, profiles
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PID = 2147483647
PR = "Possible reference: https://github.com/disposable/recovery-test/pull/1"


@pytest.fixture
def board(tmp_path, monkeypatch, request):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "load_config_readonly", lambda: {})
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"worker", "reviewer"})
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    # Inject only the OS signalling boundary: never signal a real process.
    signals = []
    def signal(pid, sig):
        assert pid == PID
        signals.append((pid, int(sig)))
        raise ProcessLookupError(pid)
    monkeypatch.setattr(kbd, "_kill_fn", lambda fn=None: fn or signal)
    conn = kbc.connect()
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    spawns, ticks = [], []
    def spawn(task, workspace, *, board=None):
        assert Path(workspace).resolve().is_relative_to(home.resolve())
        with kbc.connect() as other:
            committed = kb.get_task(other, task.id)
            assert committed.current_run_id == task.current_run_id
            assert committed.claim_lock == task.claim_lock
            assert committed.status == "running"
            assert kb.claim_task(other, task.id) is None
        spawns.append((task.id, task.current_run_id, task.assignee))
        return PID
    state = dict(conn=conn, spawn=spawn, spawns=spawns, ticks=ticks, signals=signals)
    try:
        yield state
    finally:
        receipt = {"test": request.node.nodeid, "db_path": str(db_path),
                   "spawns": spawns, "ticks": ticks, "inert_signals": signals}
        for table in ("tasks", "task_runs", "task_comments", "task_events"):
            receipt[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        receipt["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2))
        logging.getLogger(__name__).warning("RECOVERY_RECEIPT %s", json.dumps(receipt))
        conn.close()
        kbd._recent_worker_exits.pop(PID, None)


def _tick(board):
    result = kbd.dispatch_once(board["conn"], spawn_fn=board["spawn"], max_spawn=1)
    board["ticks"].append(asdict(result))
    assert not result.skipped_locked
    assert result.memory_pressure != "critical"
    return result


def _new(board, *, claim=True, old=False, max_retries=None):
    conn = board["conn"]
    tid = kb.create_task(conn, title="Recovery contract", assignee="worker",
                         workspace_kind="scratch", max_retries=max_retries)
    if old:
        kb.add_comment(conn, tid, "owner", PR)
    if not claim:
        return tid, None
    task = kb.claim_task(conn, tid)
    assert task is not None
    kbd._set_worker_pid(conn, tid, PID)
    return tid, task.current_run_id


def _holds(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "blocked"
            and e.payload.get("reason_code") == "interrupted_implementation"]


def _age(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ?, claim_expires = ?, last_heartbeat_at = NULL WHERE id = ?",
                     (int(time.time()) - 10000, int(time.time()) - 10, tid))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE task_id = ? AND ended_at IS NULL",
                     (int(time.time()) - 10000, tid))


@pytest.mark.parametrize("route", ["clean", "nonzero", "ttl", "timeout", "stale", "manual", "orphan", "budget"])
@pytest.mark.parametrize("reference", ["new", "old", "none"])
def test_only_interrupted_attempt_evidence_requires_owner_reconciliation(board, monkeypatch, route, reference):
    conn = board["conn"]
    tid, old_run = _new(board, old=reference == "old")
    if reference == "new":
        comment_id = kb.add_comment(conn, tid, "worker", PR)
    if route in {"ttl", "timeout", "stale"}:
        _age(conn, tid)
    if route in {"clean", "nonzero"}:
        # Real exit-classification bookkeeping, not a replacement reclaimer.
        kbd._record_worker_exit(PID, 0 if route == "clean" else 256)
        assert _tick(board).crashed == [tid]
    elif route == "ttl":
        assert _tick(board).reclaimed == 1
    elif route == "timeout":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = ?", (tid,))
        assert kbd.enforce_max_runtime(conn) == [tid]
    elif route == "stale":
        assert kbd.detect_stale_running(conn, stale_timeout_seconds=1) == [tid]
    elif route == "manual":
        assert kb.reclaim_task(conn, tid, reason="owner stopped unclear attempt")
    elif route == "orphan":
        # Explicit corruption fixture: retain the exact run, lose claim expiry.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = NULL WHERE id = ?", (tid,))
        assert kbd.reconcile_orphaned_running(conn) == [tid]
    else:
        assert not kbd._record_task_failure(conn, tid, error="Iteration budget exhausted",
                                            outcome="timed_out", release_claim=True, end_run=True)
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (old_run,)).fetchone()
    assert run["ended_at"] is not None
    assert run["outcome"] == {"clean": "crashed", "nonzero": "crashed", "ttl": "reclaimed",
                              "manual": "reclaimed", "orphan": "reclaimed", "stale": "stale",
                              "timeout": "timed_out", "budget": "timed_out"}[route]
    assert run["error"]
    if reference != "new":
        assert not _holds(conn, tid)
        if not board["spawns"]:
            assert len(_tick(board).spawned) == 1
        assert kb.get_task(conn, tid).status == "running"
        return
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.current_run_id is task.claim_lock is task.worker_pid is None
    assert task.assignee == "worker"
    assert task.consecutive_failures == int(route in {"nonzero", "timeout", "budget"})
    assert task.block_recurrences == 0  # Not a worker block-loop decision.
    holds = _holds(conn, tid)
    assert len(holds) == 1 and holds[0].run_id == old_run
    assert holds[0].payload["comment_id"] == comment_id
    assert "owner" in holds[0].payload["reason"].lower()
    assert kb._has_sticky_block(conn, tid)
    for _ in range(2):
        assert kb.recompute_ready(conn) == 0
        assert not _tick(board).spawned
    assert len(_holds(conn, tid)) == 1
    assert kb.claim_task(conn, tid) is None
    assert not kb.request_review(conn, tid, expected_run_id=old_run)
    # Owner reconciliation uses the existing lifecycle; no new acknowledgement
    # API or waiting period, and the old reference does not poison the next run.
    kb.add_comment(conn, tid, "owner", "Reconciled existing work; resume same card and PR")
    assert kb.unblock_task(conn, tid)
    assert len(_tick(board).spawned) == 1
    successor = kb.get_task(conn, tid)
    assert successor.current_run_id != old_run
    assert not kb.request_review(conn, tid, expected_run_id=old_run)
    kbd._record_worker_exit(PID, 256)
    assert _tick(board).crashed == [tid]
    assert kb.get_task(conn, tid).status == "running"
    assert len(_holds(conn, tid)) == 1


@pytest.mark.parametrize("scenario", ["ready", "review_return", "review_crash", "done_reopen", "live_owner",
    "losing_reclaim", "exhausted_clean", "exhausted_nonzero", "auth", "rate_limit", "dependency",
    "recent_success", "legacy_new", "legacy_old", "legacy_same_second", "ended_run", "notifier", "rollback"])
def test_continuation_and_existing_safeguards(board, monkeypatch, scenario):
    conn = board["conn"]
    tid, run_id = _new(board, claim=scenario != "ready", max_retries=1 if scenario.startswith("exhausted") else None)
    comment_id = kb.add_comment(conn, tid, "worker", PR)
    if scenario == "ready":
        assert len(_tick(board).spawned) == 1
    elif scenario in {"review_return", "review_crash"}:
        assert kb.request_review(conn, tid, reviewer="reviewer", expected_run_id=run_id)
        assert len(_tick(board).spawned) == 1
        review = kb.get_task(conn, tid)
        if scenario == "review_return":
            assert kb.request_changes(conn, tid, reason="continue same PR", expected_run_id=review.current_run_id) == (True, "worker")
        else:
            kb.add_comment(conn, tid, "reviewer", PR)
            kbd._record_worker_exit(PID, 256)
        assert len(_tick(board).spawned) == 1
        assert kb.get_task(conn, tid).assignee == ("worker" if scenario == "review_return" else "reviewer")
        assert not _holds(conn, tid)
    elif scenario == "done_reopen":
        assert kb.complete_task(conn, tid, expected_run_id=run_id)
        # Exercise the actual dashboard domain writer, not a made-up status event.
        path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
        spec = importlib.util.spec_from_file_location("kanban_recovery_dashboard", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        assert mod._set_status_direct(conn, tid, "ready")
        assert len(_tick(board).spawned) == 1
    elif scenario == "live_owner":
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
        assert not _tick(board).spawned
        with kbc.connect() as other:
            assert kb.claim_task(other, tid) is None
        assert kb.get_task(conn, tid).current_run_id == run_id
        assert not _holds(conn, tid)
    elif scenario == "losing_reclaim":
        # A genuine native handoff+successor wins after the reclaimer selected
        # its old row. The old PID/lock CAS must not close or hold the successor.
        def change_owner(pid, lock, **kwargs):
            with kbc.connect() as other:
                assert kb.request_review(other, tid, reviewer="reviewer", expected_run_id=run_id)
                successor = kb.claim_review_task(other, tid, claimer="different-host:successor")
                assert successor is not None
                board["successor"] = successor.current_run_id
            return {"terminated": True}
        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", change_owner)
        assert not kb.reclaim_task(conn, tid)
        assert kb.get_task(conn, tid).current_run_id == board["successor"]
        assert not _holds(conn, tid)
    elif scenario.startswith("exhausted"):
        kbd._record_worker_exit(PID, 0 if scenario == "exhausted_clean" else 256)
        result = _tick(board)
        assert result.auto_blocked == [tid] and not result.spawned
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]) == 1
    elif scenario == "auth":
        kbd._record_task_failure(conn, tid, "authentication rejected", outcome="spawn_failed", release_claim=True, end_run=True)
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"
        assert not _tick(board).spawned
    elif scenario == "rate_limit":
        # Rate-limit exits remain quota-wall retries, not publication holds.
        kbd._record_worker_exit(PID, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
        assert not _tick(board).spawned
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
        assert kb.get_task(conn, tid).consecutive_failures == 0
        assert not _holds(conn, tid)
    elif scenario == "dependency":
        assert kb.reclaim_task(conn, tid)
        parent = kb.create_task(conn, title="Unfinished parent")
        kb.link_tasks(conn, parent, tid)
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "todo"
        assert not _tick(board).spawned
        assert kb.claim_task(conn, tid) is None
    elif scenario == "recent_success":
        assert kb.complete_task(conn, tid, expected_run_id=run_id)
        # Remove time equality with old events: success without a later native
        # reopen is still guarded (the unrelated success policy is unchanged).
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = created_at - 10 WHERE task_id = ?", (tid,))
        assert kbd.check_respawn_guard(conn, tid) == "recent_success"
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None
    elif scenario.startswith("legacy"):
        # Pre-upgrade claim lacks the cursor; integer-second fallback explicitly
        # treats a same-second reference as uncertain, not as proven publication.
        with kb.write_txn(conn):
            row = conn.execute("SELECT id, payload FROM task_events WHERE run_id = ? AND kind = 'claimed'", (run_id,)).fetchone()
            payload = json.loads(row["payload"])
            payload.pop("comment_cursor", None)
            conn.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), row["id"]))
            started = conn.execute("SELECT started_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
            delta = {"legacy_old": -1, "legacy_new": 1, "legacy_same_second": 0}[scenario]
            conn.execute("UPDATE task_comments SET created_at = ? WHERE id = ?", (started + delta, comment_id))
        kbd._record_worker_exit(PID, 256)
        result = _tick(board)
        assert bool(_holds(conn, tid)) == (scenario != "legacy_old")
        assert bool(result.spawned) == (scenario == "legacy_old")
    elif scenario == "ended_run":
        assert kb.request_review(conn, tid, reviewer="reviewer", expected_run_id=run_id)
        with kb.write_txn(conn):
            assert kb._end_run(conn, tid, outcome="crashed") is None
        assert not _holds(conn, tid)
        assert len(_tick(board).spawned) == 1
    elif scenario == "notifier":
        from hermes_cli import kanban_db_notify as kbn
        from gateway.kanban_watchers_notifier import _KanbanNotification, TERMINAL_KINDS
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="disposable-owner",
                           notifier_profile="default", delivery_mode="notify+wake")
        assert kb.reclaim_task(conn, tid)
        sub = kbn.list_notify_subs(conn, tid)[0]
        cursor, events = kbn.unseen_events_for_sub(conn, task_id=tid, platform="telegram",
                                                  chat_id="disposable-owner", kinds=TERMINAL_KINDS)
        hold = next(e for e in events if e.kind == "blocked")
        notification = _KanbanNotification(None, {"sub": sub, "task": kb.get_task(conn, tid),
            "events": events, "cursor": cursor}, platform_cls=None, sub_fail_counts={})
        text = notification.format_event(hold)
        assert "Owner: reconcile" in text and "native unblock" in text
        notification.build_wake_text()
        assert "blocked" in notification.wake_kinds
        assert tid in notification.synth
        # Native cursor selection and formatting only: no adapter send or wake.
    else:
        # Engine-generated failure of the hold event must roll back release,
        # closed-run history and status together, not leave an eligible gap.
        conn.execute("CREATE TEMP TRIGGER refuse_hold BEFORE INSERT ON task_events WHEN NEW.kind = 'blocked' BEGIN SELECT RAISE(ABORT, 'hold refused'); END")
        kbd._record_worker_exit(PID, 256)
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError, match="hold refused"):
            kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).current_run_id == run_id
        assert kb.get_task(conn, tid).status == "running"
        assert kb.latest_run(conn, tid).ended_at is None
