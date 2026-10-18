"""Application settings loaded from .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    owner_id: int = 0

    max_rounds: int = 5
    max_retries: int = 2
    worker_timeout: int = 900
    boss_timeout: int = 600

    # Подтверждение перед сдачей отключается только явным AUTO_SUBMIT=true в .env
    auto_submit: bool = False
    worker_web: bool = False

    data_dir: Path = Path("./data")
    tasks_dir: Path = Path("~/bitowi_data/tasks")
    prompts_dir: Path = Path("~/bitowi_instructions/prompts")

    claude_bin: str = "claude"
    codex_bin: str = "codex"

    @field_validator("data_dir", "tasks_dir", "prompts_dir", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("owner_id", mode="before")
    @classmethod
    def _empty_owner(cls, value: object) -> object:
        return 0 if value in ("", None) else value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bitowi.db"

    def prompt(self, name: str) -> str:
        """Read a system prompt template from PROMPTS_DIR."""
        return (self.prompts_dir / name).read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
