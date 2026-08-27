"""Tests for workspace browser service."""

from pathlib import Path

import pytest

from app.services.workspace_browser import (
    WorkspacePathError,
    list_workspace,
    resolve_workspace_path,
)


def test_resolve_workspace_path_rejects_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ortho.tif").write_bytes(b"x")

    with pytest.raises(WorkspacePathError):
        resolve_workspace_path(workspace, "../etc/passwd")


def test_list_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.tif").write_bytes(b"1234")
    (workspace / "readme.txt").write_bytes(b"x")
    (workspace / "nested").mkdir()
    (workspace / "jobs").mkdir()

    listing = list_workspace(workspace, "")

    names = [entry.name for entry in listing.entries]
    assert "nested" in names
    assert "a.tif" in names
    assert "readme.txt" in names
    assert "jobs" not in names

    tif = next(entry for entry in listing.entries if entry.name == "a.tif")
    assert tif.selectable is True
    assert tif.size_bytes == 4

    txt = next(entry for entry in listing.entries if entry.name == "readme.txt")
    assert txt.selectable is False


def test_list_workspace_subdirectory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (nested / "scene.tiff").write_bytes(b"abc")

    listing = list_workspace(workspace, "nested")

    assert listing.relative_path == "nested"
    assert listing.parent_relative_path == ""
    assert len(listing.entries) == 1
    assert listing.entries[0].name == "scene.tiff"
    assert listing.entries[0].selectable is True
