"""Patterns for submit buttons and forbidden elements: one source for the orchestrator and guard.js.

Only regex syntax common to Python and JavaScript. \\b is ASCII-only in JS, so it is used
only before English words; Russian stems are matched as substrings.
"""

import json
import re
from pathlib import Path
from typing import Literal

GUARD_TEMPLATE = Path(__file__).resolve().parent / "guard.js"

# сдача и проверка: в режиме курса — только после подтверждения владельца
SUBMIT = r"отправ|сдать|сдач|провер|заверш|сохран|загрузить|\bsubmit|\bcheck\b|\bfinish|\bsave\b|\bsend\b|\bupload"
# навигация — не сдача, даже если совпала с SUBMIT
NAV = (
    r"начать|продолж|далее|назад|следующ|предыдущ|войти|закончить обзор|завершить просмотр|"
    r"\b(?:start|attempt quiz|continue|next|previous|back|log ?in|sign ?in|finish review)\b"
)
# текст состояния, а не действие: «Not submitted», «Submitted», «Graded», «Не сдано», «Оценено» —
# вырезается из подписи перед проверкой на сдачу
STATUS = (
    r"\b(?:not )?(?:submitted|graded|completed)\b|(?:не )?(?:отправлен|сдан|оценен|проверен|выполнен)|на проверк"
)
# согласие и подтверждение: только после «Нажать» владельца — кнопки по тексту...
CONSENT = (
    r"\bi (?:understand|agree|acknowledge|accept|confirm)\b|\baccept\b|\bagree\b|"
    r"я понимаю|я согласен|я согласна|согласен|согласна|принимаю|подтверждаю"
)
# ...и флажки: ещё правила честности и условия
CONSENT_BOX = (
    CONSENT + r"|honou?r code|academic integrity|\bterms\b|кодекс чести|академическ|честност|соглашени|"
    r"условия использования"
)
# показ ответа и сброс попытки: заблокированы всегда
DANGER = r"показать ответ|сброс|\bshow answers?\b|\breveal|\breset"
# чёрный список режима курса — по тексту элемента
DENY = (
    DANGER + r"|выйти|выход|\blog ?out|\bsign ?out|профил|\bprofile|настройк|\bsettings|\bpreferences|"
    r"парол|\bpassword|удал|\bdelete|\bremove|отпис|отказ|покинуть|\bunenrol|\bunsubscribe|"
    r"оплат|купить|\bpay\b|\bpayment|\bbuy\b|\bcheckout|\bbilling|сообщени|\bmessag|"
    r"форум|\bforum|(?:^|[^а-яё])чат|\bchat|коммент|\bcomment|пригла|\binvite"
)
# и по адресу ссылки
DENY_HREF = (
    r"log-?out|sign-?out|/profile|/user/(?:edit|preferences|profile)|preferences|settings|password|"
    r"unenrol|unsubscribe|delete|remove|payment|checkout|billing|/messages?\b|forum|/chat|comment|invite|reset"
)
# CSS-классы кнопок сдачи (Moodle и мини-LMS)
SUBMIT_CLASSES = ".submit, .show, .reset, .check, .save, [data-bitowi-forbidden]"

_FLAGS = re.IGNORECASE
SUBMIT_RE = re.compile(SUBMIT, _FLAGS)
NAV_RE = re.compile(NAV, _FLAGS)
STATUS_RE = re.compile(STATUS, _FLAGS)
CONSENT_RE = re.compile(CONSENT, _FLAGS)
CONSENT_BOX_RE = re.compile(CONSENT_BOX, _FLAGS)
DANGER_RE = re.compile(DANGER, _FLAGS)
DENY_RE = re.compile(DENY, _FLAGS)
DENY_HREF_RE = re.compile(DENY_HREF, _FLAGS)

# sessionStorage: одноразовое разрешение сдачи, ставит оркестратор после «Отправить»
ALLOW_KEY = "__bitowiAllow"

Mode = Literal["extract", "course"]


def is_submit(label: str, submit_class: bool = False, nav_item: bool = False) -> bool:
    """Same rule as isSubmit() in guard.js: a submit/check button, not plain navigation.

    nav_item — a link to another page or a menu item (navItem() in guard.js and overlay.js): never a
    submission, only «show answer» / reset stay forbidden."""
    label = STATUS_RE.sub(" ", label)
    if DANGER_RE.search(label):
        return True
    if nav_item:
        return False
    if submit_class:
        return not NAV_RE.search(label) or bool(SUBMIT_RE.search(label))
    return bool(SUBMIT_RE.search(label)) and not NAV_RE.search(label)


def is_consent(label: str, checkbox: bool = False) -> bool:
    """Same rule as isConsent() in guard.js: «I understand», «Согласен», an honor code checkbox."""
    return bool((CONSENT_BOX_RE if checkbox else CONSENT_RE).search(label))


def guard_script(mode: Mode, net=None) -> str:
    """guard.js with the patterns substituted; passed to add_init_script / --init-script.

    net — app.course.netpolicy.NetPolicy of the course mode: the course site (outside it any form
    needs the owner's allowance) and the exceptions from the ban on local addresses.
    """
    policy = {
        "mode": mode,
        "submit": SUBMIT,
        "nav": NAV,
        "status": STATUS,
        "consent": CONSENT,
        "consent_box": CONSENT_BOX,
        "danger": DANGER,
        "deny": DENY,
        "deny_href": DENY_HREF,
        "submit_classes": SUBMIT_CLASSES,
        "allow_key": ALLOW_KEY,
        "course_site": net.course_site if net else "",
        "exempt": sorted(net.exempt) if net else [],
    }
    return GUARD_TEMPLATE.read_text(encoding="utf-8").replace(
        "__BITOWI_POLICY__", json.dumps(policy, ensure_ascii=False)
    )


def write_guard(path: Path, mode: Mode, net=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(guard_script(mode, net), encoding="utf-8")
    return path
