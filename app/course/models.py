"""Data passed between the course layers. Each layer gets these as input and returns them as output."""

import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator


def as_text(v: Any) -> Any:
    """Numbers, lists and small objects → text; other types are left for the validator to reject."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return ", ".join(str(as_text(x)) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{k}: {as_text(x)}" for k, x in v.items())
    return v


ActionKind = Literal["click", "type", "select", "scroll", "upload"]
ElementKind = Literal["button", "link", "text", "password", "textarea", "select", "checkbox", "radio", "file", "other"]


class Action(BaseModel):
    """One step for the courier. element — the number [N] on the screenshot."""

    action: ActionKind
    element: int | None = None
    # type — текст; select — вариант; scroll — up | down; upload — путь внутри workspace
    value: str = ""

    # модель пишет естественно: «"value": 2», «"element": "[12]"», null — приводим, а не отвергаем
    @field_validator("action", mode="before")
    @classmethod
    def _action(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("element", mode="before")
    @classmethod
    def _element(cls, v: Any) -> Any:
        if isinstance(v, str):
            m = re.fullmatch(r"\s*\[?\s*(\d+)\s*\]?\s*", v)
            return int(m.group(1)) if m else v
        return v

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, v: Any) -> Any:
        return as_text(v)


class Element(BaseModel):
    """An interactive element numbered by the overlay."""

    n: int
    kind: ElementKind = "other"
    label: str = ""
    value: str = ""
    checked: bool | None = None
    # варианты списка select
    options: list[str] = []
    disabled: bool = False
    # только для оркестратора — воркеру не передаются
    href: str = ""
    submit_class: bool = False
    # ссылка на другую страницу или пункт меню (navItem в overlay.js) — не сдача
    nav_item: bool = False
    submit: bool = False
    # кнопка или флажок согласия («I understand», honor code) — только после «Нажать» владельца
    consent: bool = False
    blocked: str = ""
    # ссылка ведёт на внешний сайт (не домен курса)
    external: bool = False

    def for_worker(self) -> str:
        line = f"[{self.n}] {self.kind}" + (f": {self.label}" if self.label else "")
        if self.value:
            line += f" = {self.value!r}"
        if self.options:
            line += " — варианты: " + " | ".join(self.options)
        if self.checked is not None:
            line += " (отмечено)" if self.checked else " (не отмечено)"
        if self.disabled:
            line += " (неактивен)"
        if self.external and not self.blocked:
            line += " (внешний сайт)"
        if self.blocked:
            line += " (недоступен)"
        elif self.submit:
            line += " (сдача — только после подтверждения)"
        elif self.consent:
            line += " (согласие — только после подтверждения владельца)"
        return line


class ActionResult(BaseModel):
    action: Action
    ok: bool
    # для воркера — только фразы из кода (courier.ERRORS, policy)
    error: str = ""
    # слова того, кто кликал (исключение Playwright, текст курьера) — только во внутренний журнал
    detail: str = ""


class Screen(BaseModel):
    """What the courier saw: screenshots with numbers, verbatim text, elements, results."""

    shots: list[str] = []
    text: str = ""
    elements: list[Element] = []
    results: list[ActionResult] = []
    # сообщения платформы: «верно/неверно», баллы
    messages: list[str] = []
    progress: str = ""
    # страница длиннее снятых скриншотов — есть смысл прокрутить вниз
    more_below: bool = False
    # SPA успела нарисовать текст или элементы
    rendered: bool = True
    # страница не на домене курса
    external: bool = False
    # только для оркестратора
    url: str = ""
    title: str = ""


class Decision(BaseModel):
    """Worker output for one step."""

    status: Literal["continue", "submit_ready", "confirm_needed", "course_done", "stuck"]
    answers: list[str] = []
    actions: list[Action] = []
    comment: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("answers", mode="before")
    @classmethod
    def _answers(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, dict):
            return [f"{k} — {as_text(a)}" for k, a in v.items()]
        if isinstance(v, list):
            return [as_text(a) for a in v]
        return [as_text(v)]

    @field_validator("comment", mode="before")
    @classmethod
    def _comment(cls, v: Any) -> Any:
        return as_text(v)


class ActionPlan(BaseModel):
    """Worker actions after the policy check."""

    # выполнить сейчас
    run: list[Action] = []
    # сдача: только после «Отправить» владельца
    pending_submit: list[Action] = []
    # согласие: ровно одно нажатие, только после «Нажать» владельца; действия после него не выполняются
    pending_consent: Action | None = None
    dropped: list[Action] = []
    # не выполняются; воркеру — причина в следующем входе
    rejected: list[ActionResult] = []
