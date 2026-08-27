"""Safe read-only browsing of files under the workspace directory."""

from __future__ import annotations

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


def _is_excluded_dir(path: Path, workspace: Path) -> bool:
    rel = _posix_relative(path, workspace)
    if not rel:
        return False
    top_segment = rel.split("/", 1)[0]
    return top_segment in EXCLUDED_DIR_NAMES


def list_workspace(workspace_dir: Path, relative_path: str = "") -> WorkspaceListing:
    """List directories and supported image files under a workspace subdirectory."""
    workspace = workspace_dir.resolve()
    current = resolve_workspace_path(workspace_dir, relative_path)

    if current.is_file():
        raise WorkspacePathError("Cannot list a file path")

    current_rel = _posix_relative(current, workspace)
    parent_rel = None
    if current_rel:
        parent = current.parent
        parent_rel = _posix_relative(parent, workspace)

    entries: list[WorkspaceEntry] = []

    for child in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.is_dir():
            if _is_excluded_dir(child, workspace):
                continue
            child_rel = _posix_relative(child, workspace)
            entries.append(
                WorkspaceEntry(
                    name=child.name,
                    relative_path=child_rel,
                    absolute_path=str(child.resolve()),
                    entry_type="directory",
                    size_bytes=None,
                    selectable=False,
                )
            )
            continue

        if not child.is_file():
            continue

        suffix = child.suffix.lower()
        child_rel = _posix_relative(child, workspace)
        entries.append(
            WorkspaceEntry(
                name=child.name,
                relative_path=child_rel,
                absolute_path=str(child.resolve()),
                entry_type="file",
                size_bytes=child.stat().st_size,
                selectable=suffix in SUPPORTED_EXTENSIONS,
            )
        )

    return WorkspaceListing(
        relative_path=current_rel,
        absolute_path=str(current.resolve()),
        parent_relative_path=parent_rel,
        entries=entries,
    )
