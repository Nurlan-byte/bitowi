"""Input for the worker on each course step: goal, screen, results of the last actions, short history,
owner's remarks. No URLs, domain, platform name or link targets: the whole text goes through
sanitize.for_course with the banned words below."""

import shutil
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from app import sanitize
from app.course.models import ActionResult, Screen

GOAL = "Пройти учебный курс до конца: материалы, отметки просмотра, тесты, итоговый экзамен."

# названия платформ — воркеру не показываем, даже если они в тексте страницы. Только однозначные:
# «Canvas», «Blackboard» встречаются в учебном тексте — их при нужде добавляют в COURSE_BANNED_WORDS
KNOWN_PLATFORMS = (
    "Moodle", "Stepik", "Coursera", "Udemy", "edX", "Google Classroom", "iSpring", "Thinkific", "Мини-LMS",
)
# общие части доменов — не баним, иначе пострадает обычный текст
_COMMON_LABELS = {"www", "com", "net", "org", "edu", "kz", "ru", "io", "app", "dev", "lms", "school", "learn"}

MAX_TEXT = 20_000
HISTORY = 8


class StepLog(BaseModel):
    """One step of the internal journal, as the worker sees it."""

    n: int
    status: str
    results: list[ActionResult] = []
    comment: str = ""


def banned_words(origin: str, extra: str = "") -> list[str]:
    host = urlparse(origin).hostname or ""
    words = [host] if host else []
    words += [label for label in host.split(".") if len(label) > 3 and label not in _COMMON_LABELS]
    words += list(KNOWN_PLATFORMS)
    words += [w.strip() for w in extra.split(";") if w.strip()]
    # длинные — первыми: сначала весь домен, потом его части
    return sorted(set(words), key=len, reverse=True)


def copy_shots(screen: Screen, workspace: Path) -> list[str]:
    """Screenshots into workspace/screens (the worker reads them there); paths relative to workspace."""
    target = workspace / "screens"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    names = []
    for i, shot in enumerate(screen.shots, 1):
        name = f"screens/{i}.png"
        shutil.copyfile(shot, workspace / name)
        names.append(name)
    return names


def _result_line(r: ActionResult) -> str:
    a = r.action
    what = a.action + (f" [{a.element}]" if a.element is not None else "")
    if a.action in ("type", "select") and a.value:
        what += f" «{a.value[:80]}»"
    elif a.action == "scroll":
        what += f" {a.value}"
    return f"- {what} — " + ("выполнено" if r.ok else f"не выполнено: {r.error}")


def render(
    screen: Screen,
    shots: list[str],
    results: list[ActionResult],
    history: list[StepLog],
    remarks: list[str],
    banned: list[str],
    notes: list[str] = (),
) -> str:
    text = screen.text if len(screen.text) <= MAX_TEXT else screen.text[:MAX_TEXT] + "\n…(текст обрезан)"
    parts = [f"## Цель\n{GOAL}"]

    screen_lines = ["## Экран"]
    if shots:
        screen_lines.append("Скриншоты (сверху вниз): " + ", ".join(shots))
    if screen.more_below:
        screen_lines.append("Страница длиннее скриншотов — ниже есть ещё (scroll down).")
    if screen.progress:
        screen_lines.append(f"Прогресс курса: {screen.progress}")
    if screen.messages:
        screen_lines.append("Сообщения платформы:\n" + "\n".join(f"- {m}" for m in screen.messages))
    parts.append("\n".join(screen_lines))
    parts.append(f"### Текст страницы\n{text.strip() or '—'}")
    parts.append("### Элементы\n" + ("\n".join(e.for_worker() for e in screen.elements) or "—"))

    if results or notes:
        parts.append("## Результат прошлых действий\n" + "\n".join([*map(_result_line, results), *notes]))
    if history:
        lines = []
        for step in history[-HISTORY:]:
            done = sum(r.ok for r in step.results)
            line = f"{step.n}. {step.status}: действий {len(step.results)}, выполнено {done}"
            lines.append(line + (f" — {step.comment[:150]}" if step.comment else ""))
        parts.append("## История шагов\n" + "\n".join(lines))
    if remarks:
        parts.append("## Замечания владельца\n" + "\n".join(f"- {r}" for r in remarks))

    return sanitize.for_course("\n\n".join(parts), banned)
