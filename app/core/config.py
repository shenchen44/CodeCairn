from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/codecairn"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_base_url: str | None = None
    openai_provider: str = "openai"
    openai_timeout_seconds: float = 90
    openai_max_retries: int = 1
    github_app_id: str = ""
    github_webhook_secret: str = ""
    github_private_key_path: str = ""
    github_target_labels: str = "good first issue,bug,agent-fixable"
    worker_poll_interval: int = 5
    sandbox_base_image: str = "python:3.12-slim"
    sandbox_timeout_seconds: int = 300
    sandbox_memory_limit: str = "1g"
    sandbox_cpu_limit: float = 1.0
    workspace_root: str = "/tmp/codecairn"
    docker_bind_host_root: str | None = None
    docker_bind_container_root: str = "/app"
    log_level: str = "INFO"
    dashboard_enabled: bool = True
    github_api_base: str = "https://api.github.com"
    max_attempts: int = 3
    pr_review_label: str = "needs-human-review"
    agent_runtime_variant: str = "full"

    @property
    def target_labels(self) -> set[str]:
        return {label.strip().lower() for label in self.github_target_labels.split(",") if label.strip()}

    @property
    def private_key(self) -> str:
        if not self.github_private_key_path:
            return ""
        return Path(self.github_private_key_path).read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
