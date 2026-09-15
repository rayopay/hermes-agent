#!/usr/bin/env python3
"""Prepare reviewed Hermes releases without changing the installed runtime.

This is an operator tool, not a replacement updater or an approval authority.
Preparation records immutable inputs and preserves conflicts. Deployment is a
separate operation; a clean Git merge is never a release-acceptance verdict.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

FORK = "https://github.com/rayopay/hermes-agent.git"
UPSTREAM = "https://github.com/NousResearch/hermes-agent.git"


class Refusal(RuntimeError):
    """An unmet prerequisite; preserve evidence and do not guess a recovery."""


def sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise Refusal("Expected an exact lowercase 40-character Git SHA")
    return value


def exact_path(value: str | Path, *, exists: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute() or path != path.resolve():
        raise Refusal("Use an absolute canonical path without symlink ancestors")
    if exists and not path.exists():
        raise Refusal("Required path is absent")
    return path


def run(argv: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    # No shell interpolation. Hooks cannot execute incidental host code during
    # preparation; this does not change native Hermes approval configuration.
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args], cwd=repo)
    if check and p.returncode:
        # Do not echo arbitrary remote diagnostics, which may contain credentials.
        raise Refusal(f"Git {args[0]} failed (exit {p.returncode})")
    return p.stdout.strip()


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".pending")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def identity(repo: Path) -> dict:
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    if root != repo:
        raise Refusal("Path is not the exact checkout root")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise Refusal("Checkout is dirty; preserve it rather than stashing or resetting")
    return {"path": str(root), "head": sha(git(repo, "rev-parse", "HEAD")),
            "common_dir": str(Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())}


def assert_ancestor(repo: Path, ancestor: str, head: str) -> None:
    p = run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha(ancestor), sha(head)], cwd=repo)
    if p.returncode:
        raise Refusal("Candidate would omit the installed revision or ancestry is unavailable")


def prepare(repo: Path, installed: Path, operation: Path, upstream_sha: str,
            downstream_sha: str, upstream_source: str) -> dict:
    repo, installed = exact_path(repo), exact_path(installed)
    operation = exact_path(operation, exists=False)
    sha(upstream_sha); sha(downstream_sha)
    source = identity(repo)
    deployed = identity(installed)
    if source["common_dir"] == deployed["common_dir"]:
        raise Refusal("Preparation must not allocate metadata in the installed repository")
    if any(operation == root or operation.is_relative_to(root) for root in (repo, installed)):
        raise Refusal("Operation directory must be outside source and installed checkouts")
    if not operation.parent.is_dir() or operation.exists():
        raise Refusal("Operation must be a new directory under an existing canonical parent")
    if upstream_source != UPSTREAM:
        upstream_path = exact_path(upstream_source)
        # The local installed checkout is a read-only fetch source, not a writer.
        if identity(upstream_path)["head"] != upstream_sha:
            raise Refusal("Local upstream source does not match the requested SHA")
    if git(repo, "remote", "get-url", "origin") != FORK:
        raise Refusal("Preparation checkout must belong to the Rayopay fork")
    git(repo, "cat-file", "-e", downstream_sha + "^{commit}")
    operation.mkdir(mode=0o700)
    receipt = {"schema": 1, "status": "preparing", "source": source, "installed": deployed,
               "upstream_sha": upstream_sha, "downstream_sha": downstream_sha,
               "candidate": str(operation / "candidate"), "started_at": time.time(),
               "tests": "not run", "review": "not approved", "deployment": "not authorized"}
    atomic_json(operation / "release.json", receipt)
    # A common-directory lock serializes this wrapper's metadata operations.
    # Git's own locks still protect against independent Git callers.
    lock_path = Path(source["common_dir"]) / "rayopay-release.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            git(repo, "fetch", "--no-tags", upstream_source, upstream_sha)
            assert_ancestor(repo, deployed["head"], upstream_sha)
            candidate = operation / "candidate"
            git(repo, "worktree", "add", "--detach", str(candidate), downstream_sha)
            git(repo, "worktree", "lock", "--reason", "Retained reviewed-release candidate", str(candidate))
            merged = run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Rayopay Hermes maintenance",
                          "-c", "user.email=noreply@rayopay.io", "merge", "--no-ff", "--no-edit", upstream_sha], cwd=candidate)
            (operation / "merge.log").write_text(merged.stdout + merged.stderr)
            if merged.returncode:
                receipt.update(status="blocked", reason="merge failed; candidate and conflict evidence retained",
                               conflict_paths=git(candidate, "diff", "--name-only", "--diff-filter=U").splitlines())
            else:
                combined = identity(candidate)
                assert_ancestor(candidate, deployed["head"], combined["head"])
                assert_ancestor(candidate, downstream_sha, combined["head"])
                receipt.update(status="prepared-not-qualified", combined=combined,
                               changed_from_installed=git(candidate, "diff", "--name-only", deployed["head"], combined["head"]).splitlines())
        except Exception as exc:
            receipt.update(status="blocked", reason=type(exc).__name__)
            atomic_json(operation / "release.json", receipt)
            raise
        atomic_json(operation / "release.json", receipt)
    return receipt


def load_release(operation: Path) -> dict:
    operation = exact_path(operation)
    path = exact_path(operation / "release.json")
    result = json.loads(path.read_text())
    if result.get("schema") != 1 or result.get("status") != "prepared-not-qualified":
        raise Refusal("A successfully prepared candidate is required")
    candidate = exact_path(result["candidate"])
    if candidate != operation / "candidate" or identity(candidate) != result["combined"]:
        raise Refusal("Candidate identity changed; prepare and review again")
    return result


def bundle(operation: Path) -> dict:
    """Produce a prerequisite bundle; this is transport, never test acceptance.

    Only commits missing from the recorded installed revision are included. The
    installed checkout must already contain the prerequisite. That keeps the
    transport bounded and allows exact-commit, network-independent Git testing.
    """
    release = load_release(operation)
    candidate = Path(release["candidate"])
    head = release["combined"]["head"]
    branch = "rayopay-release/" + head
    ref = "refs/heads/" + branch
    dest = operation / "release.bundle"
    if dest.exists():
        raise Refusal("Bundle already exists; never overwrite release evidence")
    # expected-old=all-zero makes this create-only; never move an existing ref.
    git(candidate, "update-ref", ref, head, "0" * 40)
    git(candidate, "bundle", "create", str(dest), ref, "^" + release["installed"]["head"])
    dest.chmod(0o400)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    result = {"schema": 1, "sha": head, "branch": branch, "bundle": str(dest),
              "sha256": digest, "prerequisite": release["installed"]["head"],
              "qualification": "not established by bundle creation"}
    atomic_json(operation / "bundle.json", result)
    return result


def pinned_git_env(origin: str, bundle_path: Path, inherited: dict[str, str]) -> dict[str, str]:
    """Route one origin through immutable transport without editing Git config.

    remote.origin.url is multi-valued: overriding it can leave the persistent
    first URL effective. An insteadOf rule changes actual URL resolution. Refuse
    inherited config injection rather than silently composing competing rules.
    """
    bundle_path = exact_path(bundle_path)
    if not bundle_path.is_file() or not origin or any(c in origin for c in "\r\n"):
        raise Refusal("Invalid bundle transport")
    if any(k.startswith("GIT_CONFIG") for k in inherited):
        raise Refusal("Existing Git config environment requires explicit reconciliation")
    return {**inherited, "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{bundle_path}.insteadOf", "GIT_CONFIG_VALUE_0": origin,
            "GIT_TERMINAL_PROMPT": "0"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("prepare", help="Combine pinned upstream/downstream in a retained worktree; never install")
    for name in ("repo", "installed", "operation", "upstream-sha", "downstream-sha"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--upstream-source", default=UPSTREAM)
    p = subs.add_parser("bundle", help="Export exact candidate transport; does not approve or deploy")
    p.add_argument("--operation", type=Path, required=True)
    p = subs.add_parser("status", help="Read the preparation receipt without promoting it")
    p.add_argument("--operation", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(Path(args.repo), Path(args.installed), Path(args.operation),
                             args.upstream_sha, args.downstream_sha, args.upstream_source)
        elif args.command == "bundle":
            result = bundle(exact_path(args.operation))
        else:
            result = json.loads(exact_path(args.operation / "release.json").read_text())
        print(json.dumps(result, indent=2))
        return 2 if result.get("status") == "blocked" else 0
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc) if isinstance(exc, Refusal) else type(exc).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
