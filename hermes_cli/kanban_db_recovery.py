"""Stable unresolved blocker accounting. Classification is audit-only, never release.

Category and prose are observations, not authority to choose a cheaper identity.
Legacy counts are lower bounds, never invented exact report attributions.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _new_blocker(state, lower_bound=0):
    identity = uuid.uuid4().hex
    state["blockers"][identity] = {
        "cycle": 1, "count": lower_bound, "legacy_lower_bound": lower_bound,
    }
    state["active_blocker_id"] = identity
    return identity


def _current_epoch(state):
    """Return only current accounting; closed epochs are immutable snapshots."""
    return {**{k: v for k, v in state.items() if k != "closed_cycles"}, "version": 1}


def _validate(state):
    if isinstance(state, dict) and state.get("version") == 2:
        if type(state["version"]) is not int or not isinstance(state.get("closed_cycles"), list):
            raise ValueError("Malformed closed recovery cycles")
        _validate(_current_epoch(state))
        for cycle in state["closed_cycles"]:
            if (not isinstance(cycle, dict)
                    or set(cycle) != {"state", "completed_event_id", "closure_event_id", "prior_revision"}
                    or any(type(cycle[k]) is not int or cycle[k] < 1 for k in
                           ("completed_event_id", "closure_event_id", "prior_revision"))
                    or not isinstance(cycle["state"], dict) or cycle["state"].get("version") != 1):
                raise ValueError("Malformed closed recovery cycle")
            _validate(cycle["state"])
        return
    def require(condition):
        if not condition:
            raise ValueError("Malformed or unsupported task recovery state")

    try:
        require(set(state) - {"resolutions"} == {"version", "active_blocker_id", "active_event_id", "blockers", "reports", "corrections"})
        require(type(state["version"]) is int and state["version"] == 1)
        require(isinstance(state["blockers"], dict) and isinstance(state["reports"], dict))
        require(isinstance(state["corrections"], list))
        require(state["active_blocker_id"] is None or state["active_blocker_id"] in state["blockers"])
        require(state["active_event_id"] is None or str(state["active_event_id"]) in state["reports"])
        require(not state["blockers"] or state["active_blocker_id"] is not None)
        require(state["active_event_id"] is None or type(state["active_event_id"]) is int)
        require(not state["reports"] or state["active_event_id"] == max(map(int, state["reports"])))
        counts = {}
        for identity, blocker in state["blockers"].items():
            require(isinstance(identity, str) and len(identity) == 32 and all(c in "0123456789abcdef" for c in identity))
            require(set(blocker) == {"cycle", "count", "legacy_lower_bound"})
            require(all(type(v) is int and v >= 0 for v in blocker.values()))
            require(blocker["cycle"] >= 1)
            counts[identity] = blocker["legacy_lower_bound"]
        for event, report in state["reports"].items():
            require(event.isdigit() and int(event) > 0)
            require(set(report) == {"original_blocker_id", "blocker_id", "counted"})
            require(type(report["counted"]) is bool)
            for field in ("original_blocker_id", "blocker_id"):
                require(report[field] is None or report[field] in counts)
            if report["counted"]:
                counts[report["blocker_id"]] += 1
        cycles = dict.fromkeys(counts, 1)
        for resolution in state.get("resolutions", []):
            identity = resolution["blocker_id"]
            require(resolution["cycle"] == cycles[identity])
            require(type(resolution["count"]) is int and resolution["count"] >= 1)
            counts[identity] -= resolution["count"]
            cycles[identity] += 1
        require(all(state["blockers"][key]["cycle"] == value for key, value in cycles.items()))
        require(all(state["blockers"][key]["count"] == value for key, value in counts.items()))
        require(all(type(event) is int and event > 0 for event in state["corrections"]))
        require(len(set(state["corrections"])) == len(state["corrections"]))
    except (AssertionError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Malformed or unsupported task recovery state") from exc


def _legacy_report(conn, task):
    row = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN "
                       "('blocked','block_loop_detected','dependency_wait') ORDER BY id DESC LIMIT 1",
                       (task["id"],)).fetchone()
    if row is None or row["kind"] not in ("blocked", "block_loop_detected"):
        raise ValueError("Legacy held report unavailable or superseded")
    payload = json.loads(row["payload"] or "{}")
    count = task["block_recurrences"]
    if (not isinstance(payload, dict) or type(count) is not int or count < 1
            or type(payload.get("recurrences")) is not int or payload["recurrences"] != count
            or payload.get("kind") != task["block_kind"]
            or (row["kind"] == "block_loop_detected") != (task["status"] == "triage")):
        raise ValueError("Legacy held counter/report lineage is incomplete or contradictory")
    return row, payload


def _audit_epoch(conn, task_id, state=None, *, after=0, before=None):
    """Replay one epoch's immutable attribution history within event boundaries."""
    reports, corrections, resolutions, enrollments = {}, [], [], []
    for row in conn.execute(
            "SELECT id, kind, payload FROM task_events WHERE task_id=? "
            "AND id > ? AND (? IS NULL OR id < ?) "
            "AND kind IN ('blocked','block_loop_detected','dependency_wait','blocker_classified','blocker_resolved','blocker_legacy_enrolled') ORDER BY id",
            (task_id, after, before, before)):
        try:
            payload = json.loads(row["payload"] or "{}")
            if not isinstance(payload, dict):
                raise ValueError("Malformed recovery audit payload")
            if row["kind"] == "blocker_legacy_enrolled":
                enrollments.append((row["id"], payload))
            elif row["kind"] == "blocker_resolved":
                resolutions.append((row["id"], payload))
            elif row["kind"] == "blocker_classified":
                corrections.append((row["id"], payload))
            elif "blocker_id" in payload:
                reports[str(row["id"])] = (row["kind"], payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("Malformed recovery audit payload") from exc
    if state is None:
        if reports or corrections or resolutions or enrollments:
            raise ValueError("Recovery projection missing for tracked audit history")
        return
    try:
        if enrollments:
            from hermes_cli.kanban_db_recovery_provenance import enrollment_report
            if len(enrollments) != 1 or any(p.get("legacy_seed") is not None for _, p in corrections):
                raise ValueError("Conflicting legacy enrollment seeds")
            event, payload = enrollments[0]
            if any(int(e) < event for e in reports) or any(e < event for e, _ in corrections + resolutions):
                raise ValueError("Legacy enrollment must precede tracked history")
            report_id, report = enrollment_report(conn, task_id, event, payload, after)
            reports[report_id] = report
        for index, (event, payload) in enumerate(corrections):
            seed = payload.get("legacy_seed")
            if seed is None:
                continue
            native = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? AND id=?",
                                  (task_id, payload["block_event_id"])).fetchone()
            if (index != 0 or payload["prior_revision"] != 0 or not native
                    or native["kind"] not in ("blocked", "block_loop_detected")
                    or hashlib.sha256(native["payload"].encode()).hexdigest() != seed["payload_sha256"]
                    or "blocker_id" in json.loads(native["payload"])
                    or json.loads(native["payload"])["recurrences"] != seed["lower_bound"] + 1):
                raise ValueError("Legacy seed disagrees with immutable report")
            identity = payload["from_blocker_id"]
            if state["blockers"][identity]["legacy_lower_bound"] != seed["lower_bound"]:
                raise ValueError("Legacy lower bound disagrees with audit")
            reports[str(payload["block_event_id"])] = (native["kind"],
                {"blocker_id": identity, "blocker_cycle": 1, "blocker_seed": seed["lower_bound"]})
        if set(reports) != set(state["reports"]) or [e for e, _ in corrections] != state["corrections"]:
            raise ValueError("Recovery audit references missing or foreign")
        effective = {}
        for event, (kind, payload) in reports.items():
            report = state["reports"][event]
            identity = report["original_blocker_id"]
            if (payload["blocker_id"] != identity
                    or report["counted"] != (kind != "dependency_wait")):
                raise ValueError("Recovery original report disagrees with audit")
            effective[event] = identity
        for event, payload in corrections:
            target = str(payload["block_event_id"])
            if (target not in effective or int(target) >= event
                    or payload["from_blocker_id"] != effective[target]
                    or payload["to_blocker_id"] not in state["blockers"]
                    or payload["from_blocker_id"] == payload["to_blocker_id"]
                    or payload["revision"] != payload["prior_revision"] + 1
                    or int(target) != max(int(e) for e in reports if int(e) < event)):
                raise ValueError("Recovery correction chain disagrees with audit")
            effective[target] = payload["to_blocker_id"]
        # Keep historical zero-count identities, but never trust projection-only
        # entries (including self-consistent invented legacy lower bounds).
        origins = {payload["blocker_id"] for _, payload in reports.values()}
        origins.update(payload[field] for _, payload in corrections
                       for field in ("from_blocker_id", "to_blocker_id"))
        origins.discard(None)  # dependency-only reports need no blocker identity
        if set(state["blockers"]) != origins:
            raise ValueError("Recovery blocker identities lack audited origin")
        if any(effective[event] != report["blocker_id"] for event, report in state["reports"].items()):
            raise ValueError("Recovery effective attribution disagrees with audit")
        active = state["active_event_id"]
        if active is not None and effective[str(active)] != state["active_blocker_id"]:
            raise ValueError("Recovery active identity disagrees with report")
        # Only the first tracking report can inherit a native legacy budget.
        # Replay from immutable evidence, never from a self-consistent projection.
        counts = dict.fromkeys(state["blockers"], 0)
        for index, event in enumerate(sorted(reports, key=int)):
            payload = reports[event][1]
            if index == 0:
                seed = payload.get("blocker_seed")
                identity = payload["blocker_id"]
                if type(seed) is not int or seed < 0 or (identity is None and seed):
                    raise ValueError("Recovery immutable seed missing or malformed")
                if identity is not None:
                    counts[identity] = seed
            elif "blocker_seed" in payload:
                raise ValueError("Recovery immutable seed appears after first report")
        if any(counts[key] != blocker["legacy_lower_bound"] for key, blocker in state["blockers"].items()):
            raise ValueError("Recovery lower bound disagrees with immutable seed")
        expected_resolutions = [{"event_id": event, **payload["resolution"]} for event, payload in resolutions]
        if state.get("resolutions", []) != expected_resolutions:
            raise ValueError("Recovery resolution projection disagrees with audit")
        resolution_map = dict(resolutions)
        cycles = dict.fromkeys(counts, 1)
        consumed = set()
        correction_map = dict(corrections)
        for event in sorted({int(e) for e in reports} | set(correction_map) | set(resolution_map)):
            if event in resolution_map:
                payload = resolution_map[event]
                resolution = payload["resolution"]
                identity = resolution["blocker_id"]
                refs = resolution["evidence_refs"]
                if (resolution["cycle"] != cycles[identity] or resolution["count"] != counts[identity]
                        or not refs or consumed.intersection(refs)
                        or payload["block_event_id"] >= event
                        or effective.get(str(payload["block_event_id"])) != identity):
                    raise ValueError("Recovery resolution lineage disagrees with audit")
                consumed.update(refs)
                counts[identity] = 0
                cycles[identity] += 1
            elif event in correction_map:
                payload = correction_map[event]
                old, new = payload["from_blocker_id"], payload["to_blocker_id"]
                if payload["cycle"] != cycles[new]:
                    raise ValueError("Recovery correction cycle disagrees with audit")
                if new not in {p["blocker_id"] for _, p in reports.values()} and state["blockers"][new]["legacy_lower_bound"]:
                    raise ValueError("Correction-created identity has invented lower bound")
                counts[old] -= 1
                counts[new] += 1
            else:
                kind, payload = reports[str(event)]
                identity = payload["blocker_id"]
                if payload.get("blocker_cycle") != (cycles[identity] if identity else None):
                    raise ValueError("Recovery report cycle disagrees with audit")
                if kind != "dependency_wait":
                    counts[identity] += 1
                    if "recurrences" in payload and payload["recurrences"] != counts[identity]:
                        raise ValueError("Recovery counter lineage disagrees with native report")
        if any(counts[key] != blocker["count"] for key, blocker in state["blockers"].items()):
            raise ValueError("Recovery counts disagree with audit")
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed recovery audit relationship") from exc


def _audit(conn, task_id, state=None, revision=None):
    """Validate every closed epoch against its committed completion and closure."""
    closures = list(conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id=? AND kind='blocker_cycle_closed' ORDER BY id",
        (task_id,)))
    cycles = state.get("closed_cycles", []) if state else []
    if [row["id"] for row in closures] != [c["closure_event_id"] for c in cycles]:
        raise ValueError("Recovery closure audit references missing or foreign")
    after, previous_revision = 0, 0
    for row, cycle in zip(closures, cycles):
        completed = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND id=?",
            (task_id, cycle["completed_event_id"])).fetchone()
        if (not completed or completed["kind"] != "completed"
                or not after < cycle["completed_event_id"] < row["id"]
                or not previous_revision < cycle["prior_revision"] < revision):
            raise ValueError("Recovery closure completion or revision disagrees with audit")
        expected = {"completed_event_id": cycle["completed_event_id"],
                    "completed_payload_sha256": hashlib.sha256(completed["payload"].encode()).hexdigest(),
                    "state_sha256": hashlib.sha256(_encode(cycle["state"]).encode()).hexdigest(),
                    "prior_revision": cycle["prior_revision"], "revision": cycle["prior_revision"] + 1}
        if json.loads(row["payload"]) != expected:
            raise ValueError("Recovery closed epoch disagrees with immutable audit")
        _audit_epoch(conn, task_id, cycle["state"], after=after, before=row["id"])
        after, previous_revision = row["id"], cycle["prior_revision"]
    _audit_epoch(conn, task_id, _current_epoch(state) if state else None, after=after)


def close_completed_cycle(conn, task_id, completed_event_id):
    """Archive tracked accounting inside native completion's existing transaction.

    Only the successful native completion caller invokes this, after its event.
    A fresh epoch mints new identities on demand; no closed identity is reusable
    for current classification. Untracked completion never creates a projection.
    The append-only closure binds the entire old epoch and completion payload;
    both it and the new projection roll back with the native task/run/event.
    """
    from hermes_cli import kanban_db as kb
    if not conn.in_transaction:
        raise ValueError("Cycle closure requires the native completion transaction")
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    state, revision, legacy = _load(conn, task)
    if legacy or not state["reports"] and not state["blockers"]:
        return
    completed = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? AND id=?",
        (task_id, completed_event_id)).fetchone()
    latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0]
    if task["status"] != "done" or not completed or completed["kind"] != "completed" or latest != completed_event_id:
        raise ValueError("Cycle closure requires the current native completed event")
    epoch = _current_epoch(state)
    payload = {"completed_event_id": completed_event_id,
               "completed_payload_sha256": hashlib.sha256(completed["payload"].encode()).hexdigest(),
               "state_sha256": hashlib.sha256(_encode(epoch).encode()).hexdigest(),
               "prior_revision": revision, "revision": revision + 1}
    closure = kb._append_event(conn, task_id, "blocker_cycle_closed", payload)
    cycles = [*state.get("closed_cycles", []), {"state": epoch,
        "completed_event_id": completed_event_id, "closure_event_id": closure, "prior_revision": revision}]
    fresh = {"version": 2, "active_blocker_id": None, "active_event_id": None,
             "blockers": {}, "reports": {}, "corrections": [], "closed_cycles": cycles}
    _save(conn, task_id, fresh, revision)


def _load(conn, task):
    row = conn.execute("SELECT revision, state_json FROM task_recovery WHERE task_id=?", (task["id"],)).fetchone()
    if row:
        try:
            state = json.loads(row["state_json"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Malformed task recovery state") from exc
        _validate(state)
        if type(row["revision"]) is not int or row["revision"] < 1:
            raise ValueError("Malformed recovery revision")
        _audit(conn, task["id"], state, row["revision"])
        return state, row["revision"], False
    _audit(conn, task["id"])
    return _legacy_state(conn, task), 0, True


def _legacy_state(conn, task):
    state = {"version": 1, "active_blocker_id": None, "active_event_id": None,
             "blockers": {}, "reports": {}, "corrections": []}
    if task["status"] in ("blocked", "triage") and task["block_recurrences"]:
        row, payload = _legacy_report(conn, task)
        # Stable virtual identity: read-only show must not mint changing tokens.
        identity = uuid.uuid5(uuid.NAMESPACE_URL, _encode([task["id"], row["id"], payload])).hex
        state["active_blocker_id"] = identity
        state["active_event_id"] = row["id"]
        state["blockers"][identity] = {"cycle": 1, "count": task["block_recurrences"],
                                      "legacy_lower_bound": task["block_recurrences"] - 1}
        state["reports"][str(row["id"])] = {"original_blocker_id": identity,
                                          "blocker_id": identity, "counted": True}
    return state


def _save(conn, task_id, state, revision):
    _validate(state)
    conn.execute("INSERT INTO task_recovery(task_id,revision,state_json) VALUES(?,?,?) "
                 "ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,state_json=excluded.state_json",
                 (task_id, revision + 1, _encode(state)))


def _observe(conn, task_id):
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise ValueError("Task not found")
    task = dict(task)
    state, revision, legacy = _load(conn, task)
    # Full native rows bind contract, assignment, ownership and parent changes;
    # per-task events avoid unrelated board activity invalidating observations.
    parents = [dict(row) for row in conn.execute(
        "SELECT t.* FROM tasks t JOIN task_links l ON t.id=l.parent_id WHERE l.child_id=? ORDER BY t.id", (task_id,))]
    runs = [dict(row) for row in conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,))]
    latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0]
    token = hashlib.sha256(_encode([task, parents, runs, latest, revision, state]).encode()).hexdigest()
    view = {**state, "revision": revision, "legacy": legacy, "observed_token": token,
            "legacy_lower_bound": int(task["block_recurrences"] or 0) if legacy else 0}
    return task, state, view, runs


def get_recovery_state(conn, task_id):
    """Read-only current counts plus inspectable, completed closed_cycles history.

    Showing a legacy card never creates an identity or row. Closed counts are
    historical, not unresolved recurrence budget for deliberately reopened work.
    """
    if conn.in_transaction:
        return _observe(conn, task_id)[2]
    conn.execute("BEGIN")
    try:
        return _observe(conn, task_id)[2]
    finally:
        conn.rollback()


def prepare_report(conn, task, kind):
    """Called only after native status/run CAS preflight, inside its transaction."""
    state, revision, legacy = _load(conn, task)
    if legacy and int(task["block_recurrences"] or 0):
        _new_blocker(state, int(task["block_recurrences"]))
    if kind != "dependency" and state["active_blocker_id"] is None:
        _new_blocker(state)
    return state, revision


def report_count(state):
    identity = state["active_blocker_id"]
    return state["blockers"][identity]["count"] if identity else 0


def record_report(conn, task_id, state, revision, event_id, kind):
    identity = state["active_blocker_id"]
    counted = kind != "dependency"
    if counted:
        identity = identity or _new_blocker(state)
        state["blockers"][identity]["count"] += 1
    state["active_event_id"] = event_id
    state["reports"][str(event_id)] = {
        "original_blocker_id": identity, "blocker_id": identity, "counted": counted,
    }
    _save(conn, task_id, state, revision)


def recovery_hold(conn, task_id):
    """Status-independent exhausted-identity fence; completed epochs are history."""
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False
    state, _, legacy = _load(conn, dict(row))
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT
    return (report_count(state) >= BLOCK_RECURRENCE_LIMIT
            or (legacy and int(row["block_recurrences"] or 0) >= BLOCK_RECURRENCE_LIMIT))


def _actor():
    from agent.delegation_context import KANBAN_ENV_KEYS, is_delegated_child_process_context
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home, profile_name_for_home
    if is_delegated_child_process_context() or any(key in os.environ for key in KANBAN_ENV_KEYS):
        raise PermissionError("Workers and delegated contexts cannot classify blockers")
    return {"profile": profile_name_for_home(get_hermes_home()),
            "session_id": get_session_env("HERMES_SESSION_ID") or None}


def classify_blocker(conn, task_id, *, observed_token, block_event_id,
                     existing_blocker_id=None, rationale, evidence_refs):
    """Reattribute one exact current report, retaining its immutable original event.

    Authority is native runtime context, not caller-supplied author metadata.
    This cooperative same-UID boundary is not OS confinement or new board RBAC.
    """
    from hermes_cli import kanban_db as kb
    actor = _actor()
    def denied(reason):
        return {"ok": False, "reason": reason}
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000:
        return denied("invalid rationale")
    if (not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 20
            or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 1000 for ref in evidence_refs)):
        return denied("invalid evidence references")
    if type(block_event_id) is not int or not isinstance(observed_token, str):
        return denied("invalid observation")
    if existing_blocker_id is not None and not isinstance(existing_blocker_id, str):
        return denied("invalid blocker identity")
    with kb.write_txn(conn):
        actor = _actor()
        task, state, view, runs = _observe(conn, task_id)
        if observed_token != view["observed_token"] or block_event_id != state["active_event_id"]:
            return denied("stale report or observation")
        if (task["status"] not in ("blocked", "triage")
                or any(task[key] is not None for key in ("claim_lock", "claim_expires", "worker_pid", "current_run_id"))
                or any(run["ended_at"] is None or run["claim_lock"] is not None or run["worker_pid"] is not None for run in runs)):
            return denied("task must be held and unclaimed with no open ownership")
        native_report = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND id=?",
            (task_id, block_event_id),
        ).fetchone()
        latest_report = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id=? AND kind IN "
            "('blocked','block_loop_detected','dependency_wait')", (task_id,),
        ).fetchone()[0]
        if not native_report or native_report["kind"] not in ("blocked", "block_loop_detected") or latest_report != block_event_id:
            return denied("exact native report unavailable or superseded")
        report = state["reports"][str(block_event_id)]
        if not report["counted"]:
            return denied("dependency reports cannot be classified in this slice")
        if existing_blocker_id is not None and existing_blocker_id not in state["blockers"]:
            return denied("unknown blocker identity")
        old = report["blocker_id"]
        if existing_blocker_id == old:
            return denied("report already attributed to this blocker")
        new = existing_blocker_id or _new_blocker(state)
        state["blockers"][old]["count"] -= 1
        state["blockers"][new]["count"] += 1
        state["active_blocker_id"] = new
        report["blocker_id"] = new
        payload = kb.redact_review_value({"block_event_id": block_event_id,
            "from_blocker_id": old, "to_blocker_id": new, "cycle": state["blockers"][new]["cycle"],
            "observed_token": observed_token, "actor": actor,
            "rationale": rationale.strip(), "evidence_refs": evidence_refs,
            "prior_revision": view["revision"], "revision": view["revision"] + 1})
        if view["legacy"]:
            original, _ = _legacy_report(conn, task)
            payload["legacy_seed"] = {"payload_sha256": hashlib.sha256(original["payload"].encode()).hexdigest(),
                                      "lower_bound": state["blockers"][old]["legacy_lower_bound"]}
        correction = kb._append_event(conn, task_id, "blocker_classified", payload)
        state["corrections"].append(correction)
        _save(conn, task_id, state, view["revision"])
        conn.execute("UPDATE tasks SET block_recurrences=? WHERE id=?", (report_count(state), task_id))
        result = get_recovery_state(conn, task_id)
    return {"ok": True, "state": result, "correction_event_id": correction, "released": False}
