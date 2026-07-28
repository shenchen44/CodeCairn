from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    github_app_id: str = ""
    github_private_key_path: str = ""
    github_api_base: str = "https://api.github.com"
    sandbox_base_image: str = "python:3.12-slim"
    sandbox_timeout_seconds: int = 300
    sandbox_memory_limit: str = "1g"
    sandbox_cpu_limit: float = 1.0
    sandbox_pids_limit: int = 256
    sandbox_uid: int | None = None
    sandbox_gid: int | None = None
    docker_bind_host_root: str | None = None
    docker_bind_container_root: str = "/app"

    @property
    def private_key(self) -> str:
        if not self.github_private_key_path:
            return ""
        return Path(self.github_private_key_path).read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
