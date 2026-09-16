"""Owner-only direct triage completion for already-bound exact PR contracts.

No local-only/legacy enrollment or artifact-copy proof is inferred here. Remote
observations are not atomic with SQLite; the native collector rereads PR identity.
Settlement retains the existing cooperative owner-attestation boundary, not RBAC.
"""
from dataclasses import dataclass

from hermes_cli import kanban_db_recovery as accounting


class _Held(ValueError):
    pass


@dataclass(frozen=True)
class _Finalization:
    conn: object
    task_id: str
    observed_token: str
    block_event_id: int
    blocker_id: str
    settlement_refs: list
    actor: dict
    attribution: dict

    def validate(self, conn, task_id):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_recovery_release import _settled
        from hermes_cli.kanban_pr_acceptance import _PR
        if conn is not self.conn or task_id != self.task_id or accounting._actor() != self.actor:
            raise _Held("recovery authority changed")
        task, state, view, runs = accounting._observe(conn, task_id)
        if (view["observed_token"] != self.observed_token
                or state["active_event_id"] != self.block_event_id
                or state["active_blocker_id"] != self.blocker_id):
            raise _Held("stale report or observation")
        if (task["status"] != "triage" or view["legacy"]
                or any(task[k] is not None for k in ("claim_lock", "claim_expires", "worker_pid", "current_run_id"))
                or any(r["ended_at"] is None or r["claim_lock"] is not None or r["worker_pid"] is not None for r in runs)):
            raise _Held("tracked triage with settled native ownership required")
        if not state["reports"][str(self.block_event_id)]["counted"]:
            raise _Held("exact counted blocker required")
        if not _PR.fullmatch(task["completion_contract"] or ""):
            raise _Held("only an already-bound exact PR contract is supported")
        if not kb._parents_satisfied(conn, task_id):
            raise _Held("unfinished parent")
        if kb._scratch_workspace(conn, task_id) is not None:
            raise _Held("scratch artifact obligations require separate supported preservation")
        try:
            settled = _settled(conn, task_id, self.settlement_refs)
        except (OSError, ValueError, TypeError):
            settled = False
        if not settled:
            raise _Held("local ownership or scoped owner settlement unresolved")

    def record(self, conn, task_id):
        from hermes_cli import kanban_db as kb
        kb._append_event(conn, task_id, "triage_finalized", self.attribution)


def finalize_task(conn, task_id, *, observed_token, block_event_id, blocker_id,
                  rationale, evidence_refs, initiation_mode, settlement_refs,
                  instruction_ref=None):
    """Finish existing accepted PR work, without execution or status cycling.

    CLI/tool recovery adapters route explicit finalize requests here.
    Postcommit failures propagate after durable completion, as in complete_task.
    """
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_db_completion import prepare, consume, postcommit
    actor = accounting._actor()
    def denied(reason):
        return {"ok": False, "held": True, "completed": False, "reason": reason}
    if (not isinstance(observed_token, str) or type(block_event_id) is not int
            or not isinstance(blocker_id, str)
            or initiation_mode not in ("user_directed", "autonomous")
            or not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000
            or not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 20
            or any(not isinstance(r, str) or not r.strip() or len(r) > 1000 for r in evidence_refs)
            or len(set(evidence_refs)) != len(evidence_refs)
            or (initiation_mode == "user_directed" and (not isinstance(instruction_ref, str) or not instruction_ref.strip()))):
        return denied("invalid finalization request")
    if conn.in_transaction:
        raise ValueError("Finalization requires its own dispatch-excluded transaction")
    attribution = dict(actor=actor, observed_token=observed_token, block_event_id=block_event_id,
                       blocker_id=blocker_id, rationale=rationale, evidence_refs=evidence_refs,
                       initiation_mode=initiation_mode, instruction_ref=instruction_ref,
                       settlement_refs=settlement_refs,
                       settlement_basis="OWNER ATTESTATIONS plus recorded local PID probes")
    if kb.redact_review_value(attribution) != attribution:
        return denied("recovery references must not contain secrets")
    context = _Finalization(conn, task_id, observed_token, block_event_id, blocker_id,
                            settlement_refs, actor, attribution)
    try:
        path = kbc.connection_db_path(conn)
    except (ValueError, OSError):
        return denied("dispatch database identity unavailable")
    with kbc._dispatch_tick_lock(path, strict=True) as held:
        if not held:
            return denied("dispatch exclusion unavailable")
        try:
            context.validate(conn, task_id)
            prepared = prepare(conn, task_id, recovery=context)
            if prepared is False or prepared["acceptance"] is None or not prepared["acceptance"][1]["ok"]:
                return denied("current PR acceptance not established")
            with kb.write_txn(conn):
                outcome = consume(conn, task_id, prepared, recovery=context)
                if outcome is False:
                    raise _Held("completion snapshot changed")
        except _Held as exc:
            return denied(str(exc))
        postcommit(conn, task_id, prepared, outcome)
        return {"ok": True, "completed": True, "held": False, "status": "done"}
