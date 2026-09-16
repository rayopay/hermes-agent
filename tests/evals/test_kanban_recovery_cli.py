"""Launcher rejection only: never execute or simulate the owner lifecycle."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.linux_only

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def foreign_repo():
    # An ordinary Git checkout, not a worktree of the pinned fleet repository.
    # Keep it outside real Hermes state and retain it for failure inspection.
    repo = Path(tempfile.mkdtemp(prefix="kq-foreign-", dir="/tmp"))
    (repo / "evals").mkdir()
    shutil.copyfile(REPO / "evals/kanban_recovery_cli.py", repo / "evals/kanban_recovery_cli.py")
    for args in (("init",), ("add", "evals/kanban_recovery_cli.py"),
                 ("-c", "user.name=Launcher test", "-c", "user.email=launcher@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-m", "Disposable rejection fixture")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    assert (repo / ".git").is_dir()
    assert not (repo / ".venv").exists()
    return repo


@pytest.fixture(params=["current", "foreign"])
def launcher_repo(request):
    return REPO if request.param == "current" else request.getfixturevalue("foreign_repo")


@pytest.mark.parametrize("case, message", [
    ("missing", "--expected-head"),
    ("short", "literal 40-character"),
    ("whitespace", "literal 40-character"),
    ("uppercase", "literal 40-character"),
    ("mismatch", "unexpected HEAD"),
    ("unknown", "invalid choice"),
    ("empty", "explicit existing test .py files"),
    ("owner", "owner proof blocked by inherited authority"),
])
def test_launcher_rejects_before_lifecycle(launcher_repo, case, message):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=launcher_repo, text=True).strip()
    sha = {"short": head[:12], "whitespace": head + " ", "uppercase": head.upper(),
           "mismatch": "0" * 40}.get(case, head)
    mode = {"unknown": "not-a-mode", "empty": "tests", "owner": "owner"}.get(case, "refusal")
    args = [mode] if case == "missing" else ["--expected-head", sha, mode]
    # Preserve every inherited authority key/value. Add a restrictive marker only
    # when the canonical unit environment has none; never construct owner authority.
    env = dict(os.environ)
    env.setdefault("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    result = subprocess.run([sys.executable,
                             str(launcher_repo / "evals/kanban_recovery_cli.py"), *args],
                            cwd=launcher_repo, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr
    # No scratch-path, lifecycle-result or runner output; preallocation ordering
    # is independently reviewed, not inferred from an unrelated tmp_path.
    assert not result.stdout
    assert "Traceback" not in result.stderr


def test_foreign_repository_still_rejects_valid_refusal(foreign_repo):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=foreign_repo, text=True).strip()
    env = dict(os.environ)
    env.setdefault("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    result = subprocess.run([sys.executable, str(foreign_repo / "evals/kanban_recovery_cli.py"),
                             "--expected-head", head, "refusal"],
                            cwd=foreign_repo, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "unexpected repository" in result.stderr
    assert "Traceback" not in result.stderr
    assert not result.stdout  # no accepted lifecycle or scratch-path output
