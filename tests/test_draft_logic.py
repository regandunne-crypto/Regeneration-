"""Run the JavaScript draft-logic tests as part of `pytest`.

The decision logic that caused the draft bug lives in `draft_utils.js`. Node
runs it directly; this wrapper means `pytest` alone is enough to verify the
whole fix, and CI does not silently skip half of it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed; run `node --test tests/` manually")
def test_draft_decision_logic_js():
    result = subprocess.run(
        [NODE, "--test", "tests/test_draft_logic.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_frontend_files_parse():
    """A syntax error in app.js breaks the entire app and nothing else catches it."""
    for name in ("app.js", "draft_utils.js"):
        result = subprocess.run([NODE, "--check", name], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, f"{name} has a syntax error:\n{result.stderr}"
