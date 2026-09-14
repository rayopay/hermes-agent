"""Owner reconciliation for an interrupted implementation's possible PR handoff."""

from __future__ import annotations

import re
import sqlite3


_POSSIBLE_PR_URL = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE,
)
_INTERRUPTED_OUTCOMES = frozenset({"crashed", "timed_out", "stale", "reclaimed", "spawn_failed", "gave_up"})


def hold_interrupted_implementation(
    conn: sqlite3.Connection, task_id: str, run_id: int, outcome: str,
) -> None:
    """Compose a sticky hold under the successful run-close transaction.

    This is recovery, not admission policy: a Ready/Review card with an old PR
    reference is ordinary work. Only closing an interrupted implementation can
    introduce this hold; native handoff, completion and quota-wall retries keep
    their existing semantics. The caller still owns current_run_id here.
    """
    from hermes_cli import kanban_db as kb

    if outcome not in _INTERRUPTED_OUTCOMES or kb._retry_status_for_run(conn, task_id, run_id) == "review":
        return
    claim = kb._json_dict(kb._row_get(kb._latest_event(conn, task_id, "claimed", run_id), "payload"))
    cursor = claim.get("comment_cursor")
    if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
        comments = conn.execute(
            "SELECT id, body FROM task_comments WHERE task_id = ? AND id > ? ORDER BY id",
            (task_id, cursor),
        )
        evidence_basis = "claim_comment_cursor"
    else:
        # Pre-upgrade runs have no cursor. Their integer-second timestamps
        # cannot order a same-second comment against the claim, so include the
        # boundary conservatively, but NEVER sweep older task history. This
        # fallback can hold an old same-second reference once; after owner
        # unblock the next native claim's cursor removes that ambiguity.
        comments = conn.execute(
            "SELECT c.id, c.body FROM task_comments c JOIN task_runs r "
            "ON r.id = ? AND r.task_id = c.task_id "
            "WHERE c.task_id = ? AND c.created_at >= r.started_at ORDER BY c.id",
            (run_id, task_id),
        )
        evidence_basis = "legacy_started_at_inclusive"
    comment = next((c for c in comments if _POSSIBLE_PR_URL.search(c["body"] or "")), None)
    if comment is None:
        return
    # The reference is NOT evidence of who published, completion, acceptance,
    # or reviewer readiness. It only makes replay uncertain. Retain the failed
    # run verbatim and ask the owner to inspect what exists rather than guessing
    # a handoff or using elapsed time as permission to repeat external effects.
    reason = (
        "Owner: reconcile existing work/publication before native unblock. "
        f"Interrupted implementation run {run_id} has a possible PR reference "
        f"in comment {comment['id']}; publication/handoff is uncertain. "
        "Resume this card after reconciliation, or choose the appropriate lifecycle disposition."
    )
    changed = conn.execute(
        "UPDATE tasks SET status = 'blocked' WHERE id = ? AND current_run_id = ? "
        "AND status IN ('ready', 'blocked') AND claim_lock IS NULL",
        (task_id, run_id),
    )
    if changed.rowcount != 1:
        return
    # Existing blocked/unblocked events already provide durable readiness
    # fencing and owner notifications. This is not a worker's classified block
    # request: do not consume/reset its separate block-loop/triage counter.
    kb._append_event(conn, task_id, "blocked", {
        "reason": reason, "reason_code": "interrupted_implementation",
        "source_status": "ready", "comment_id": comment["id"],
        "evidence_basis": evidence_basis, "trigger_outcome": outcome,
    }, run_id=run_id)
