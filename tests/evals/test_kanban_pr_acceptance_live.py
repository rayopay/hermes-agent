"""The standalone acceptance probe must not promote delegated children."""
import os
from pathlib import Path
import subprocess
import sys


def test_delegated_probe_refuses_before_fixture_setup(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home / ".hermes"),
               HERMES_DELEGATED_CHILD_CONTEXT="1")
    result = subprocess.run(
        [sys.executable, "-B", str(root / "evals/kanban_pr_acceptance_live.py"),
         str(root), str(tmp_path / "missing-fixtures")],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "authorized top-level test process" in result.stderr
    assert "Traceback" not in result.stderr
    assert not home.exists()
