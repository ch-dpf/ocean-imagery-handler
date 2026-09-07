"""Command-line interface for synchronous imagery processing."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.schemas import ImageryJobCreate, PublishOptions
from app.services.imagery_pipeline import PipelineEvent, run_imagery_pipeline
from app.services.preprocessor import PreprocessError
from app.services.raster.errors import RasterError
from app.services.tile_json import TileJsonError
from app.services.tile_publisher import PublishError
from app.services.tiler_runner import TilerError

_JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _job_id(value: str) -> str:
    candidate = value.strip()
    if ".." in candidate or _JOB_ID_RE.fullmatch(candidate) is None:
        raise argparse.ArgumentTypeError(
            "job ID must contain only letters, numbers, '.', '_' or '-' and cannot contain '..'"
        )
    return candidate


class _ConsoleProgress:
    """Render useful progress changes without flooding stderr."""

    def __init__(self) -> None:
        self._last_stage: str | None = None
        self._last_percent: int | None = None

    def __call__(self, event: PipelineEvent) -> None:
        percent = int(event.progress.percent)
        if event.stage == self._last_stage and percent == self._last_percent and not event.fields:
            return

        message = event.progress.message or event.stage
        print(
            f"[{event.status.value}] {percent:3d}% {event.stage}: {message}",
            file=sys.stderr,
            flush=True,
        )
        self._last_stage = event.stage
        self._last_percent = percent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocean-imagery",
        description="Process orthophoto GeoTIFF imagery without the API or Celery.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run the imagery pipeline synchronously.",
    )
    run_parser.add_argument("input", type=Path, metavar="input.tif", help="Input GeoTIFF path.")
    run_parser.add_argument(
        "--job-id",
        type=_job_id,
        help="Output job directory name; defaults to a generated UUID.",
    )
    run_parser.add_argument(
        "--auto-publish",
        action="store_true",
        default=None,
        help="Publish the generated tileset after processing.",
    )
    run_parser.add_argument(
        "--tileset-name",
        help="Published tileset name; implies --auto-publish.",
    )
    run_parser.add_argument(
        "--workspace-dir",
        type=Path,
        help="Host workspace directory; overrides WORKSPACE_DIR for this run.",
    )
    return parser


def _run_command(args: argparse.Namespace) -> dict[str, Any]:
    input_path = args.input.expanduser().resolve()
    settings = Settings()
    if args.workspace_dir is not None:
        workspace_dir = args.workspace_dir.expanduser().resolve()
        settings = settings.model_copy(update={"workspace_dir": workspace_dir})
    auto_publish = True if args.tileset_name else args.auto_publish
    request = ImageryJobCreate(
        input_path=str(input_path),
        publish=PublishOptions(
            auto_publish=auto_publish,
            tileset_name=args.tileset_name,
        ),
    )
    return run_imagery_pipeline(
        args.job_id or str(uuid4()),
        request,
        settings=settings,
        on_event=_ConsoleProgress(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "run":
            result = _run_command(args)
        else:  # pragma: no cover - argparse enforces the available commands.
            raise ValueError(f"Unsupported command: {args.command}")
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (
        PreprocessError,
        RasterError,
        TilerError,
        PublishError,
        TileJsonError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
