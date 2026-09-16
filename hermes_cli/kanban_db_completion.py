"""Shared native completion preparation, transactional consume and postcommit effects."""
import time

def prepare(conn, task_id, *, result=None, summary=None, metadata=None, created_cards=None, expected_run_id=None, recovery=None):
    from hermes_cli import kanban_db as kb
    now = int(time.time())
    # Cheap pre-check; re-checked inside the txn to close the parent-reopen race.
    if not kb._parents_satisfied(conn, task_id):
        return False
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance
    verified_cards = kb._gate_created_cards(conn, task_id, created_cards, summary or result)
    metadata = kb._merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    acceptance = (prepare_acceptance(conn, task_id, expected_run_id, metadata) if recovery is None
                  else prepare_acceptance(conn, task_id, expected_run_id, metadata, _recovery=recovery))
    if acceptance is False:
        return False
    return dict(result=result, summary=summary, metadata=metadata, expected_run_id=expected_run_id,
                verified_cards=verified_cards, acceptance=acceptance, now=now)


def consume(conn, task_id, prepared, *, recovery=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_pr_acceptance_store import record_acceptance
    result, summary, metadata = (prepared[k] for k in ("result", "summary", "metadata"))
    expected_run_id, verified_cards, acceptance, now = (prepared[k] for k in ("expected_run_id", "verified_cards", "acceptance", "now"))
    handoff_summary = summary if summary is not None else result
    # Hard invariant even for human review approval: a parent may have
    # reopened while this task waited.
    if not kb._parents_satisfied(conn, task_id):
        return False
    if recovery is not None:
        from hermes_cli.kanban_db_recovery_finalize import _Finalization
        if type(recovery) is not _Finalization:
            raise ValueError("invalid internal completion context")
        recovery.validate(conn, task_id)
    if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
        return False
    prior_status = kb._task_status(conn, task_id)
    sql = """
            UPDATE tasks
               SET status       = 'done',
                   result       = ?,
                   completed_at = ?,
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL,
                   block_kind   = NULL,
                   block_recurrences = 0
             WHERE id = ?
               AND status IN ('running', 'ready', 'blocked', 'review')
            """
    if recovery is not None:
        recovery.record(conn, task_id)
        sql = sql.replace("status IN ('running', 'ready', 'blocked', 'review')", "status = 'triage'")
    params: tuple = (result, now, task_id)
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params = (*params, int(expected_run_id))
    if conn.execute(sql, params).rowcount != 1:
        return False
    if isinstance(metadata, dict):
        kb._stage_completion_artifacts(conn, task_id, metadata, now)
    run_id = kb._end_run(
        conn, task_id, outcome="completed", status="done", summary=handoff_summary,
        metadata=metadata,
    )
    # Never-claimed task: synthesize a run so the handoff fields survive.
    if run_id is None and (summary or metadata or result or prior_status == "review"):
        synth_summary, synth_metadata = handoff_summary, metadata
        if prior_status == "review" and not synth_summary and not synth_metadata:
            synth_summary = kb._REVIEW_APPROVED_NOTE
            synth_metadata = {"source_status": "review", "approval": "manual"}
        run_id = kb._synthesize_ended_run(
            conn, task_id, outcome="completed", summary=synth_summary, metadata=synth_metadata,
        )
    event_summary = handoff_summary
    if prior_status == "review" and not event_summary:
        event_summary = kb._REVIEW_APPROVED_NOTE
    completed_event_id = kb._append_event(
        conn, task_id, "completed",
        kb._completed_event_payload(result, event_summary, verified_cards, metadata),
        run_id=run_id,
    )
    from hermes_cli.kanban_db_recovery import close_completed_cycle
    close_completed_cycle(conn, task_id, completed_event_id)
    return {"run_id": run_id}


def postcommit(conn, task_id, prepared, outcome, *, fire_lifecycle_hook=True):
    from hermes_cli import kanban_db as kb
    result, summary, metadata = (prepared[k] for k in ("result", "summary", "metadata"))
    expected_run_id, verified_cards, acceptance, now = (prepared[k] for k in ("expected_run_id", "verified_cards", "acceptance", "now"))
    handoff_summary = summary if summary is not None else result
    run_id = outcome["run_id"]
    kb._flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    # Success wipes the breaker counter (history stays on the event log).
    kb._clear_failure_counter(conn, task_id)
    kb.recompute_ready(conn)  # separate txn so children see ``done``
    kb._cleanup_workspace(conn, task_id)
    _done_task = kb.get_task(conn, task_id)
    if fire_lifecycle_hook:
        kb._fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
    return True
