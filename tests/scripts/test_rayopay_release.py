"""Real disposable Git proofs for the release-preparation operator tool.

These verify orchestration/transport, not Hermes runtime or deployment acceptance.
"""
import importlib.util
import hashlib
import json
import os
import sys
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location("rayopay_release", Path(__file__).resolve().parents[2] / "scripts/rayopay_release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


def commit(path, name, text):
    (path / name).write_text(text)
    git(path, "add", "--", name)
    git(path, "commit", "-qm", "Disposable fixture change")
    return git(path, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch):
    """Isolate Git inputs only; retain Hermes execution-authority markers."""
    for key in tuple(os.environ):
        if key.startswith("GIT_CONFIG") or key in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SHALLOW_FILE",
        }:
            monkeypatch.delenv(key)


@pytest.fixture
def trees(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    git(installed, "init", "-q", "--initial-branch=main")
    git(installed, "config", "user.name", "Fixture")
    git(installed, "config", "user.email", "fixture@example.invalid")
    commit(installed, "shared.txt", "base\n")
    source = tmp_path / "fork"
    subprocess.run(["git", "clone", "-q", str(installed), str(source)], check=True)
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "remote", "set-url", "origin", release.FORK)
    downstream = commit(source, "shared.txt", "downstream fix\n")
    upstream = commit(installed, "upstream.txt", "new official feature\n")
    return source, installed, downstream, upstream


def prepare(trees, operation):
    source, installed, downstream, upstream = trees
    return release.prepare(source, installed, operation, upstream, downstream, str(installed))


def test_prepare_preserves_both_histories_and_bundle_pins_real_fetch(trees, tmp_path):
    """A real merge includes both inputs; a bundle fetch cannot follow later remote drift."""
    source, installed, downstream, upstream = trees
    source_before = release.identity(source)
    installed_before = release.identity(installed)
    op = tmp_path / "operation"
    receipt = prepare(trees, op)
    assert receipt["status"] == "prepared-not-qualified"
    assert receipt["tests"] == "not run" and receipt["deployment"] == "not authorized"
    candidate = Path(receipt["candidate"])
    assert (candidate / "shared.txt").read_text() == "downstream fix\n"
    assert (candidate / "upstream.txt").read_text() == "new official feature\n"
    assert release.identity(source) == source_before
    assert release.identity(installed) == installed_before
    transport = release.bundle(op)
    git(installed, "bundle", "verify", transport["bundle"])
    git(installed, "remote", "add", "origin", str(source))
    later = commit(source, "later.txt", "not part of approved candidate\n")
    # Move the SAME advertised release ref, not merely main: otherwise a fetch
    # from the wrong source can coincidentally return the approved SHA.
    git(source, "update-ref", "refs/heads/" + transport["branch"], later)
    env = release.pinned_git_env(str(source), Path(transport["bundle"]), dict(os.environ))
    resolved = subprocess.check_output(["git", "-C", str(installed), "remote", "get-url", "origin"], env=env, text=True).strip()
    assert resolved == transport["bundle"]
    subprocess.run(["git", "-C", str(installed), "fetch", "origin", transport["branch"]], env=env, check=True, capture_output=True)
    assert git(installed, "rev-parse", "FETCH_HEAD") == transport["sha"]
    assert git(installed, "rev-parse", "refs/remotes/origin/" + transport["branch"]) == transport["sha"]
    assert git(installed, "remote", "get-url", "origin") == str(source)
    assert release.identity(installed) == installed_before
    with pytest.raises(release.Refusal, match="already exists"):
        release.bundle(op)


def test_conflict_is_retained_and_never_promoted(trees, tmp_path):
    """A conflicted merge leaves source and installation untouched and refuses export."""
    source, installed, downstream, _ = trees
    upstream = commit(installed, "shared.txt", "conflicting upstream\n")
    before = (release.identity(source), release.identity(installed))
    op = tmp_path / "conflict"
    result = prepare((source, installed, downstream, upstream), op)
    assert result["status"] == "blocked" and result["conflict_paths"] == ["shared.txt"]
    assert git(op / "candidate", "diff", "--name-only", "--diff-filter=U") == "shared.txt"
    assert (release.identity(source), release.identity(installed)) == before
    with pytest.raises(release.Refusal, match="successfully prepared"):
        release.bundle(op)


@pytest.mark.parametrize("case", ["malformed_sha", "same_store", "existing_operation", "dirty_source", "symlink", "omitted_installed"])
def test_preparation_refuses_unsafe_inputs_before_candidate_write(trees, tmp_path, case):
    """Bad identity, dirty state and unsafe destinations cannot become release candidates."""
    source, installed, downstream, upstream = trees
    op = tmp_path / "operation"
    if case == "malformed_sha":
        upstream = upstream.upper()
    elif case == "same_store":
        source = installed
    elif case == "existing_operation":
        op.mkdir()
    elif case == "dirty_source":
        (source / "untracked.txt").write_text("preserve me")
    elif case == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif case == "omitted_installed":
        upstream = git(installed, "rev-parse", "HEAD^")
    with pytest.raises(release.Refusal):
        release.prepare(source, installed, op, upstream, downstream, str(installed))
    assert not (op / "candidate").exists()


def test_changed_candidate_cannot_be_bundled(trees, tmp_path):
    """A new candidate commit invalidates the recorded release identity."""
    op = tmp_path / "operation"
    result = prepare(trees, op)
    candidate = Path(result["candidate"])
    commit(candidate, "changed.txt", "unreviewed\n")
    with pytest.raises(release.Refusal, match="identity changed"):
        release.bundle(op)
    assert not (op / "release.bundle").exists()


def file_snapshot(root):
    """Compare actual fixture bytes/modes, including Git index/config/refs."""
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


def test_verify_cli_is_read_only_and_does_not_grant_deployment(trees, tmp_path):
    """Exercise the public CLI, including real bundle verification without fetching."""
    _, installed, _, _ = trees
    git(installed, "remote", "add", "origin", release.UPSTREAM)
    op = tmp_path / "operation"
    prepare(trees, op)
    transport = release.bundle(op)
    before = file_snapshot(tmp_path)
    result = subprocess.run([sys.executable, "-B", str(SPEC.origin), "verify", "--operation", str(op),
                             "--expected-sha", transport["sha"], "--expected-origin", release.UPSTREAM,
                             "--expected-branch", "main"], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "snapshot-verified-not-deployable"
    assert report["deployment"] == "not authorized"
    assert report["remaining_gates"]
    assert file_snapshot(tmp_path) == before


@pytest.mark.parametrize("case", ["sha", "dirty", "head", "origin", "branch", "bundle_bytes",
                                    "manifest_sha", "prerequisite", "extra_ref", "refspec",
                                    "symlink", "config_env", "git_dir_env", "malformed_manifest"])
def test_verify_refuses_drift_without_modification(trees, tmp_path, monkeypatch, case):
    """Corruption or drift is a refusal, never a repair or an implicit source switch."""
    _, installed, _, _ = trees
    git(installed, "remote", "add", "origin", release.UPSTREAM)
    op = tmp_path / "operation"
    prepare(trees, op)
    transport = release.bundle(op)
    expected = transport["sha"]
    bundle_path = Path(transport["bundle"])
    if case == "sha":
        expected = "0" * 40
    elif case == "dirty":
        (installed / "untracked.txt").write_text("preserve")
    elif case == "head":
        commit(installed, "advance.txt", "new version")
    elif case == "origin":
        git(installed, "remote", "set-url", "origin", release.FORK)
    elif case == "branch":
        git(installed, "switch", "-qc", "other")
    elif case == "bundle_bytes":
        bundle_path.chmod(0o600)
        with bundle_path.open("ab") as stream:
            stream.write(b"corruption")
    elif case == "manifest_sha":
        transport["sha"] = "0" * 40
    elif case == "prerequisite":
        transport["prerequisite"] = "0" * 40
    elif case == "extra_ref":
        candidate = op / "candidate"
        git(candidate, "update-ref", "refs/heads/extra", transport["sha"])
        bundle_path.chmod(0o600)
        git(candidate, "bundle", "create", str(bundle_path), "refs/heads/" + transport["branch"],
            "refs/heads/extra", "^" + transport["prerequisite"])
        bundle_path.chmod(0o400)
        transport["sha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    elif case == "refspec":
        git(installed, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/elsewhere/*")
    elif case == "symlink":
        real = op / "moved.bundle"
        bundle_path.rename(real)
        bundle_path.symlink_to(real)
    elif case == "config_env":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    elif case == "git_dir_env":
        monkeypatch.setenv("GIT_DIR", str(installed / ".git"))
    (op / "bundle.json").write_text(json.dumps(transport if case != "malformed_manifest" else []))
    before = file_snapshot(tmp_path)
    with pytest.raises(release.Refusal):
        release.verify(op, expected, release.UPSTREAM, "main")
    assert file_snapshot(tmp_path) == before


@pytest.mark.parametrize("command", ["prepare", "bundle", "verify"])
@pytest.mark.parametrize("override", ["GIT_CONFIG_COUNT", "GIT_INDEX_FILE", "GIT_DIR"])
def test_cli_rejects_git_overrides_before_any_mutation(trees, tmp_path, command, override):
    """Each public Git-using entry rejects inherited identity/config overrides."""
    source, installed, downstream, upstream = trees
    op = tmp_path / "operation"
    if command != "prepare":
        prepare(trees, op)
    if command == "verify":
        transport = release.bundle(op)
    args = [sys.executable, "-B", str(SPEC.origin), command, "--operation", str(op)]
    if command == "prepare":
        args += ["--repo", str(source), "--installed", str(installed),
                 "--upstream-sha", upstream, "--downstream-sha", downstream,
                 "--upstream-source", str(installed)]
    elif command == "verify":
        args += ["--expected-sha", transport["sha"], "--expected-origin", release.UPSTREAM,
                 "--expected-branch", "main"]
    value = {"GIT_CONFIG_COUNT": "0", "GIT_INDEX_FILE": str(tmp_path / "alternate-index"),
             "GIT_DIR": str(installed / ".git")}[override]
    before = file_snapshot(tmp_path)
    result = subprocess.run(args, text=True, capture_output=True, env={**os.environ, override: value})
    assert result.returncode == 2, result.stdout + result.stderr
    assert json.loads(result.stdout)["reason"] == "Inherited Git overrides require explicit reconciliation"
    assert file_snapshot(tmp_path) == before


@pytest.mark.parametrize("preserve_installed", [True, False])
def test_next_fork_release_checks_combined_ancestry(trees, tmp_path, preserve_installed):
    """A later release retains the installed fork merge, not just upstream ancestry."""
    source, installed, _, _ = trees
    official = tmp_path / "official"
    subprocess.run(["git", "clone", "-q", str(installed), str(official)], check=True)
    git(official, "config", "user.name", "Fixture")
    git(official, "config", "user.email", "fixture@example.invalid")
    first = prepare(trees, tmp_path / "first")
    merged = first["combined"]["head"]
    for target in (installed, source):
        git(target, "fetch", first["candidate"], merged)
        git(target, "checkout", "--detach", merged)
    downstream = commit(source, "next-fix.txt", "next downstream change\n")
    upstream = commit(official, "next-upstream.txt", "next official change\n")
    if not preserve_installed:
        commit(installed, "must-preserve.txt", "installed-only commit\n")
    before = file_snapshot(installed)
    op = tmp_path / "second"
    if preserve_installed:
        result = release.prepare(source, installed, op, upstream, downstream, str(official))
        assert result["status"] == "prepared-not-qualified"
        candidate = Path(result["candidate"])
        for ancestor in (merged, downstream, upstream):
            release.assert_ancestor(candidate, ancestor, result["combined"]["head"])
    else:
        with pytest.raises(release.Refusal, match="omit the installed revision"):
            release.prepare(source, installed, op, upstream, downstream, str(official))
        assert json.loads((op / "release.json").read_text())["status"] == "blocked"
    assert file_snapshot(installed) == before


@pytest.mark.parametrize("command", ["bundle", "status"])
@pytest.mark.parametrize("value", [[], None, "not-an-object"])
def test_cli_refuses_non_object_receipts(tmp_path, command, value):
    op = tmp_path / "operation"
    op.mkdir()
    (op / "release.json").write_text(json.dumps(value), encoding="utf-8")
    before = file_snapshot(op)
    result = subprocess.run(
        [sys.executable, str(release.__file__), command, "--operation", str(op)],
        text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "refused"
    assert "Traceback" not in result.stderr
    assert file_snapshot(op) == before


@pytest.mark.parametrize("value, expected_exit", [
    ({}, 2),
    ({"schema": 1}, 2),
    ({"schema": 2, "status": "preparing"}, 2),
    ({"schema": True, "status": "preparing"}, 2),
    ({"schema": 1.0, "status": "preparing"}, 2),
    ({"schema": 1, "status": []}, 2),
    ({"schema": 1, "status": "deployed"}, 2),
    ({"schema": 1, "status": "preparing"}, 0),
    ({"schema": 1, "status": "blocked"}, 2),
    ({"schema": 1, "status": "prepared-not-qualified"}, 0),
])
def test_status_validates_receipt_envelope_without_promoting_it(tmp_path, value, expected_exit):
    op = tmp_path / "operation"
    op.mkdir()
    (op / "release.json").write_text(json.dumps(value), encoding="utf-8")
    before = file_snapshot(op)
    result = subprocess.run(
        [sys.executable, "-B", str(release.__file__), "status", "--operation", str(op)],
        text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert result.returncode == expected_exit
    valid = type(value.get("schema")) is int and value.get("schema") == 1 and value.get("status") in (
        "preparing", "blocked", "prepared-not-qualified",
    )
    assert json.loads(result.stdout) == (value if valid else {
        "status": "refused", "reason": "Malformed release evidence",
    })
    assert "Traceback" not in result.stderr
    assert file_snapshot(op) == before


@pytest.mark.parametrize("lock_failure", ["contended", "directory"])
def test_prepare_records_lock_failure_as_blocked(trees, tmp_path, lock_failure):
    import fcntl
    from contextlib import ExitStack
    source, installed, downstream, upstream = trees
    lock_path = source / ".git" / "rayopay-release.lock"
    op = tmp_path / "blocked-operation"
    with ExitStack() as stack:
        if lock_failure == "contended":
            lock = stack.enter_context(lock_path.open("a", encoding="utf-8"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            lock_path.mkdir()
        before = file_snapshot(installed)
        result = subprocess.run(
            [sys.executable, str(release.__file__), "prepare",
             "--repo", str(source), "--installed", str(installed), "--operation", str(op),
             "--upstream-sha", upstream, "--downstream-sha", downstream,
             "--upstream-source", str(installed)],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        assert result.returncode == 2
        assert json.loads(result.stdout)["status"] == "refused"
        receipt = json.loads((op / "release.json").read_text(encoding="utf-8"))
        assert receipt["status"] == "blocked"
        assert receipt["reason"] == ("BlockingIOError" if lock_failure == "contended" else "IsADirectoryError")
        assert not (op / "candidate").exists()
        assert file_snapshot(installed) == before


@pytest.mark.parametrize("location", ["source", "installed", "upstream"])
def test_prepare_cli_preserves_every_supplied_checkout(trees, tmp_path, location):
    """Preparation refuses destinations inside any input before writing anything."""
    source, installed, downstream, upstream = trees
    official = tmp_path / "official"
    subprocess.run(["git", "clone", "-q", str(installed), str(official)], check=True)
    op = {"source": source, "installed": installed, "upstream": official}[location] / "operation"
    before = file_snapshot(tmp_path)
    result = subprocess.run(
        [sys.executable, "-B", str(release.__file__), "prepare", "--repo", str(source),
         "--installed", str(installed), "--operation", str(op), "--upstream-sha", upstream,
         "--downstream-sha", downstream, "--upstream-source", str(official)],
        text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert file_snapshot(tmp_path) == before
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "refused"
    assert "outside" in json.loads(result.stdout)["reason"]
    assert not op.exists()


@pytest.mark.parametrize("field", ["candidate", "combined"])
@pytest.mark.parametrize("damage", ["missing", "null", "wrong_type"])
def test_bundle_cli_refuses_incomplete_candidate_evidence(trees, tmp_path, field, damage):
    """Malformed prepared receipts are read-only refusals, never tracebacks."""
    op = tmp_path / "operation"
    receipt = prepare(trees, op)
    if damage == "missing":
        del receipt[field]
    else:
        receipt[field] = None if damage == "null" else 42
    (op / "release.json").write_text(json.dumps(receipt), encoding="utf-8")
    before = file_snapshot(tmp_path)
    result = subprocess.run(
        [sys.executable, "-B", str(release.__file__), "bundle", "--operation", str(op)],
        text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert file_snapshot(tmp_path) == before
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "refused"
    assert "Traceback" not in result.stderr
    assert not (op / "release.bundle").exists()
