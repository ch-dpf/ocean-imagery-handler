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
    assert "readme.txt" not in names  # non-imagery files are omitted
    assert "jobs" not in names

    tif = next(entry for entry in listing.entries if entry.name == "a.tif")
    assert tif.selectable is True
    assert tif.size_bytes is None  # size skipped for bind-mount performance
    assert tif.absolute_path == str(workspace / "a.tif")

    nested = next(entry for entry in listing.entries if entry.name == "nested")
    assert nested.entry_type == "directory"
    assert nested.selectable is False


def test_list_workspace_subdirectory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (nested / "scene.tiff").write_bytes(b"abc")
    (nested / "notes.md").write_bytes(b"x")

    listing = list_workspace(workspace, "nested")

    assert listing.relative_path == "nested"
    assert listing.parent_relative_path == ""
    assert len(listing.entries) == 1
    assert listing.entries[0].name == "scene.tiff"
    assert listing.entries[0].selectable is True
    assert listing.entries[0].absolute_path == str(workspace / "nested" / "scene.tiff")


def test_list_workspace_large_imagery_folder_is_fast(tmp_path: Path) -> None:
    """Regression: listing thousands of imagery names must not resolve/stat each file."""
    import time

    workspace = tmp_path / "workspace"
    folder = workspace / "many"
    folder.mkdir(parents=True)
    for index in range(1500):
        (folder / f"tile_{index:04d}.tif").write_bytes(b"x")
    (folder / "ignore.txt").write_bytes(b"y")

    started = time.perf_counter()
    listing = list_workspace(workspace, "many")
    elapsed = time.perf_counter() - started

    assert len(listing.entries) == 1500
    assert all(entry.selectable for entry in listing.entries)
    assert elapsed < 2.0
