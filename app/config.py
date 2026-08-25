"""Application configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8100
    app_debug: bool = False

    redis_url: str = "redis://localhost:6380/0"
    celery_broker_url: str = "redis://localhost:6380/0"
    celery_result_backend: str = "redis://localhost:6380/1"

    workspace_dir: Path = Path("/data/workspace")
    gdal_cachemax: int = 512
    job_ttl: int = 604800

    imagery_server_public_url: str = "http://localhost:8102"
    imagery_base_path: str = "/imagery"
    auto_publish: bool = True

    @property
    def jobs_dir(self) -> Path:
        return self.workspace_dir / "jobs"

    @property
    def uploads_dir(self) -> Path:
        return self.workspace_dir / "uploads"

    @property
    def tilesets_dir(self) -> Path:
        return self.workspace_dir / "tilesets" / "imagery"

    def imagery_url_for(self, tileset_name: str) -> str:
        base = self.imagery_server_public_url.rstrip("/")
        path = self.imagery_base_path.rstrip("/")
        return f"{base}{path}/{tileset_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
