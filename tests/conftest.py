"""Shared fixtures for the composite action script tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

ACTIONS_DIR = Path(__file__).resolve().parents[1] / ".github" / "actions"


class WriteFile(Protocol):
    """Writes a file inside the fake repo root."""

    def __call__(self, relative: str, content: str) -> Path:
        """Write `content` to `relative`, creating parent directories."""
        ...


class ReadOutputs(Protocol):
    """Parses whatever a script appended to GITHUB_OUTPUT."""

    def __call__(self) -> dict[str, str]:
        """Return the written key/value pairs."""
        ...


def _load(name: str, path: Path) -> ModuleType:
    """Import a script that ships as a plain file, not as an installed module."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def extract_config() -> ModuleType:
    """The extract-config action's script, loaded from its action directory."""
    return _load("extract_config", ACTIONS_DIR / "extract-config" / "extract_config.py")


@pytest.fixture(scope="session")
def update_manifest() -> ModuleType:
    """The update-version-manifest action's script, loaded from its action directory."""
    return _load(
        "update_version_manifest",
        ACTIONS_DIR / "update-version-manifest" / "update_version_manifest.py",
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty repo root, cwd'd into - extract_config always reads Path.cwd()."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def write(repo: Path) -> WriteFile:
    """Write a file inside the fake repo, creating parent directories."""

    def _write(relative: str, content: str) -> Path:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    return _write


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReadOutputs:
    """Point GITHUB_OUTPUT at a file and parse what the script wrote to it."""
    output_file = tmp_path / "github_output"
    output_file.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    def _read() -> dict[str, str]:
        values: dict[str, str] = {}
        for line in output_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key] = value
        return values

    return _read
