"""Input for the worker on each course step: goal, screen, results of the last actions, short history,
owner's remarks. No URLs, domain, platform name or link targets: the whole text goes through
sanitize.for_course with the banned words below."""

import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from app import sanitize
from app.course.models import ActionResult, Screen

GOAL = "Пройти учебный курс до конца по правилам прохождения ниже."

# правила выбора заданий: PROMPTS_DIR/course_rules.md, читается на каждом шаге (правки — без перезапуска);
# нет файла или он пустой — эти
RULES_FILE = "course_rules.md"
DEFAULT_RULES = """\
- Идти по порядку курса: модуль за модулем, сверху вниз.
- Материалы для чтения: открыть, при необходимости прокрутить до конца, перейти дальше.
- Видео: отметить «просмотрено», если есть такая кнопка.
- Тесты и задания с ответами: решить и сдать (сдача — только после подтверждения владельца).
- Итоговый экзамен — после остальных элементов курса.
- Уже пройденное (отмечено как выполненное) повторно не проходить.
- Форумы, чаты, обсуждения, сообщения и внешние ссылки пропускать.
- Тест проверен, есть ошибки и остались попытки — исправить ответы и сдать ещё раз; попыток нет — идти дальше.
- Всё пройдено или прогресс 100% — курс пройден."""


def load_rules(prompts_dir: Path) -> str:
    """The owner's rules file, re-read on every step; missing or empty → DEFAULT_RULES."""
    try:
        text = (prompts_dir / RULES_FILE).read_text(encoding="utf-8")
        # комментарии <!-- … --> — для владельца, воркеру не идут
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    except OSError:
        text = ""
    return text or DEFAULT_RULES

# названия платформ — воркеру не показываем, даже если они в тексте страницы. Только однозначные:
# «Canvas», «Blackboard» встречаются в учебном тексте — их при нужде добавляют в COURSE_BANNED_WORDS
KNOWN_PLATFORMS = (
    "Moodle", "Stepik", "Coursera", "Udemy", "edX", "Google Classroom", "iSpring", "Thinkific", "Мини-LMS",
)
# общие части доменов — не баним, иначе пострадает обычный текст
_COMMON_LABELS = {"www", "com", "net", "org", "edu", "kz", "ru", "io", "app", "dev", "lms", "school", "learn"}

MAX_TEXT = 20_000
MAX_ELEMENTS = 30_000
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


def split_lines(text: str, limit: int) -> list[str]:
    """Parts of at most `limit` chars, cut at line ends (a single huge line is cut as is)."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur or not parts:
        parts.append(cur)
    return parts


def _in_parts(title: str, body: str, limit: int, name: str, workspace: Path | None, banned: list[str]) -> str:
    """Short body inline; a long one — the first part inline, all parts as files in workspace/screens."""
    chunks = split_lines(body, limit)
    if len(chunks) == 1:
        return f"### {title}\n{chunks[0].strip() or '—'}"
    if workspace is None:
        return f"### {title}\n{chunks[0].strip()}\n…(обрезано)"
    (workspace / "screens").mkdir(parents=True, exist_ok=True)
    files = []
    for i, chunk in enumerate(chunks, 1):
        rel = f"screens/{name}-{i}.md"
        (workspace / rel).write_text(sanitize.for_course(chunk, banned), encoding="utf-8")
        files.append(rel)
    return (
        f"### {title} (часть 1 из {len(chunks)})\n{chunks[0].strip()}\n\n"
        f"…продолжение — в файлах (прочитай все части перед ответом): {', '.join(files)}"
    )


def render(
    screen: Screen,
    shots: list[str],
    results: list[ActionResult],
    history: list[StepLog],
    remarks: list[str],
    banned: list[str],
    notes: list[str] = (),
    workspace: Path | None = None,
    rules: str = DEFAULT_RULES,
    instructions: str = "",
) -> str:
    parts = [f"## Цель\n{GOAL}", f"## Правила прохождения\n{rules.strip() or DEFAULT_RULES}"]
    if instructions.strip():
        parts.append(f"## Указания владельца на этот курс (важнее правил прохождения)\n{instructions.strip()}")

    screen_lines = ["## Экран"]
    if shots:
        screen_lines.append("Скриншоты (сверху вниз): " + ", ".join(shots))
    if screen.more_below:
        screen_lines.append("Страница длиннее скриншотов — ниже есть ещё (scroll down).")
    if not screen.rendered:
        screen_lines.append("Страница не отрисовалась (белый экран): подожди шаг без действий или верни stuck.")
    if screen.external:
        screen_lines.append("Это внешний сайт (не домен курса): входа здесь нет, формы отправляются только "
                            "после подтверждения владельца.")
    if screen.progress:
        screen_lines.append(f"Прогресс курса: {screen.progress}")
    if screen.messages:
        screen_lines.append("Сообщения платформы:\n" + "\n".join(f"- {m}" for m in screen.messages))
    parts.append("\n".join(screen_lines))
    # длинная страница — частями, как скриншоты: первая часть здесь, все — файлами в screens/
    parts.append(_in_parts("Текст страницы", screen.text, MAX_TEXT, "text", workspace, banned))
    elements = "\n".join(e.for_worker() for e in screen.elements)
    parts.append(_in_parts("Элементы", elements, MAX_ELEMENTS, "elements", workspace, banned))

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
