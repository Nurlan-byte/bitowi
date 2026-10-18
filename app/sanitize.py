"""Filters for text passed between the boss and the worker."""

import re
from collections.abc import Iterable

_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.(?:com|ru|kz|org|net|io|edu|dev|app|ai)\b(?:/\S*)?", re.IGNORECASE)
# служебные теги вида <boss>...</boss>, <private>...</private>, [[...]]
_TAG_BLOCK_RE = re.compile(r"<(boss|private|internal|system)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"</?(?:boss|private|internal|system)\b[^>]*>|\[\[.*?\]\]", re.IGNORECASE | re.DOTALL)

# фразы, выдающие, что исполнитель — модель
_AI_PHRASES = [
    r"как (?:ии|ai|языковая модель|искусственный интеллект|нейросеть|ии-модель|ai-модель)[^.!?\n]*[.!?]?",
    r"я\s*[—-]?\s*(?:ии|языковая модель|нейросеть|искусственный интеллект|claude|ассистент anthropic)[^.!?\n]*[.!?]?",
    r"as an ai(?: language model)?[^.!?\n]*[.!?]?",
    r"i(?:'m| am) (?:an ai|a language model|claude)[^.!?\n]*[.!?]?",
    r"(?:я не могу|i cannot) (?:просматривать|browse|открыть ссылк)[^.!?\n]*[.!?]?",
]
_AI_RE = re.compile("|".join(_AI_PHRASES), re.IGNORECASE)


# следы прошлых попыток: отметки, баллы, показанные ответы — воркеру не передаются
_GRADE_MARKS_RE = re.compile(r"[✓✔✗✘☑☒❌✅]")
# строка-метка с двоеточием: «Правильный ответ: 7», «Your answer: 9», «Баллов: 0 из 1»
_GRADE_LINE_RE = re.compile(
    r"^[ \t]*(?:правильн\w* ответ\w*|верн\w* ответ\w*|ваш ответ|выбранн\w* ответ\w*|"
    r"(?:the )?correct answers?(?: is| are)?|your answers?|right answers?|"
    r"баллы?|баллов|оценка|mark|marks|grade|points?|score|"
    r"попыт\w* осталось|осталось попыт\w*|attempts? (?:left|remaining))[ \t]*[:：][^\n]*$"
    # строка-статус целиком: «Верно», «Incorrect», «Частично верно»
    r"|^[ \t]*(?:верно|неверно|правильно|неправильно|частично верно|correct|incorrect|partially correct)[ \t]*[.!]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
# «Mark 0.00 out of 1.00», «Баллов 1 из 2», «2 из 5 баллов»
_SCORE_RE = re.compile(
    r"(?:\b(?:mark|marks|баллов|балл|points?)\s*:?\s*\d+(?:[.,]\d+)?\s*(?:из|out of)\s*\d+(?:[.,]\d+)?)"
    r"|(?:\b\d+(?:[.,]\d+)?\s*(?:из|out of)\s*\d+(?:[.,]\d+)?\s*(?:балл\w*|points?|marks?)\b)",
    re.IGNORECASE,
)


def _collapse(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def for_worker(text: str, banned: Iterable[str] = ()) -> str:
    """Strip URLs, service tags and banned words (e.g. platform name) before the worker sees text."""
    text = _TAG_BLOCK_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _URL_RE.sub("[ссылка удалена]", text)
    text = _DOMAIN_RE.sub("[ссылка удалена]", text)
    text = _GRADE_MARKS_RE.sub("", text)
    text = _GRADE_LINE_RE.sub("", text)
    text = _SCORE_RE.sub("", text)
    for word in banned:
        if word and word.strip():
            text = re.sub(re.escape(word.strip()), "[…]", text, flags=re.IGNORECASE)
    return _collapse(text)


def for_boss(text: str) -> str:
    """Remove phrases that reveal the executor is a model."""
    return _collapse(_AI_RE.sub("", text))
