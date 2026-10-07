"""Environment configuration.

Values come from environment variables (or a local `.env` file when running
outside Docker). Docker Compose sets them explicitly for each container.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Required: there is no safe default for where the data lives.
    database_url: str
    celery_broker_url: str

    certificate_storage_dir: Path = Path("/data/certificates")
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
