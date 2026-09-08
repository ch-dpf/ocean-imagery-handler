"""Process a real imagery file synchronously with the API's request options."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas import (
    ImageryJobCreate,
    ResamplingMethod,
    TileFormat,
    TileProfile,
    TileScheme,
)
from app.services.preprocessor import parse_wgs84_bounds, preprocess_imagery
from app.services.tile_publisher import publish_tileset
from app.services.tiler_runner import run_raster_tile


def _option(name: str) -> tuple[str, ...]:
    """Accept conventional CLI hyphens and API-style underscores."""
    conventional = f"--{name.replace('_', '-')}"
    api_style = f"--{name}"
    return (conventional,) if conventional == api_style else (conventional, api_style)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronously preprocess and tile a real imagery file using the same "
            "request fields and defaults as POST /api/v1/imagery/jobs."
        )
    )
    parser.add_argument(
        "--request-json",
        metavar="JSON_OR_FILE",
        help="Full ImageryJobCreate JSON object, or a path to a JSON file",
    )
    parser.add_argument(*_option("input_path"), help="Input TIF/TIFF/IMG path")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress JSON lines; the final result is still written to stdout",
    )

    preprocess = parser.add_argument_group("preprocess")
    preprocess.add_argument(*_option("target_crs"))
    preprocess.add_argument(
        *_option("build_overviews"),
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    preprocess.add_argument(*_option("block_size"), type=int)
    preprocess.add_argument(*_option("compress"), choices=["DEFLATE", "LZW", "JPEG"])
    preprocess.add_argument(*_option("jpeg_quality"), type=int)
    preprocess.add_argument(
        *_option("add_alpha"),
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    preprocess.add_argument(
        *_option("white_as_transparent"),
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    preprocess.add_argument(*_option("near_white"), type=int)

    tiling = parser.add_argument_group("tiling_options")
    tiling.add_argument(*_option("profile"), choices=[item.value for item in TileProfile])
    tiling.add_argument(*_option("tile_format"), choices=[item.value for item in TileFormat])
    tiling.add_argument(*_option("tile_size"), type=int)
    tiling.add_argument(*_option("start_zoom"), type=int)
    tiling.add_argument(*_option("end_zoom"), type=int)
    tiling.add_argument(
        *_option("resampling_method"),
        choices=[item.value for item in ResamplingMethod],
    )
    tiling.add_argument(*_option("thread_count"), type=int)
    tiling.add_argument(
        *_option("resume"),
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    tiling.add_argument(
        *_option("verbose"),
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    tiling.add_argument(
        *_option("kml"),
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    tiling.add_argument(*_option("tile_scheme"), choices=[item.value for item in TileScheme])

    publish = parser.add_argument_group("publish")
    publish.add_argument(
        *_option("auto_publish"),
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    publish.add_argument(*_option("tileset_name"))
    return parser


def _read_request_json(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    stripped = value.strip()
    if stripped.startswith("{"):
        data = json.loads(stripped)
    else:
        data = json.loads(Path(stripped).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("--request-json must contain a JSON object")
    return data


def request_from_args(args: argparse.Namespace) -> ImageryJobCreate:
    """Validate a CLI namespace through the exact API request models."""
    request = ImageryJobCreate.model_validate(_read_request_json(args.request_json))

    preprocess_fields = (
        "target_crs",
        "build_overviews",
        "block_size",
        "compress",
        "jpeg_quality",
        "add_alpha",
        "white_as_transparent",
        "near_white",
    )
    tiling_fields = (
        "profile",
        "tile_format",
        "tile_size",
        "start_zoom",
        "end_zoom",
        "resampling_method",
        "thread_count",
        "resume",
        "verbose",
        "kml",
        "tile_scheme",
    )
    publish_fields = ("auto_publish", "tileset_name")

    preprocess_updates = {
        field: getattr(args, field)
        for field in preprocess_fields
        if getattr(args, field) is not None
    }
    tiling_updates = {
        field: getattr(args, field)
        for field in tiling_fields
        if getattr(args, field) is not None
    }
    publish_updates = {
        field: getattr(args, field)
        for field in publish_fields
        if getattr(args, field) is not None
    }

    payload = request.model_dump()
    payload["input_path"] = args.input_path or request.input_path
    payload["preprocess"].update(preprocess_updates)
    payload["tiling_options"].update(tiling_updates)
    payload["publish"].update(publish_updates)
    return ImageryJobCreate.model_validate(payload)


class _ConsoleProgress:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.stage = ""
        self.last_percent = -1.0

    def begin(self, stage: str) -> None:
        self.stage = stage
        self.last_percent = -1.0

    def __call__(self, percent: float, message: str | None) -> None:
        if self.quiet or (percent < 100.0 and percent - self.last_percent < 1.0):
            return
        self.last_percent = percent
        print(
            json.dumps(
                {
                    "stage": self.stage,
                    "percent": round(percent, 2),
                    "message": message,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )


def _resolve_tiling_options(request: ImageryJobCreate, settings: Settings):
    tiling = request.tiling_options
    return tiling.model_copy(
        update={
            "thread_count": (
                tiling.thread_count
                if tiling.thread_count is not None
                else settings.tiling_thread_count
            ),
            "resume": tiling.resume if tiling.resume is not None else settings.tiling_resume,
        }
    )


def process_request(
    request: ImageryJobCreate,
    *,
    settings: Settings | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Synchronously execute the same processing stages as the Celery job."""
    resolved_settings = settings or get_settings()
    if not request.input_path:
        raise ValueError("input_path is required")

    input_path = Path(request.input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    job_id = str(uuid4())
    job_dir = resolved_settings.jobs_dir / job_id
    preprocess_dir = job_dir / "preprocess"
    output_dir = job_dir / "tiles"
    progress = _ConsoleProgress(quiet)

    progress.begin("gdal_preprocess")
    preprocessed = preprocess_imagery(
        input_path=input_path,
        work_dir=preprocess_dir,
        options=request.preprocess,
        gdal_cachemax=resolved_settings.gdal_cachemax,
        on_subprogress=progress,
    )
    bounds_wgs84 = parse_wgs84_bounds(
        preprocessed,
        env={"GDAL_CACHEMAX": str(resolved_settings.gdal_cachemax)},
    )

    tiling = _resolve_tiling_options(request, resolved_settings)
    progress.begin("gdal_raster_tile")
    run_raster_tile(
        input_path=preprocessed,
        output_dir=output_dir,
        options=tiling,
        gdal_cachemax=resolved_settings.gdal_cachemax,
        on_subprogress=progress,
    )

    result: dict[str, Any] = {
        "job_id": job_id,
        "status": "completed",
        "input_path": str(input_path),
        "preprocessed_path": str(preprocessed),
        "output_dir": str(output_dir),
        "bounds_wgs84": bounds_wgs84,
        "published": False,
        "request": request.model_dump(mode="json"),
    }

    should_publish = (
        request.publish.auto_publish
        if request.publish.auto_publish is not None
        else resolved_settings.auto_publish
    )
    if should_publish:
        imagery_url, tileset_name, url_template = publish_tileset(
            job_id=job_id,
            tiles_dir=output_dir,
            tilesets_dir=resolved_settings.tilesets_dir,
            public_url=resolved_settings.imagery_server_public_url,
            base_path=resolved_settings.imagery_base_path,
            profile=tiling.profile,
            tile_format=tiling.tile_format,
            bounds_wgs84=bounds_wgs84,
            tileset_name=request.publish.tileset_name,
            tile_scheme=tiling.tile_scheme,
        )
        result.update(
            {
                "published": True,
                "imagery_url": imagery_url,
                "tileset_name": tileset_name,
                "cesium_url_template": url_template,
            }
        )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        request = request_from_args(args)
        result = process_request(request, quiet=args.quiet)
    except (
        ValidationError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
