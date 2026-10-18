"""Shared notebook (notebook.md) through which the client and the executor talk."""

import re
from pathlib import Path

CLIENT = "Заказчик"
EXECUTOR = "Исполнитель"

_ANSWER_RE = re.compile(r"^#{2,4}\s*ОТВЕТ\s*$", re.IGNORECASE | re.MULTILINE)


class Notebook:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def append(self, role: str, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = f"**{role}:**\n\n{text.strip()}\n"
        current = self.read()
        sep = "\n---\n\n" if current else ""
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(sep + entry)


def extract_answer(reply: str) -> str:
    """Return the text under the '### ОТВЕТ' heading, or the whole reply if absent."""
    matches = list(_ANSWER_RE.finditer(reply))
    if not matches:
        return reply.strip()
    tail = reply[matches[-1].end():]
    nxt = re.search(r"^#{1,4}\s", tail, re.MULTILINE)
    return (tail[: nxt.start()] if nxt else tail).strip()
