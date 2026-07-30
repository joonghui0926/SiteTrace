"""Runtime configuration.

Secrets are loaded from the process environment or the repository-local
``.env`` file.  The settings object deliberately exposes only boolean
capability checks so health responses can never leak credential values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPOSITORY_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "SiteTrace"
    app_environment: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    openai_api_key: str = ""
    openai_normalization_model: str = "gpt-5.6-luna"
    openai_reasoning_model: str = "gpt-5.6-terra"
    openai_escalation_model: str = "gpt-5.6-sol"

    twelve_labs_api_key: str = ""
    twelve_labs_knowledge_store_id: str = ""
    twelve_labs_store_prefix: str = "SiteTrace"
    twelve_labs_pegasus_model: str = "pegasus1.5"
    twelve_labs_marengo_model: str = "marengo3.0"
    twelve_labs_poll_seconds: float = 5.0
    twelve_labs_poll_timeout_seconds: int = 1800

    neo4j_uri: str = ""
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    aws_region: str = Field(default="us-east-1", validation_alias="AWS_REGION")
    sitetrace_s3_bucket: str = ""
    sitetrace_session_table: str = ""
    sitetrace_session_directory: str = ".sitetrace/sessions"

    sitetrace_demo_mode: bool = False
    upload_directory: Path = REPOSITORY_ROOT / ".sitetrace" / "uploads"
    case_directory: Path = REPOSITORY_ROOT / ".sitetrace" / "cases"
    report_directory: Path = REPOSITORY_ROOT / ".sitetrace" / "reports"
    max_upload_bytes: int = 5 * 1024 * 1024 * 1024

    @property
    def allowed_origins(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_twelvelabs(self) -> bool:
        return bool(self.twelve_labs_api_key)

    @property
    def has_neo4j(self) -> bool:
        return bool(
            self.neo4j_uri
            and self.neo4j_uri not in {"neo4j+s://", "neo4j://"}
            and self.neo4j_password
        )

    @property
    def has_aws_storage(self) -> bool:
        return bool(self.sitetrace_s3_bucket)

    def ensure_directories(self) -> None:
        for path in (
            self.upload_directory,
            self.case_directory,
            self.report_directory,
            Path(self.sitetrace_session_directory),
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    configured = Settings()
    configured.ensure_directories()
    return configured


settings = get_settings()

