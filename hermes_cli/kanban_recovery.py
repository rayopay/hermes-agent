"""Shared CLI/tool classification adapter; never a lifecycle release operation."""
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_recovery import classify_blocker


def classify_recovery(conn, task_id, recovery):
    """Validate the discriminator before invoking any native mutation."""
    required = {"action", "observed_token", "block_event_id", "rationale", "evidence_refs"}
    if not isinstance(recovery, dict):
        raise ValueError("recovery must be an object")
    if recovery.get("action") != "classify":
        raise ValueError("unsupported recovery action; only classify is available")
    if not required <= recovery.keys() or recovery.keys() - required - {"existing_blocker_id"}:
        raise ValueError("invalid recovery fields; provide observation, report, rationale and evidence_refs")
    result = classify_blocker(conn, task_id, **{k: v for k, v in recovery.items() if k != "action"})
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError("Task not found")
    return {**result, "task_id": task_id, "classified": result["ok"], "released": False,
            "status": task.status}


def recovery_summary(state):
    """Readable current accounting, with exact identifiers for audited correction."""
    identity = state["active_blocker_id"]
    if identity is None:
        return "No active blocker report."
    blocker = state["blockers"][identity]
    return (f"Blocker {identity}; report {state['active_event_id']}; "
            f"count {blocker['count']}; revision {state['revision']} "
            f"({'legacy lower-bound accounting' if state['legacy'] else 'tracked accounting'}). "
            "Classification only; no release.\n"
            f"  observed_token: {state['observed_token']}")
