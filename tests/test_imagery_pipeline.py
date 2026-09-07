from pathlib import Path

from app.config import Settings
from app.schemas import ImageryJobCreate, JobStatus, PublishOptions, TilingOptions
from app.services.byte_progress import ByteBudget
from app.services.imagery_pipeline import PipelineEvent, run_imagery_pipeline


def _settings(workspace_dir: Path) -> Settings:
    return Settings(
        workspace_dir=workspace_dir,
        auto_publish=False,
        tiling_thread_count=3,
        tiling_resume=True,
    )


def test_pipeline_runs_without_celery_or_job_store(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.tif"
    source.touch()
    preprocessed = tmp_path / "jobs" / "sync-job" / "preprocess" / "preprocessed.tif"
    calls: dict[str, object] = {}
    events: list[PipelineEvent] = []

    monkeypatch.setattr(
        "app.services.imagery_pipeline.plan_pipeline_bytes",
        lambda *_args, **_kwargs: ByteBudget(reproject=40, overviews=10, tiles=50),
    )
    monkeypatch.setattr(
        "app.services.imagery_pipeline.validate_source_imagery",
        lambda *_args, **_kwargs: {"wgs84Bounds": [1.0, 2.0, 3.0, 4.0]},
    )

    def fake_preprocess(**kwargs):
        calls["preprocess"] = kwargs
        kwargs["on_subprogress"](100.0, "Preprocessed")
        return preprocessed

    def fake_tile(**kwargs):
        calls["tiling"] = kwargs
        kwargs["on_subprogress"](100.0, "Zoom 7")

    monkeypatch.setattr("app.services.imagery_pipeline.preprocess_imagery", fake_preprocess)
    monkeypatch.setattr("app.services.imagery_pipeline.run_raster_tile", fake_tile)
    monkeypatch.setattr(
        "app.services.imagery_pipeline.parse_wgs84_bounds",
        lambda *_args, **_kwargs: [5.0, 6.0, 7.0, 8.0],
    )

    request = ImageryJobCreate(
        input_path=str(source),
        tiling_options=TilingOptions(start_zoom=7),
    )
    result = run_imagery_pipeline(
        "sync-job",
        request,
        settings=_settings(tmp_path),
        on_event=events.append,
    )

    assert result == {
        "job_id": "sync-job",
        "status": "completed",
        "output_dir": str(tmp_path / "jobs" / "sync-job" / "tiles"),
        "bounds_wgs84": [5.0, 6.0, 7.0, 8.0],
        "published": False,
    }
    assert calls["tiling"]["options"].thread_count == 3
    assert calls["tiling"]["options"].resume is True
    assert [event.stage for event in events if event.stage == "done"] == ["done"]
    assert events[-1].status is JobStatus.COMPLETED
    assert events[-1].progress.percent == 100.0
    assert events[-1].fields == result


def test_pipeline_can_publish_synchronously(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.tif"
    source.touch()

    monkeypatch.setattr(
        "app.services.imagery_pipeline.plan_pipeline_bytes",
        lambda *_args, **_kwargs: ByteBudget(reproject=1, overviews=0, tiles=1),
    )
    monkeypatch.setattr(
        "app.services.imagery_pipeline.validate_source_imagery",
        lambda *_args, **_kwargs: {"wgs84Bounds": [1.0, 2.0, 3.0, 4.0]},
    )
    monkeypatch.setattr(
        "app.services.imagery_pipeline.preprocess_imagery",
        lambda **_kwargs: tmp_path / "preprocessed.tif",
    )
    monkeypatch.setattr(
        "app.services.imagery_pipeline.parse_wgs84_bounds",
        lambda *_args, **_kwargs: [1.0, 2.0, 3.0, 4.0],
    )
    monkeypatch.setattr(
        "app.services.imagery_pipeline.run_raster_tile",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.imagery_pipeline.publish_pipeline_tileset",
        lambda *_args, **_kwargs: (
            "http://localhost/imagery/demo",
            "demo",
            "http://localhost/imagery/demo/{z}/{x}/{y}.png",
        ),
    )

    request = ImageryJobCreate(
        input_path=str(source),
        publish=PublishOptions(auto_publish=True, tileset_name="demo"),
    )
    result = run_imagery_pipeline("sync-job", request, settings=_settings(tmp_path))

    assert result["published"] is True
    assert result["tileset_name"] == "demo"
    assert result["imagery_url"] == "http://localhost/imagery/demo"


def test_pipeline_rejects_missing_input_before_processing(tmp_path: Path) -> None:
    request = ImageryJobCreate(input_path=str(tmp_path / "missing.tif"))

    try:
        run_imagery_pipeline("sync-job", request, settings=_settings(tmp_path))
    except FileNotFoundError as exc:
        assert "missing.tif" in str(exc)
    else:
        raise AssertionError("missing input should fail")
