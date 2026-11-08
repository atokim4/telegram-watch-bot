"""Settings from environment variables or a ``.env`` file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr | None = None
    telegram_api_base: str = ""
    """Point aiogram at a different Bot API server (a local one, or a test stub)."""
    admin_chat_ids: list[int] = Field(default_factory=list)

    anthropic_api_key: SecretStr | None = None
    """Leave empty to run without the AI relevance filter."""
    anthropic_model: str = "claude-opus-5-5"

    database_path: Path = Path("data/tgwatch.sqlite3")
    max_watches_per_chat: int = Field(default=10, ge=1)
    default_interval_minutes: int = Field(default=15, ge=5)
    preview_cooldown_seconds: float = Field(default=10.0, ge=0)
    """Minimum gap between two dry runs (/test, new watch) in one chat."""

    user_agent: str = ""
    """Empty = ``tgwatch/<version> (+<repo url>)``. Add a contact URL if you run it publicly."""
    http_timeout_seconds: float = Field(default=20.0, gt=0)
    host_min_interval_seconds: float = Field(default=2.0, ge=0)
    respect_robots_txt: bool = True
    allow_private_hosts: bool = False
    """Allow watching localhost / LAN addresses. Keep off for a bot strangers can use."""
    scheduler_tick_seconds: float = Field(default=30.0, gt=0)
