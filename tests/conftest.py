from pathlib import Path

import pytest

from app.browser.session import SessionExpired
from app.config import Settings
from app.db import Database


class FakeNotifier:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.previews: list[tuple[int, str]] = []
        self.messages: list[str] = []
        self.photos: list[Path] = []

    async def status(self, task, text: str) -> None:
        self.statuses.append(text)

    async def preview(self, task, task_text: str, answer: str) -> None:
        self.previews.append((task.id, answer))

    async def message(self, task, text: str) -> None:
        self.messages.append(text)

    async def photo(self, task, path: Path, caption: str) -> None:
        self.photos.append(path)


class FakeSessions:
    """Browser sessions without a browser: scripted ensure_session results."""

    def __init__(self, results=()) -> None:
        self.results = list(results)
        self.calls: list[str] = []
        self.screens: list[Path] = []

    async def ensure_session(self, profile, url: str):
        self.calls.append(url)
        step = self.results.pop(0) if self.results else profile.auth_file
        if isinstance(step, BaseException):
            raise step
        return step

    async def screenshot(self, url: str, storage, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG fake")
        self.screens.append(path)
        return path


@pytest.fixture
def prompts_dir(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "worker.md").write_text("Ты — исполнитель.", encoding="utf-8")
    (d / "boss_review.md").write_text(
        'Раунд {round} из {max_rounds}.\nВерни JSON: {"verdict": "accept | revise"}', encoding="utf-8"
    )
    (d / "boss_extract.md").write_text("Открой {url}, скриншот в {screens_dir}/extract.png, до {max_questions}.", encoding="utf-8")
    return d


@pytest.fixture
def settings(tmp_path, prompts_dir):
    return Settings(
        _env_file=None,
        bot_token="x",
        owner_id=1,
        max_rounds=3,
        data_dir=tmp_path / "data",
        tasks_dir=tmp_path / "tasks",
        prompts_dir=prompts_dir,
    )


@pytest.fixture
async def db(settings):
    database = Database(settings.db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def sessions():
    return FakeSessions()


__all__ = ["FakeNotifier", "FakeSessions", "SessionExpired"]
