"""Application settings loaded from .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # секреты не попадают в repr (вывод упавших тестов, логи)
    bot_token: str = Field(default="", repr=False)
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
    # доп. слова через «;», которые воркер не должен видеть (название вуза, платформы)
    course_banned_words: str = ""
    # лимиты одного запуска курса (до паузы; /resume продолжает с теми же счётчиками шагов на странице)
    course_max_steps: int = 300
    course_max_runtime: int = 7200
    course_max_steps_per_page: int = 15
    # тот же экран столько раз подряд — застряли
    course_stuck_repeats: int = 3
    # столько шагов подряд без единого выполненного действия — застряли
    course_fail_repeats: int = 3
    # local — codex от текущего пользователя (разработка); sudo — от COURIER_USER через обёртку,
    # без доступа к /home бота (.env, state.json, логины)
    courier_mode: Literal["local", "sudo"] = "local"
    courier_user: str = "bitowi-courier"
    courier_runner: str = "/usr/local/bin/bitowi-courier-run"
    courier_root: Path = Path("/srv/bitowi-courier")
    courier_mcp_bin: str = "/home/bitowi-courier/.local/bin/playwright-mcp"

    # вход по форме (пароли модели не передаются — вход делает оркестратор)
    demo_moodle_host: str = "school.moodledemo.net"
    demo_moodle_user: str = ""
    demo_moodle_password: str = Field(default="", repr=False)
    fake_lms_host: str = "127.0.0.1:8765"
    fake_lms_user: str = "student"
    fake_lms_password: str = Field(default="", repr=False)

    @field_validator("data_dir", "tasks_dir", "prompts_dir", "courier_root", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("owner_id", mode="before")
    @classmethod
    def _empty_owner(cls, value: object) -> object:
        return 0 if value in ("", None) else value

    def __repr__(self) -> str:
        # значения из .env не печатаются нигде: вывод упавших тестов, логи, сообщения
        return "Settings(<скрыто>)"

    __str__ = __repr__

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bitowi.db"

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / "auth"

    @property
    def boss_root(self) -> Path:
        return self.data_dir / "boss"

    def courier_dir(self, task_id: int) -> Path:
        """Courier's working dir: guard.js, MCP output, files approved for upload."""
        return self.courier_root / str(task_id)

    def prompt(self, name: str) -> str:
        """Read a system prompt template from PROMPTS_DIR."""
        return (self.prompts_dir / name).read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
