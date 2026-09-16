"""Shared CLI/tool adapter; lifecycle authority and checks remain native."""
import json

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_recovery import classify_blocker, get_recovery_state


def apply_recovery(conn, task_id, recovery):
    """Reject unknown per-action fields before any mutation; never fall through."""
    required = {"action", "observed_token", "block_event_id", "rationale", "evidence_refs"}
    if not isinstance(recovery, dict):
        raise ValueError("recovery must be an object")
    action = recovery.get("action")
    if action == "classify":
        optional = {"existing_blocker_id"}
    elif action in ("retry", "resolved_resume", "finalize"):
        required |= {"blocker_id", "initiation_mode", "settlement_refs"}
        optional = {"instruction_ref"}
    else:
        raise ValueError("unsupported recovery action; use classify, retry, resolved_resume or finalize")
    if not required <= recovery.keys() or recovery.keys() - required - optional:
        raise ValueError("invalid recovery fields for action")
    # Transport shape only; lifecycle, evidence policy and authority remain native.
    for key in ("observed_token", "rationale", "existing_blocker_id", "blocker_id",
                "initiation_mode", "instruction_ref"):
        if key in recovery and not isinstance(recovery[key], str):
            raise ValueError(f"{key} must be a string")
    if type(recovery["block_event_id"]) is not int:
        raise ValueError("block_event_id must be an integer")
    if (not isinstance(recovery["evidence_refs"], list)
            or any(not isinstance(ref, str) for ref in recovery["evidence_refs"])):
        raise ValueError("evidence_refs must be an array of strings")
    if action != "classify":
        from hermes_cli.kanban_db_recovery_release import recover_task
        refs = recovery["settlement_refs"]
        if not isinstance(refs, list):
            raise ValueError("settlement_refs must be an array")
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("settlement reference must be an object")
            fields = {"observed_token", "task_id", "block_event_id", "blocker_id",
                      "board_path", "host", "boot_id", "run_ids", "scopes",
                      "observed_at", "assertion", "reference"}
            if set(ref) != fields:
                raise ValueError("invalid settlement reference fields")
            for key in ("block_event_id", "observed_at"):
                if type(ref.get(key)) is not int:
                    raise ValueError("settlement scope IDs and time must be integers")
            for key in ("observed_token", "task_id", "blocker_id", "board_path", "host", "boot_id",
                        "assertion", "reference"):
                if not isinstance(ref.get(key), str):
                    raise ValueError("settlement identity fields must be strings")
            for key, kind in (("run_ids", int), ("scopes", str)):
                if not isinstance(ref.get(key), list) or any(type(v) is not kind for v in ref[key]):
                    raise ValueError("invalid settlement scope array")
        if action == "finalize":
            from hermes_cli.kanban_db_recovery_finalize import finalize_task
            try:
                result = finalize_task(conn, task_id, **{k: v for k, v in recovery.items() if k != "action"})
            except Exception as exc:
                # Native postcommit failures do not roll back done. Observe only;
                # never retry or turn an exception into a held/success assertion.
                observation = ""
                try:
                    if not conn.in_transaction:
                        task = kb.get_task(conn, task_id)
                        if task is not None:
                            observation = f"; observed committed status={task.status} (not a rollback guarantee; do not replay finalization)"
                except Exception:
                    pass  # A failed observation must not mask the original error.
                raise RuntimeError(f"finalize: {type(exc).__name__}: {exc}{observation}") from exc
            if "status" in result:
                return {"task_id": task_id, **result}
        else:
            result = recover_task(conn, task_id, **recovery)
        task = kb.get_task(conn, task_id)
        return {"task_id": task_id, "status": task.status if task else None, **result}
    result = classify_blocker(conn, task_id, **{k: v for k, v in recovery.items() if k != "action"})
    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError("Task not found")
    return {**result, "task_id": task_id, "classified": result["ok"], "released": False,
            "status": task.status}


OWNER_ASSESSMENT = (
    "Read-only template, NOT settlement evidence or permission. Before use the OWNER must "
    "assess local attribution, detached descendants, external runtime and workspace; "
    "only then add assertion='settled' and a reference to that assessment. Native PID "
    "checks do not prove detached/external/workspace settlement."
)


def recovery_inspection(conn, task_id):
    """Pin accounting and binding template to one read snapshot; never attest."""
    from hermes_cli.kanban_db_recovery_release import settlement_scope
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        state = get_recovery_state(conn, task_id)
        scope = {"read_only": True, "owner_assessment_required": OWNER_ASSESSMENT}
        try:
            template = settlement_scope(conn, task_id)
        except (OSError, ValueError, TypeError):
            scope.update(available=False, reason="Native local host/boot settlement scope unavailable; no settlement asserted.")
        else:
            scope.update(available=True, template=template)
        return {"recovery": state, "settlement_scope": scope}
    finally:
        if own_transaction:
            conn.rollback()


def recovery_summary(state):
    """Readable current accounting, with exact identifiers for audited recovery."""
    identity = state["active_blocker_id"]
    if identity is None:
        return "No active blocker report."
    blocker = state["blockers"][identity]
    return (f"Blocker {identity}; report {state['active_event_id']}; "
            f"count {blocker['count']}; revision {state['revision']} "
            f"({'legacy lower-bound accounting' if state['legacy'] else 'tracked accounting'}). "
            "Read-only observation; no release.\n"
            f"  observed_token: {state['observed_token']}")


def settlement_summary(scope):
    return OWNER_ASSESSMENT + "\n  " + (
        json.dumps(scope["template"], sort_keys=True) if scope["available"] else scope["reason"])
