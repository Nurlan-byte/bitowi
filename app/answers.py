"""Questions → worker text, numbered worker answers → per-question dict."""

import re

from app.agents.base import Question
from app.notebook import extract_answer

_ITEM_RE = re.compile(r"^\s*(?:вопрос\s*)?(\d{1,3})\s*[:.)\-—]\s?(.*)$", re.IGNORECASE)

TYPE_HINT = {
    "single_choice": "номер одного варианта",
    "multi_choice": "номера всех верных вариантов через запятую",
    "number": "число",
    "text": "текст",
    "code": "код",
    "file": "содержимое файла",
}


def render_question(q: Question) -> str:
    """Only the condition, options and format: nothing about previous attempts or grades."""
    lines = [f"Вопрос {q.n}.", q.text.strip()]
    if q.options:
        lines.append("Варианты:")
        lines += [f"{i}) {opt}" for i, opt in enumerate(q.options, 1)]
    lines.append("Формат ответа: " + (q.answer_format.strip() or TYPE_HINT.get(q.answer_type, "текст")))
    return "\n".join(lines)


def render_task(questions: list[Question], part: str | None = None) -> str:
    head = f"Часть {part}.\n\n" if part else ""
    body = "\n\n".join(render_question(q) for q in questions)
    if len(questions) > 1:
        tail = (
            "\n\nОтветь на все вопросы. В блоке «ОТВЕТ» — пронумерованный список, "
            "по одной строке на вопрос: «1: …», «2: …»."
        )
    else:
        tail = ""
    return head + body + tail


def parse_answers(reply: str, expected: list[int]) -> dict[int, str]:
    """Split the '### ОТВЕТ' block into answers by question number."""
    block = extract_answer(reply)
    if len(expected) == 1:
        m = _ITEM_RE.match(block.splitlines()[0]) if block else None
        if m and int(m.group(1)) == expected[0] and len(block.splitlines()) == 1:
            return {expected[0]: m.group(2).strip()}
        return {expected[0]: block.strip()}
    result: dict[int, list[str]] = {}
    current: int | None = None
    for line in block.splitlines():
        m = _ITEM_RE.match(line)
        if m and int(m.group(1)) in expected:
            current = int(m.group(1))
            result[current] = [m.group(2)]
        elif current is not None:
            result[current].append(line)
    return {n: "\n".join(parts).strip() for n, parts in result.items()}


def render_answers(answers: dict[int, str]) -> str:
    if list(answers) == [1] or len(answers) == 1:
        return next(iter(answers.values()), "")
    return "\n".join(f"{n}: {answers[n]}" for n in sorted(answers))


def chunk(questions: list[Question], size: int) -> list[list[Question]]:
    return [questions[i : i + size] for i in range(0, len(questions), size)] or [[]]
