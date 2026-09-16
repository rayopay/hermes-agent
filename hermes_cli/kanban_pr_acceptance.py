"""Exact-head reported CI for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.
This is a snapshot of reported evidence, not discovery of expected checks.
"""
from __future__ import annotations

import json
import re
import subprocess

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, paginate: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com", "--method", "GET"]
    if paginate:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def _pr_identity(pr: dict) -> tuple[str, str, str, bool]:
    sha, branch, state, merged = pr["head"]["sha"], pr["base"]["ref"], pr["state"], pr["merged"]
    if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not isinstance(branch, str) or not branch or type(merged) is not bool
            or state not in {"open", "closed"} or merged != (state == "closed")):
        raise ValueError("PR is closed or current identity is unavailable")
    return sha, branch, state, merged


def _page_items(pages, field: str | None = None) -> list[dict]:
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing pagination envelope")
    items = []
    for page in pages:
        rows = page[field] if field else page
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Malformed evidence page")
        items.extend(rows)
    return items


def _latest_checks(runs: list[dict], statuses: list[dict], sha: str) -> list[dict]:
    latest = {}
    for is_run, checks in ((True, runs), (False, statuses)):
        for check in checks:
            name = check["name"] if is_run else check["context"]
            app = check["app"]["id"] if is_run else None
            suite = check["check_suite"]["id"] if is_run else None
            if (not isinstance(name, str) or not name or type(check["id"]) is not int or check["id"] <= 0
                    or (is_run and (type(app) is not int or app <= 0 or type(suite) is not int or suite <= 0))):
                raise ValueError("Incomplete check identity")
            # Never merge Apps, suites or legacy statuses sharing a check name.
            key = (is_run, name, app, suite)
            outcome = check.get("conclusion") if is_run else check.get("state")
            # The legacy statuses endpoint is SHA-scoped but has no SHA field.
            head = check.get("head_sha") if is_run else check.get("sha", sha)
            evidence = {"type": "check_run" if is_run else "status", "name": name,
                        "id": check["id"], "app_id": app, "check_suite_id": suite,
                        "url": check.get("html_url") or check.get("target_url"),
                        "head_sha": head, "status": check.get("status") if is_run else outcome,
                        "conclusion": outcome,
                        "classification": _classify(head, sha, outcome, is_run, check.get("status"))}
            if head != sha:
                raise ValueError("Evidence belongs to another head")
            if key not in latest or check["id"] > latest[key]["id"]:
                latest[key] = evidence
    return list(latest.values())


def collect_acceptance(contract: str, published_pr: str | None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "base_ref": None, "pr_url": published_pr, "checks": [],
               "acceptance_basis": "reported-ci",
               "recovery": "Inspect reported check URLs, fix failures, rerun CI or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    phase = "PR contract"
    try:
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        phase = "PR identity"
        identity = _pr_identity(_api(f"repos/{repo}/pulls/{number}"))
        sha, branch, state, merged = identity
        receipt.update(head_sha=sha, base_ref=branch, pr_state=state, merged=merged)
        phase = "check runs"
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", paginate=True)
        runs = _page_items(pages, "check_runs")
        if (len({r["id"] for r in runs}) != len(runs)
                or any(type(p["total_count"]) is not int or p["total_count"] != len(runs) for p in pages)):
            raise ValueError("Incomplete or changing check-run pagination")
        phase = "legacy statuses"
        statuses = _page_items(_api(f"repos/{repo}/commits/{sha}/statuses?per_page=100", paginate=True))
        # Overlapping pages are not status history: equal IDs must never let
        # whichever row arrived first hide conflicting or changing evidence.
        if len({s["id"] for s in statuses}) != len(statuses):
            raise ValueError("Duplicate legacy status IDs in pagination")
        phase = "reported check evidence"
        receipt["checks"] = _latest_checks(runs, statuses, sha)
        # Re-read after all pages: old-head/base/state successes are not transferable.
        phase = "final PR identity"
        if _pr_identity(_api(f"repos/{repo}/pulls/{number}")) != identity:
            receipt.update(classification="stale", detail="PR head/base/state changed while collecting evidence; retry.")
            return receipt
        outcomes = [c["classification"] for c in receipt["checks"]]
        receipt["classification"] = next((x for x in outcomes if x not in {"success", "non_blocking"}),
                                          "success" if "success" in outcomes else "missing")
        receipt["ok"] = receipt["classification"] == "success"
        receipt["detail"] = ("All reported CI is successful or completed skipped/neutral, with at least one success."
                             if receipt["ok"] else "Reported CI must have at least one success and no blocking outcomes.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist exceptions or gh stderr: either can contain credentials.
        receipt.update(classification="infra", detail=f"GitHub {phase} unavailable or incomplete. "
                       "Check gh version (2.48+), repository read access and API availability, then retry.")
        return receipt


def _classify(head: str, sha: str, outcome: str | None, is_run: bool, status: str | None) -> str:
    if head != sha:
        return "stale"
    if is_run and status != "completed":
        return "pending"
    if is_run and outcome in {"skipped", "neutral"}:
        return "non_blocking"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
