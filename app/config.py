"""Application settings loaded from .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    # песочница bubblewrap для воркера (Linux). На Ubuntu 26.04 AppArmor (bwrap-userns-restrict)
    # не даёт ей стартовать — включать только после решения владельца по AppArmor
    worker_sandbox: bool = False
    # local — claude от текущего пользователя (разработка); sudo — от WORKER_USER через обёртку
    worker_mode: Literal["local", "sudo"] = "local"
    worker_user: str = "bitowi-worker"
    worker_runner: str = "/usr/local/bin/bitowi-worker-run"

    data_dir: Path = Path("./data")
    tasks_dir: Path = Path("~/bitowi_data/tasks")
    prompts_dir: Path = Path("~/bitowi_instructions/prompts")

    claude_bin: str = "claude"
    codex_bin: str = "codex"
    playwright_mcp_bin: str = "playwright-mcp"

    # браузер
    browser_timeout: int = 45
    max_questions: int = 15

    # режим курса: курьер (Codex + Playwright MCP поверх браузера оркестратора)
    courier_timeout: int = 300
    course_max_shots: int = 4
    course_viewport: str = "1280x900"
    # доп. источники через «;» для --allowed-origins (CDN платформы), по умолчанию — только домен курса
    course_extra_origins: str = ""

    # вход по форме (пароли модели не передаются — вход делает оркестратор)
    demo_moodle_host: str = "school.moodledemo.net"
    demo_moodle_user: str = ""
    demo_moodle_password: str = ""
    fake_lms_host: str = "127.0.0.1:8765"
    fake_lms_user: str = "student"
    fake_lms_password: str = ""

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

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / "auth"

    @property
    def boss_root(self) -> Path:
        return self.data_dir / "boss"

    def prompt(self, name: str) -> str:
        """Read a system prompt template from PROMPTS_DIR."""
        return (self.prompts_dir / name).read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
