"""Data passed between the course layers. Each layer gets these as input and returns them as output."""

from typing import Literal

from pydantic import BaseModel, Field

ActionKind = Literal["click", "type", "select", "scroll", "upload"]
ElementKind = Literal["button", "link", "text", "textarea", "select", "checkbox", "radio", "file", "other"]


class Action(BaseModel):
    """One step for the courier. element — the number [N] on the screenshot."""

    action: ActionKind
    element: int | None = None
    # type — текст; select — вариант; scroll — up | down; upload — путь внутри workspace
    value: str = ""


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
    submit: bool = False
    blocked: str = ""

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
        if self.blocked:
            line += " (недоступен)"
        elif self.submit:
            line += " (сдача — только после подтверждения)"
        return line


class ActionResult(BaseModel):
    action: Action
    ok: bool
    error: str = ""


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
    # только для оркестратора
    url: str = ""
    title: str = ""


class Decision(BaseModel):
    """Worker output for one step."""

    status: Literal["continue", "submit_ready", "course_done", "stuck"]
    answers: list[str] = []
    actions: list[Action] = Field(default_factory=list, max_length=30)
    comment: str = ""


class ActionPlan(BaseModel):
    """Worker actions after the policy check."""

    # выполнить сейчас
    run: list[Action] = []
    # сдача: только после «Отправить» владельца
    pending_submit: list[Action] = []
    # не выполняются; воркеру — причина в следующем входе
    rejected: list[ActionResult] = []
