import pytest

from app.config import Settings
from app.db import Database


class FakeNotifier:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.previews: list[tuple[int, str]] = []
        self.messages: list[str] = []

    async def status(self, task, text: str) -> None:
        self.statuses.append(text)

    async def preview(self, task, task_text: str, answer: str) -> None:
        self.previews.append((task.id, answer))

    async def message(self, task, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def prompts_dir(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "worker.md").write_text("Ты — исполнитель.", encoding="utf-8")
    (d / "boss_review.md").write_text(
        'Раунд {round} из {max_rounds}.\nВерни JSON: {"verdict": "accept | revise"}', encoding="utf-8"
    )
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
