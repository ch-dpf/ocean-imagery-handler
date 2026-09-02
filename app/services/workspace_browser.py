"""Safe read-only browsing of files under the workspace directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_EXTENSIONS = {".tif", ".tiff", ".img"}
EXCLUDED_DIR_NAMES = {"jobs", "uploads", "tilesets"}


class WorkspacePathError(ValueError):
    """Raised when a workspace path is invalid or outside the workspace root."""


@dataclass(frozen=True)
class WorkspaceEntry:
    name: str
    relative_path: str
    absolute_path: str
    entry_type: str
    size_bytes: int | None
    selectable: bool


@dataclass(frozen=True)
class WorkspaceListing:
    relative_path: str
    absolute_path: str
    parent_relative_path: str | None
    entries: list[WorkspaceEntry]


def _posix_relative(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return "" if rel == "." else rel


def _child_relative(current_rel: str, name: str) -> str:
    return f"{current_rel}/{name}" if current_rel else name


def _absolute_under_workspace(workspace: Path, relative_path: str) -> str:
    """Build an absolute path string without resolving each child (avoids bind-mount cost)."""
    if not relative_path:
        return str(workspace)
    return str(workspace / relative_path)


def resolve_workspace_path(workspace_dir: Path, relative_path: str = "") -> Path:
    """Resolve a user-provided relative path safely inside workspace_dir."""
    workspace = workspace_dir.resolve()
    normalized = relative_path.strip().replace("\\", "/").strip("/")
    target = (workspace / normalized).resolve() if normalized else workspace

    if target != workspace and workspace not in target.parents:
        raise WorkspacePathError("Path is outside the workspace")

    if not target.exists():
        raise WorkspacePathError(f"Path not found: {normalized or '/'}")

    return target


def _is_excluded_relative(child_rel: str) -> bool:
    if not child_rel:
        return False
    top_segment = child_rel.split("/", 1)[0]
    return top_segment in EXCLUDED_DIR_NAMES


def _suffix_lower(name: str) -> str:
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return name[dot:].lower()


def list_workspace(workspace_dir: Path, relative_path: str = "") -> WorkspaceListing:
    """List directories and supported image files under a workspace subdirectory.

    Optimized for large folders on slow bind mounts:
    - ``os.scandir`` (DirEntry caches type bits)
    - skip non-imagery files entirely
    - avoid per-entry ``Path.resolve()`` / size ``stat``
    """
    workspace = workspace_dir.resolve()
    current = resolve_workspace_path(workspace_dir, relative_path)

    if current.is_file():
        raise WorkspacePathError("Cannot list a file path")

    current_rel = _posix_relative(current, workspace)
    parent_rel = None
    if current_rel:
        parent_rel = _posix_relative(current.parent, workspace)

    directories: list[WorkspaceEntry] = []
    files: list[WorkspaceEntry] = []

    with os.scandir(current) as iterator:
        for entry in iterator:
            name = entry.name
            child_rel = _child_relative(current_rel, name)
            suffix = _suffix_lower(name)

            # Extension-first: imagery folders often have 1000+ *.tif files. On Docker
            # Desktop bind mounts, DirEntry.is_file/is_dir may still stat each entry
            # (DT_UNKNOWN), so avoid type checks for known imagery names.
            if suffix in SUPPORTED_EXTENSIONS:
                files.append(
                    WorkspaceEntry(
                        name=name,
                        relative_path=child_rel,
                        absolute_path=_absolute_under_workspace(workspace, child_rel),
                        entry_type="file",
                        size_bytes=None,
                        selectable=True,
                    )
                )
                continue

            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_directory:
                continue
            if _is_excluded_relative(child_rel):
                continue

            directories.append(
                WorkspaceEntry(
                    name=name,
                    relative_path=child_rel,
                    absolute_path=_absolute_under_workspace(workspace, child_rel),
                    entry_type="directory",
                    size_bytes=None,
                    selectable=False,
                )
            )

    directories.sort(key=lambda item: item.name.lower())
    files.sort(key=lambda item: item.name.lower())

    return WorkspaceListing(
        relative_path=current_rel,
        absolute_path=str(current),
        parent_relative_path=parent_rel,
        entries=directories + files,
    )
