"""Native recovery/claim races: disposable SQLite, no real workers or signals."""
import json
from pathlib import Path

import pytest

from hermes_cli import config, profiles
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR = "Possible reference https://github.com/disposable/recovery-test/pull/1"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "load_config_readonly", lambda: {})
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "worker")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **kw: {"terminated": True})
    conn = kbc.connect()
    try:
        yield conn
    finally:
        receipt = {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
                   for table in ("tasks", "task_runs", "task_comments", "task_events")}
        receipt["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2))
        conn.close()


def _holds(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "blocked"
            and e.payload.get("reason_code") == "interrupted_implementation"]


@pytest.mark.parametrize("reference", ["new", "old", "none"])
def test_dangling_claim_closes_once_and_requires_owner_only_for_new_reference(board, reference):
    conn = board
    tid = kb.create_task(conn, title="Dangling claim", assignee="worker", workspace_kind="scratch")
    if reference == "old":
        kb.add_comment(conn, tid, "owner", PR)
    original = kb.claim_task(conn, tid)
    run_id = original.current_run_id
    if reference == "new":
        kb.add_comment(conn, tid, "worker", PR)
    # Corruption fixture: an unclaimed Ready row still points to its open run.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET summary=?, error=?, metadata=? WHERE id=?",
                     ("existing summary", "existing error", '{"retained": true}', run_id))
    successor = kb.claim_task(conn, tid)
    closed = dict(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone())
    assert closed["ended_at"] is not None and closed["outcome"] == "reclaimed"
    assert (closed["summary"], closed["error"], closed["metadata"]) == (
        "existing summary", "existing error", '{"retained": true}')
    if reference != "new":
        assert successor is not None and successor.current_run_id != run_id
        assert not _holds(conn, tid)
        return
    assert successor is None
    held = kb.get_task(conn, tid)
    assert held.status == "blocked" and held.current_run_id is held.claim_lock is None
    assert len(_holds(conn, tid)) == 1
    assert _holds(conn, tid)[0].run_id == run_id
    def refuse_spawn(*args, **kwargs):
        pytest.fail("held task must not spawn")
    for _ in range(2):
        assert not kbd.dispatch_once(conn, spawn_fn=refuse_spawn, max_spawn=1).spawned
        assert kb.claim_task(conn, tid) is None
    assert len(_holds(conn, tid)) == 1
    kb.add_comment(conn, tid, "owner", "Reconciled existing publication; resume")
    assert kb.unblock_task(conn, tid)
    successor = kb.claim_task(conn, tid)
    assert successor is not None and successor.current_run_id != run_id
    assert dict(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()) == closed


@pytest.mark.parametrize("race", [True, False])
def test_null_lock_manual_reclaim_cannot_erase_concurrent_orphan_hold(board, monkeypatch, race):
    conn = board
    tid = kb.create_task(conn, title="Orphan race", assignee="worker", workspace_kind="scratch")
    original = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, "worker", PR)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
    settled = {}
    def terminate(*args, **kwargs):
        if race:
            # Inject at the real out-of-transaction termination boundary. The
            # competing writer uses its own connection and native recovery.
            with kbc.connect() as other:
                assert kbd.reconcile_orphaned_running(other) == [tid]
                for table in ("tasks", "task_runs", "task_events"):
                    settled[table] = [dict(r) for r in other.execute(f"SELECT * FROM {table} ORDER BY id")]
        return {"terminated": True}
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate)
    assert kb.reclaim_task(conn, tid) is (not race)
    assert kb.get_task(conn, tid).status == "blocked"
    assert len(_holds(conn, tid)) == 1
    assert _holds(conn, tid)[0].run_id == original.current_run_id
    if race:
        for table, rows in settled.items():
            assert [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")] == rows
    assert kb.unblock_task(conn, tid)
    assert kb.claim_task(conn, tid) is not None
