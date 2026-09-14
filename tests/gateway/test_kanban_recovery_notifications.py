"""Recovery diagnostics consume real native history, not invented hold payloads."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.kanban_watchers_notifier import _Collector, _KanbanNotification
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn


@pytest.mark.parametrize("kind", ["crashed", "timed_out"])
@pytest.mark.parametrize("split", [False, True])
def test_current_hold_survives_batches_and_later_attempts(tmp_path, monkeypatch, kind, split):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *args, **kwargs: True)
    monkeypatch.setattr(kbd, "_kill_fn", lambda fn=None: lambda *args: None)
    sent = []
    async def send(chat, text, **kwargs):
        sent.append(text)
    adapter = SimpleNamespace(send=send)
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter},
        _authorization_adapter=lambda *a: adapter, _owns_kanban_dispatcher_lock=lambda: True)
    collector = _Collector(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Recovery", assignee="worker", max_retries=10)
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="42", delivery_mode="notify+wake")
        sub = kbn.list_notify_subs(conn)[0]
        previous_run = None
        previous_event = None
        for held in (False, True, True, False):
            task = kb.claim_task(conn, tid)
            assert task is not None
            run_id = task.current_run_id
            comment_id = kb.add_comment(conn, tid, "worker", "https://github.com/disposable/test/pull/1") if held else None
            kbd._set_worker_pid(conn, tid, 2147483647)
            if kind == "crashed":
                assert kbd.detect_crashed_workers(conn) == [tid]
            else:
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = ?", (tid,))
                    conn.execute("UPDATE task_runs SET started_at = 1 WHERE id = ?", (run_id,))
                assert kbd.enforce_max_runtime(conn) == [tid]
            # Persist the first poll's cursor through the hold, then collect
            # the trailing native crash/timeout in a genuinely separate claim.
            if split and held:
                hold = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1]
                kbn.advance_notify_cursor(conn, task_id=tid, platform="telegram",
                                          chat_id="42", new_cursor=hold.id)
            delivery = collector._claim_for_sub(conn, "default", sub)
            assert delivery is not None
            if split and held:
                assert all(e.kind != "blocked" for e in delivery["events"])
            n = _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts={})
            n.adapter = adapter
            for event in delivery["events"]:
                msg = n.format_event(event)
                if msg:
                    asyncio.run(n._send_event(event, msg))
            n.build_wake_text()
            if held:
                for text in (sent[-1], n.synth):
                    assert "reconcile existing work/publication before native unblock" in text
                    assert f"run_id={run_id}" in text and f"comment_id={comment_id}" in text
                    assert "will retry" not in text
                    if previous_run is not None:
                        assert f"run_id={previous_run};" not in text
                if previous_event is not None:
                    historical = n.format_event(previous_event)
                    assert f"event run: {previous_run}" in historical
                    assert f"Current recovery hold: run_id={run_id};" in historical
                    n.build_wake_text()
                    assert f"run_id={run_id};" in n.synth
                assert kb.unblock_task(conn, tid)
            else:
                assert "will retry" in sent[-1]
                assert "native unblock" not in n.synth
            previous_run = run_id
            previous_event = [e for e in delivery["events"] if e.kind == kind][-1]
