import json
from pathlib import Path

import pytest

from app import cli
from app.schemas import JobProgress, JobStatus
from app.services.imagery_pipeline import PipelineEvent


def test_run_command_invokes_synchronous_pipeline(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "source.tif"
    source.touch()
    captured: dict[str, object] = {}

    def fake_run(job_id, request, *, settings, on_event):
        captured.update(job_id=job_id, request=request, settings=settings)
        on_event(
            PipelineEvent(
                status=JobStatus.COMPLETED,
                stage="done",
                progress=JobProgress(percent=100, phase="done", message="Completed"),
            )
        )
        return {"job_id": job_id, "status": "completed", "published": False}

    monkeypatch.setattr(cli, "run_imagery_pipeline", fake_run)

    workspace = tmp_path / "workspace"
    exit_code = cli.main(
        [
            "run",
            str(source),
            "--job-id",
            "local-job",
            "--workspace-dir",
            str(workspace),
        ]
    )

    assert exit_code == 0
    assert captured["job_id"] == "local-job"
    assert captured["request"].input_path == str(source.resolve())
    assert captured["settings"].workspace_dir == workspace.resolve()
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_tileset_name_enables_publish(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.tif"
    source.touch()
    captured: dict[str, object] = {}

    def fake_run(job_id, request, *, settings, on_event):
        captured["request"] = request
        return {"job_id": job_id, "status": "completed", "published": True}

    monkeypatch.setattr(cli, "run_imagery_pipeline", fake_run)

    exit_code = cli.main(["run", str(source), "--tileset-name", "demo"])

    assert exit_code == 0
    request = captured["request"]
    assert request.publish.auto_publish is True
    assert request.publish.tileset_name == "demo"


def test_run_command_returns_error_for_missing_input(tmp_path: Path, capsys) -> None:
    exit_code = cli.main(["run", str(tmp_path / "missing.tif")])

    assert exit_code == 1
    assert "Input file not found" in capsys.readouterr().err


def test_run_command_rejects_unsafe_job_id(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", str(tmp_path / "source.tif"), "--job-id", "../outside"])

    assert exc_info.value.code == 2
