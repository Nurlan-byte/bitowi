"""Worker layer of the course mode: what the worker sees and how its JSON decision is parsed."""

import json

import pytest

from app.agents.base import WorkerBadJSON
from app.agents.fakes import FakeWorker
from app.agents.runner import RunResult
from app.agents.worker_claude import ClaudeWorker
from app.course.models import Action, ActionResult, Decision, Element, Screen
from app.course.policy import classify
from app.course.worker_input import StepLog, banned_words, copy_shots, render

ORIGIN = "https://school.moodledemo.net"


def test_banned_words():
    words = banned_words(ORIGIN, "Astana IT University; AITU")
    assert words[0] == "school.moodledemo.net"
    assert {"moodledemo", "Moodle", "AITU", "Astana IT University"} <= set(words)
    assert "school" not in words and "net" not in words


def screen() -> Screen:
    els = [
        Element(n=1, kind="link", label="Модуль 2", href=ORIGIN + "/course/view.php?id=7"),
        Element(n=2, kind="button", label="Выйти"),
        Element(n=3, kind="radio", label="391", checked=False),
        Element(n=4, kind="button", label="Проверить"),
        Element(n=5, kind="select", label="Город", value="Алматы", options=["Алматы", "Астана"]),
    ]
    return Screen(
        shots=[], text=(
            "Mount Orange School — Moodle\nСколько будет 17·23?\nНеверно\nБаллы: 0 из 1\n"
            "Правильный ответ: 391\nПодробнее: https://school.moodledemo.net/mod/quiz/view.php?id=7"
        ),
        elements=[classify(e, ORIGIN) for e in els], messages=["Есть ошибки"], progress="40%", more_below=True,
        url=ORIGIN + "/mod/quiz/attempt.php", title="Тест 1 | Moodle",
    )


def test_render_has_no_source_and_keeps_grades():
    results = [
        ActionResult(action=Action(action="click", element=3), ok=True),
        ActionResult(action=Action(action="click", element=2), ok=False, error="элемент [2] недоступен"),
    ]
    history = [StepLog(n=1, status="continue", results=results, comment="открыл тест")]
    out = render(screen(), ["screens/1.png"], results, history, ["вопрос 1 перепроверь"], banned_words(ORIGIN))
    for leak in ("moodledemo", "Moodle", "https", "view.php", "course/view", "Правильный ответ"):
        assert leak not in out, leak
    for keep in ("Неверно", "Баллы: 0 из 1", "Прогресс курса: 40%", "screens/1.png", "scroll down",
                 "[1] link: Модуль 2", "[2] button: Выйти (недоступен)", "[3] radio: 391 (не отмечено)",
                 "[4] button: Проверить (сдача — только после подтверждения)",
                 "варианты: Алматы | Астана", "- click [2] — не выполнено: элемент [2] недоступен",
                 "1. continue: действий 2, выполнено 1 — открыл тест", "- вопрос 1 перепроверь", "Есть ошибки"):
        assert keep in out, keep


def test_copy_shots(tmp_path):
    src = tmp_path / "boss"
    src.mkdir()
    for i in (1, 2):
        (src / f"{i}.png").write_bytes(b"\x89PNG" + bytes([i]))
    ws = tmp_path / "ws"
    (ws / "screens").mkdir(parents=True)
    (ws / "screens" / "old.png").write_bytes(b"old")
    names = copy_shots(Screen(shots=[str(src / "1.png"), str(src / "2.png")]), ws)
    assert names == ["screens/1.png", "screens/2.png"]
    assert sorted(p.name for p in (ws / "screens").iterdir()) == ["1.png", "2.png"]


def _claude_json(result: str) -> RunResult:
    return RunResult(0, json.dumps({"result": result, "is_error": False}), "")


async def test_decide_parses_and_retries(settings, tmp_path):
    seen = []
    replies = [
        "Сейчас подумаю… не JSON",
        '```json\n{"status": "submit_ready", "answers": ["17·23 — 391"], '
        '"actions": [{"action": "click", "element": 3}, {"action": "click", "element": 4}]}\n```',
    ]

    async def fake_run(cmd, *, cwd, timeout, stdin):
        seen.append({"cwd": cwd, "stdin": stdin})
        return _claude_json(replies.pop(0))

    ws = settings.tasks_dir / "5" / "workspace"
    d = await ClaudeWorker(settings, run=fake_run).decide("ЭКРАН", ws)
    assert d.status == "submit_ready" and [a.element for a in d.actions] == [3, 4]
    assert seen[0]["stdin"] == "ЭКРАН" and "Верни ТОЛЬКО JSON" in seen[1]["stdin"]
    assert seen[0]["cwd"] == ws
    assert (ws.parent / "worker_system.md").read_text(encoding="utf-8") == "Вход — состояние экрана. Верни JSON."


async def test_decide_bad_json_twice(settings):
    async def fake_run(cmd, *, cwd, timeout, stdin):
        return _claude_json('{"status": "maybe"}')

    with pytest.raises(WorkerBadJSON) as err:
        await ClaudeWorker(settings, run=fake_run).decide("ЭКРАН", settings.tasks_dir / "6" / "workspace")
    assert err.value.replies == ['{"status": "maybe"}'] * 2
    assert err.value.errors[0].startswith("status: Input should be")


async def test_fake_worker_decisions(tmp_path):
    w = FakeWorker(decisions=[
        Decision(status="continue", actions=[Action(action="click", element=1)]),
        lambda text: Decision(status="course_done", comment=text[:5]),
    ])
    assert (await w.decide("экран 1", tmp_path)).actions[0].element == 1
    assert (await w.decide("экран 2", tmp_path)).comment == "экран"
    assert (await w.decide("экран 3", tmp_path)).status == "stuck"
    assert w.screens == ["экран 1", "экран 2", "экран 3"]


def test_long_text_and_elements_go_in_parts(tmp_path):
    from app.course import worker_input

    ws = tmp_path / "ws"
    questions = "".join(f"Вопрос {i}. Тип реакции? Подробнее: https://school.moodledemo.net/q/{i}\n" for i in range(1, 900))
    els = [Element(n=i, kind="radio", label=f"вариант {i}") for i in range(1, 1500)]
    out = render(Screen(text=questions, elements=els), [], [], [], [], banned_words(ORIGIN), workspace=ws)
    text_files = sorted((ws / "screens").glob("text-*.md"))
    el_files = sorted((ws / "screens").glob("elements-*.md"))
    assert len(text_files) >= 2 and len(el_files) >= 2
    assert "### Текст страницы (часть 1 из" in out and "screens/text-2.md" in out
    assert "### Элементы (часть 1 из" in out and "screens/elements-2.md" in out
    whole = "".join(p.read_text(encoding="utf-8") for p in text_files)
    assert "Вопрос 899." in whole and "moodledemo" not in whole and "https" not in whole  # ничего не потеряно, всё отфильтровано
    assert "[1499] radio: вариант 1499" in "".join(p.read_text(encoding="utf-8") for p in el_files)
    assert len(out) < worker_input.MAX_TEXT + worker_input.MAX_ELEMENTS + 5000


def test_split_lines():
    from app.course.worker_input import split_lines

    assert split_lines("a\nb\nc\n", 4) == ["a\nb\n", "c\n"]
    assert split_lines("x" * 10, 4) == ["xxxx", "xxxx", "xx"]
    assert split_lines("", 4) == [""]
