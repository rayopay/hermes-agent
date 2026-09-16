"""Target-local recovery. Settlement references are OWNER ATTESTATIONS.

Native code checks recorded local PIDs and database ownership, not remote runtime
or detached-descendant absence. The top-level owner must evaluate the referenced
evidence. This cooperative boundary is not an OS sandbox or project RBAC.
"""
from __future__ import annotations

import hashlib
import json
import socket
import time
from pathlib import Path

import psutil

from hermes_cli import kanban_db_recovery as accounting


def settlement_scope(conn, task_id):
    """Read-only binding template, NOT a settlement receipt or absence proof."""
    from hermes_cli.kanban_db_connect import connection_db_path
    task, state, view, runs = accounting._observe(conn, task_id)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text(encoding="utf-8").strip()
    if not boot or not socket.gethostname():
        raise ValueError("Known local host identity required")
    return {"observed_token": view["observed_token"], "task_id": task_id,
            "block_event_id": state["active_event_id"],
            "blocker_id": state["active_blocker_id"],
            "board_path": str(connection_db_path(conn)),
            "host": socket.gethostname(), "boot_id": boot,
            "run_ids": [r["id"] for r in runs],
            "scopes": ["local_attribution", "detached_descendants", "external_runtime", "workspace"],
            "observed_at": int(time.time())}


def _settled(conn, task_id, refs):
    expected = settlement_scope(conn, task_id)
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
        return False
    receipt = refs[0]
    timestamp = receipt.get("observed_at")
    if (type(timestamp) is not int or not 0 <= int(time.time()) - timestamp <= 300
            or receipt.get("assertion") != "settled"
            or not isinstance(receipt.get("reference"), str) or not receipt["reference"].strip()
            or len(receipt["reference"]) > 1000
            or set(receipt) != set(expected) | {"reference", "assertion"}
            or any(receipt.get(k) != v for k, v in expected.items() if k != "observed_at") ):
        return False
    # Row PIDs are cleared by native terminal transitions. Never forget durable
    # spawned events. A live bare legacy PID is conservatively unsafe even if reused.
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='spawned'", (task_id,)):
        payload = json.loads(row[0])
        pid = payload.get("pid")
        if type(pid) is not int or pid <= 0:
            return False
        try:
            # Only authoritative absence permits progress; live/reused PIDs
            # (including zombies) and probe uncertainty must retain the hold.
            if psutil.pid_exists(pid) is not False:
                return False
        except (psutil.Error, OSError, ValueError, TypeError):
            return False
    return True


def recover_task(conn, task_id, *, observed_token, block_event_id, blocker_id,
                 action, rationale, evidence_refs, initiation_mode, settlement_refs,
                 instruction_ref=None):
    """Release one held observation; never spawn, kill, complete or reset failures."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    actor = accounting._actor()
    def denied(reason):
        return {"ok": False, "released": False, "eligible": False, "held": True, "reason": reason}
    if (action not in ("retry", "resolved_resume")
            or initiation_mode not in ("user_directed", "autonomous")
            or not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000
            or type(block_event_id) is not int or not isinstance(blocker_id, str)
            or not isinstance(observed_token, str)
            or not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 20
            or any(not isinstance(r, str) or not r.strip() or len(r) > 1000 for r in evidence_refs)
            or len(set(evidence_refs)) != len(evidence_refs)
            or (initiation_mode == "user_directed" and (not isinstance(instruction_ref, str) or not instruction_ref.strip()))):
        return denied("invalid recovery request")
    if conn.in_transaction:
        raise ValueError("Recovery requires its own dispatch-excluded transaction")
    try:
        path = kbc.connection_db_path(conn)
    except (ValueError, OSError):
        return denied("dispatch database identity unavailable")
    with kbc._dispatch_tick_lock(path, strict=True) as held:
        if not held:
            return denied("dispatch exclusion unavailable")
        with kb.write_txn(conn):
            actor = accounting._actor()
            task, state, view, runs = accounting._observe(conn, task_id)
            if (view["observed_token"] != observed_token or state["active_event_id"] != block_event_id
                    or state["active_blocker_id"] != blocker_id):
                return denied("stale report or observation")
            if (task["status"] not in ("blocked", "triage")
                    or any(task[k] is not None for k in ("claim_lock", "claim_expires", "worker_pid", "current_run_id"))
                    or any(r["ended_at"] is None or r["claim_lock"] is not None or r["worker_pid"] is not None for r in runs)):
                return denied("held unclaimed task with no open ownership required")
            if not state["reports"][str(block_event_id)]["counted"]:
                return denied("exact counted blocker required")
            try:
                settled = _settled(conn, task_id, settlement_refs)
            except (OSError, ValueError, TypeError):
                settled = False
            if not settled:
                return denied("local ownership or scoped owner settlement unresolved")
            resume = kb._resume_status_from_events(conn, task_id)
            # A committed native completion ends the execution epoch. Run ids,
            # unlike second-resolution timestamps, order same-second reopenings.
            # Without the completion event, retain the conservative hold.
            completed = conn.execute(
                "SELECT id FROM task_events WHERE task_id=? AND kind='completed' ORDER BY id DESC LIMIT 1",
                (task_id,)).fetchone()
            completed_run = max((r["id"] for r in runs if r["outcome"] == "completed"), default=0) if completed else 0
            # Accepted work in the current epoch still cannot restart execution.
            from hermes_cli.kanban_db_recovery_provenance import authorized_rework, legacy_enrollment
            if resume != "review" and not authorized_rework(conn, task, runs, completed_run, block_event_id):
                return denied("accepted execution requires a narrower native continuation phase")
            blocker = state["blockers"][blocker_id]
            if action == "retry" and blocker["count"] >= kb.BLOCK_RECURRENCE_LIMIT:
                return denied("unresolved blocker budget exhausted")
            consumed = {ref for epoch in [state, *(c["state"] for c in state.get("closed_cycles", []))]
                        for resolution in epoch.get("resolutions", []) for ref in resolution["evidence_refs"]}
            if action == "resolved_resume" and (not blocker["count"] or consumed.intersection(evidence_refs)):
                return denied("resolution evidence already consumed or no unresolved count")
            prior = json.loads(accounting._encode(state))
            landing = "todo" if not kb._parents_satisfied(conn, task_id) else resume
            payload = {"action": action, "actor": actor, "initiation_mode": initiation_mode,
                       "instruction_ref": instruction_ref, "rationale": rationale.strip(),
                       "evidence_refs": evidence_refs, "observed_token": observed_token,
                       "block_event_id": block_event_id, "blocker_id": blocker_id,
                       "cycle": blocker["cycle"], "prior_status": task["status"], "status": landing,
                       "resume_status": resume, "prior_revision": view["revision"], "revision": view["revision"] + 1,
                       "settlement_basis": "OWNER ATTESTATIONS plus recorded local PID probes",
                       "settlement_refs": settlement_refs,
                       "prior_state_sha256": hashlib.sha256(accounting._encode(prior).encode()).hexdigest()}
            if action == "resolved_resume":
                resolution = {"blocker_id": blocker_id, "cycle": blocker["cycle"],
                              "count": blocker["count"], "evidence_refs": evidence_refs}
                payload["resolution"] = resolution
                blocker["cycle"] += 1
                blocker["count"] = 0
            # Redaction must not alter evidence keys that enforce consumption.
            if kb.redact_review_value(payload) != payload:
                return denied("recovery references must not contain secrets")
            if view["legacy"]:
                legacy_enrollment(conn, task, prior, view, actor)
            event = kb._append_event(conn, task_id, "blocker_resolved" if action == "resolved_resume" else "blocker_retried", payload)
            if action == "resolved_resume":
                state.setdefault("resolutions", []).append({"event_id": event, **resolution})
            accounting._save(conn, task_id, state, view["revision"])
            conn.execute("UPDATE tasks SET status=?, block_recurrences=? WHERE id=?",
                         (landing, accounting.report_count(state), task_id))
            result = accounting.get_recovery_state(conn, task_id)
        return {"ok": True, "released": True, "held": False, "eligible": landing in ("ready", "review"),
                "status": landing, "state": result, "recovery_event_id": event}
