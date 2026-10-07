"""Runtime settings, read from environment variables (and .env when present)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = f"sqlite:///{REPO_ROOT / 'data' / 'app.db'}"

    llm_provider: Literal["scripted", "gemini", "openai_compat"] = "scripted"
    gemini_api_key: str = ""
    gemini_model: str = ""
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    openai_compat_model: str = ""
    llm_max_rpm: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
