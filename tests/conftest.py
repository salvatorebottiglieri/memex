"""Shared fixtures for memex tests."""
import subprocess
import sys
from pathlib import Path

import pytest


def _run_memex(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run the memex CLI and return the completed process."""
    return subprocess.run(
        [sys.executable, "-m", "memex.cli"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


@pytest.fixture
def run_memex():
    """Fixture that provides the run_memex helper."""
    return _run_memex
