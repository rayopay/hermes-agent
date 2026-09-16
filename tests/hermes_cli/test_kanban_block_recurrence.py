"""Unclassified blocker recurrence survives wording, category and dependency waits.

RP-HERMES-002: category/reason-only reports do not establish resolution or a
new blocker identity. Exercise native SQLite lifecycle transitions; no worker
processes or providers are launched.
"""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim_and_block(conn, task_id, *, kind, reason):
    claimed = kb.claim_task(conn, task_id, claimer="worker")
    assert claimed is not None
    assert kb.block_task(
        conn, task_id, kind=kind, reason=reason,
        expected_run_id=claimed.current_run_id,
    )
    return kb.get_task(conn, task_id)


@pytest.mark.parametrize("next_kind", ["capability", "needs_input", "transient", None])
def test_rewording_or_category_change_preserves_unresolved_recurrence(
    kanban_home, next_kind,
):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Use required build tool", assignee="worker")
        for recurrence in range(1, kb.BLOCK_RECURRENCE_LIMIT + 1):
            task = _claim_and_block(
                conn, tid,
                kind="capability" if recurrence == 1 else next_kind,
                reason=("Required build tool unavailable" if recurrence == 1
                        else f"Attempt {recurrence}: still need the tool installed"),
            )
            assert task.block_recurrences == recurrence
            if recurrence < kb.BLOCK_RECURRENCE_LIMIT:
                assert task.status == "blocked"
                assert kb.unblock_task(conn, tid)
                resumed = kb.get_task(conn, tid)
                assert resumed.status == "ready"
                assert resumed.block_recurrences == recurrence
            else:
                assert task.status == "triage"
                assert not kb.unblock_task(conn, tid)

        events = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
        assert len(events) == 1
        assert events[0].payload["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT
        assert events[0].payload["kind"] == next_kind
        assert events[0].payload["reason"] == (
            f"Attempt {kb.BLOCK_RECURRENCE_LIMIT}: still need the tool installed"
        )


@pytest.mark.parametrize("next_kind", ["capability", "transient"])
def test_dependency_wait_preserves_but_does_not_consume_unresolved_recurrence(
    kanban_home, next_kind,
):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Use required build tool", assignee="worker")
        # Reach the last permitted retry entirely through native transitions.
        for recurrence in range(1, kb.BLOCK_RECURRENCE_LIMIT):
            blocked = _claim_and_block(
                conn, tid, kind="capability", reason="Required build tool unavailable",
            )
            assert blocked.status == "blocked"
            assert blocked.block_recurrences == recurrence
            assert kb.unblock_task(conn, tid)

        prior_count = kb.get_task(conn, tid).block_recurrences
        claimed = kb.claim_task(conn, tid, claimer="worker")
        assert claimed is not None
        parent = kb.create_task(conn, title="Prepare input", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        assert kb.block_task(
            conn, tid, kind="dependency", reason="Wait for parent input",
            expected_run_id=claimed.current_run_id,
        )
        waiting = kb.get_task(conn, tid)
        assert waiting.status == "todo"
        assert waiting.block_kind == "dependency"
        assert waiting.block_recurrences == prior_count
        assert waiting.claim_lock is None
        assert not kb.unblock_task(conn, tid)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "todo"
        assert kb.claim_task(conn, tid, claimer="worker") is None
        events = kb.list_events(conn, tid)
        assert any(e.kind == "dependency_wait" for e in events)
        assert not any(e.kind == "block_loop_detected" for e in events)

        parent_claim = kb.claim_task(conn, parent, claimer="worker")
        assert parent_claim is not None
        assert kb.complete_task(
            conn, parent, result="Input prepared",
            expected_run_id=parent_claim.current_run_id,
        )
        kb.recompute_ready(conn)
        ready = kb.get_task(conn, tid)
        assert ready.status == "ready"
        assert ready.block_recurrences == prior_count

        blocked = _claim_and_block(
            conn, tid, kind=next_kind,
            reason="Input ready, but the required build tool is still missing",
        )
        assert blocked.block_recurrences == prior_count + 1
        assert blocked.status == "triage"
        events = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
        assert len(events) == 1
        assert events[0].payload["recurrences"] == prior_count + 1
        assert events[0].payload["kind"] == next_kind
