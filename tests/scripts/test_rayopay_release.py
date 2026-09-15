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
