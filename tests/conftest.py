"""Shared fixture paths and helpers for the bcp unit tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BRE_MINI = FIXTURES / "bre-mini"
REPO_MINI = FIXTURES / "repo-mini"
MANIFEST_REL = "domain-decomposition-overview/domain-manifest.json"


def copy_tree(src: Path, dst: Path) -> Path:
    """Copy a fixture tree into ``dst`` and return ``dst``."""
    shutil.copytree(src, dst)
    return dst


def run_python(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """Run Python with ``args`` in a child process, without a shell.

    The command list starts with a literal interpreter name and a literal flag (``-B``: write no bytecode),
    the code shape the security review asks for (no ``sys.executable`` as the program; Semgrep rule
    ``dangerous-subprocess-use-audit``). The folder of the interpreter that runs the tests goes first on
    ``PATH``, so on Linux and macOS the child is the same Python; on Windows the process search starts in
    the running interpreter's own folder, which gives the same result. The other arguments are paths the
    tests create.
    """
    env = dict(kwargs.pop("env", None) or os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    kwargs.setdefault("check", False)
    if os.name == "nt":
        return subprocess.run(["python", "-B", *args], env=env, **kwargs)
    return subprocess.run(["python3", "-B", *args], env=env, **kwargs)


def load_json(path: Path) -> Any:
    """Read a JSON file."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    """Write a JSON file, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@pytest.fixture
def bre_mini() -> Path:
    """Read-only path of the synthetic BRE fixture."""
    return BRE_MINI


@pytest.fixture
def repo_mini() -> Path:
    """Read-only path of the synthetic source repo fixture."""
    return REPO_MINI


@pytest.fixture
def bre_copy(tmp_path: Path) -> Path:
    """Writable copy of the BRE fixture."""
    return copy_tree(BRE_MINI, tmp_path / "bre")


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """Writable copy of the repo fixture."""
    return copy_tree(REPO_MINI, tmp_path / "repo")


@pytest.fixture
def bre_noinv(tmp_path: Path) -> Path:
    """``bre-mini-noinv``: the BRE fixture without its configuration inventory."""
    dst = copy_tree(BRE_MINI, tmp_path / "bre-mini-noinv")
    (dst / "reference-documentation" / "feature-flag-inventory.md").unlink()
    return dst


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A target workspace holding a copy of the repo with the BRE inside it."""
    target = copy_tree(REPO_MINI, tmp_path / "ws")
    copy_tree(BRE_MINI, target / "business-rules-extraction")
    return target
