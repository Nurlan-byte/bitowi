"""Hard limits of the course mode, checked in code before the courier gets an action.

The same patterns run inside the page (guard.js), so a courier that deviates from the plan is
stopped there too.
"""

from pathlib import Path
from urllib.parse import urlparse

from app.browser.guard import DENY_HREF_RE, DENY_RE, is_submit
from app.course.models import Action, ActionPlan, ActionResult, Element

TYPEABLE = ("text", "textarea")
SCROLL = ("up", "down")


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else ""


def same_origin(url: str, origin: str) -> bool:
    return bool(origin) and origin_of(url) == origin.lower()


def classify(el: Element, origin: str) -> Element:
    """Mark forbidden and submit elements. Forbidden wins: such an element is never clicked."""
    # варианты ответа и поля ввода по тексту не запрещаем: «Как удалить файл?» — это вопрос, не кнопка
    clickable = el.kind in ("button", "link", "other")
    blocked = ""
    if clickable and DENY_RE.search(el.label):
        blocked = "deny"
    elif el.href:
        if DENY_HREF_RE.search(el.href):
            blocked = "deny"
        elif el.href.startswith(("http://", "https://")) and not same_origin(el.href, origin):
            blocked = "offsite"
    submit = el.kind == "file" or (clickable and is_submit(el.label, el.submit_class))
    return el.model_copy(update={"blocked": blocked, "submit": submit and not blocked})


def safe_upload_path(value: str, workspace: Path) -> Path | None:
    """A file inside the task workspace, or None."""
    if not value.strip():
        return None
    root = workspace.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def _reject(action: Action, why: str) -> ActionResult:
    return ActionResult(action=action, ok=False, error=why)


def _check(action: Action, el: Element | None, workspace: Path) -> str:
    """Why the action is not allowed, or ''."""
    if action.action == "scroll":
        return "" if action.value in SCROLL else "прокрутка: value — up или down"
    if el is None:
        return f"элемент [{action.element}] не найден"
    if el.blocked:
        return f"элемент [{el.n}] недоступен"
    if el.disabled:
        return f"элемент [{el.n}] неактивен"
    if action.action == "type" and el.kind not in TYPEABLE:
        return f"элемент [{el.n}] — не поле ввода"
    if action.action == "select" and el.kind != "select":
        return f"элемент [{el.n}] — не список"
    if action.action == "upload":
        if el.kind != "file":
            return f"элемент [{el.n}] — не поле файла"
        if not safe_upload_path(action.value, workspace):
            return "файл не найден в рабочей папке"
    if action.action == "click" and el.kind == "file":
        return f"элемент [{el.n}] — поле файла, используй upload"
    return ""


def plan_actions(actions: list[Action], elements: list[Element], workspace: Path) -> ActionPlan:
    """Split worker actions: run now / wait for the owner's confirmation / rejected.

    Everything from the first submit action on waits: the following actions belong to the
    screen after the submission.
    """
    by_n = {el.n: el for el in elements}
    plan = ActionPlan()
    for action in actions:
        el = by_n.get(action.element) if action.element is not None else None
        why = _check(action, el, workspace)
        if why:
            plan.rejected.append(_reject(action, why))
        elif plan.pending_submit or (el is not None and el.submit and action.action in ("click", "upload")):
            plan.pending_submit.append(action)
        else:
            plan.run.append(action)
    return plan
