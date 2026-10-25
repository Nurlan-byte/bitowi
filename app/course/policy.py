"""Hard limits of the course mode, checked in code before the courier gets an action.

The same patterns run inside the page (guard.js), so a courier that deviates from the plan is
stopped there too.
"""

from pathlib import Path
from urllib.parse import urlparse

from app.browser.guard import DENY_HREF_RE, DENY_RE, is_consent, is_submit
from app.course.models import Action, ActionPlan, ActionResult, Element
from app.course.netpolicy import NetPolicy

TYPEABLE = ("text", "textarea")
SCROLL = ("up", "down")


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else ""


def same_origin(url: str, origin: str) -> bool:
    return bool(origin) and origin_of(url) == origin.lower()


def classify(el: Element, policy: NetPolicy | str) -> Element:
    """Mark forbidden, submit and external elements. Forbidden wins: such an element is never clicked.

    Links to other public sites are allowed (marked external); local addresses and file: are not.
    """
    if isinstance(policy, str):
        policy = NetPolicy(policy)
    # варианты ответа и поля ввода по тексту не запрещаем: «Как удалить файл?» — это вопрос, не кнопка
    clickable = el.kind in ("button", "link", "other")
    blocked = ""
    if clickable and DENY_RE.search(el.label):
        blocked = "deny"
    elif el.href:
        if DENY_HREF_RE.search(el.href):
            blocked = "deny"
        elif policy.literal_block(el.href):
            blocked = "private"
    external = bool(el.href) and el.href.startswith(("http://", "https://")) and not policy.is_course(el.href)
    submit = el.kind == "file" or (clickable and is_submit(el.label, el.submit_class, el.nav_item))
    # «Принять и отправить» — сдача: её превью и так подтверждает владелец
    consent = not submit and (clickable or el.kind == "checkbox") and is_consent(el.label, el.kind == "checkbox")
    return el.model_copy(update={
        "blocked": blocked, "submit": submit and not blocked, "consent": consent and not blocked, "external": external,
    })


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
    if action.action == "type" and el.kind == "password":
        return f"элемент [{el.n}] — поле пароля, вводить нельзя"
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


def plan_actions(actions: list[Action], elements: list[Element], workspace: Path, ask: int | None = None) -> ActionPlan:
    """Split worker actions: run now / wait for the owner's confirmation / rejected.

    Everything from the first submit action on waits: the following actions belong to the
    screen after the submission. A consent click (or the element the worker asked about —
    status confirm_needed, `ask`) waits alone: the actions after it are dropped.
    """
    by_n = {el.n: el for el in elements}
    plan = ActionPlan()
    for action in actions:
        if plan.pending_consent is not None or plan.dropped:
            plan.dropped.append(action)
            continue
        el = by_n.get(action.element) if action.element is not None else None
        why = _check(action, el, workspace)
        consent = el is not None and action.action == "click" and (el.consent or el.n == ask) and not el.submit
        if why:
            plan.rejected.append(_reject(action, why))
        elif consent and plan.pending_submit:
            # согласие после сдачи — уже другой экран
            plan.dropped.append(action)
        elif plan.pending_submit or (el is not None and el.submit and action.action in ("click", "upload")):
            plan.pending_submit.append(action)
        elif consent:
            plan.pending_consent = action
        else:
            plan.run.append(action)
    return plan
