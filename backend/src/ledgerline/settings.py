from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEDGERLINE_", env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ledgerline:ledgerline@localhost:5433/ledgerline"
    test_database_url: str = "postgresql+psycopg://ledgerline:ledgerline@localhost:5433/ledgerline_test"


settings = Settings()
