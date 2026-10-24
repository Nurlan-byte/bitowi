"""Course mode limits in code: submit buttons, black list, domain, action white list, upload paths."""

import json

from app import sanitize
from app.browser.guard import DENY_RE, guard_script, is_submit
from app.course.models import Action, Decision, Element
from app.course.policy import classify, origin_of, plan_actions, safe_upload_path, same_origin

ORIGIN = "https://lms.test"

# (текст, сдача?) — кнопки Moodle и мини-LMS
SUBMIT_LABELS = [
    ("Отправить", True),
    ("Проверить", True),
    ("Завершить попытку...", True),
    ("Отправить всё и завершить", True),
    ("Submit all and finish", True),
    ("Check", True),
    ("Сохранить без отправки", True),
    ("Показать ответ", True),
    ("Далее", False),
    ("Next page", False),
    ("Начать попытку", False),
    ("Продолжить", False),
    ("Закончить обзор", False),
    ("Finish review", False),
    ("Submit feedback", True),  # «back» внутри «feedback» — не навигация
    ("Видео 1", False),
]

DENY_LABELS = ["Выйти", "Log out", "Профиль", "Настройки", "Изменить пароль", "Удалить аккаунт",
               "Отписаться от курса", "Unenrol me", "Оплатить", "Сообщения", "Форум", "Чат", "Комментарии",
               "Пригласить друга", "Сбросить попытку"]
ALLOWED_LABELS = ["Начать попытку", "Модуль 1. Введение", "Далее", "Отметить как просмотренное", "Тест 1"]


def test_submit_labels():
    for label, expected in SUBMIT_LABELS:
        assert is_submit(label) is expected, label


def test_submit_class_without_nav_text():
    assert is_submit("→", submit_class=True)
    assert not is_submit("Далее", submit_class=True)


def test_deny_labels():
    for label in DENY_LABELS:
        assert DENY_RE.search(label), label
    for label in ALLOWED_LABELS:
        assert not DENY_RE.search(label), label  # «Начать» не задевает «чат»


def test_origin():
    assert origin_of("https://LMS.test:8443/a?b") == "https://lms.test:8443"
    assert same_origin("https://lms.test/x", ORIGIN)
    assert not same_origin("https://evil.test/x", ORIGIN)
    assert not same_origin("https://lms.test.evil.test/", ORIGIN)


def el(n, kind="button", label="", **kw) -> Element:
    return classify(Element(n=n, kind=kind, label=label, **kw), ORIGIN)


def test_classify():
    assert el(1, label="Выйти").blocked == "deny"
    assert el(2, "link", "Мой курс", href="https://lms.test/user/profile.php").blocked == "deny"
    wiki = el(3, "link", "Википедия", href="https://wikipedia.org/x")
    assert wiki.blocked == "" and wiki.external  # внешние сайты открыты
    assert el(31, "link", "Роутер", href="http://192.168.0.1/").blocked == "private"
    assert el(32, "link", "Сервис", href="http://localhost:8080/").blocked == "private"
    assert el(33, "link", "Файл", href="file:///etc/passwd").blocked == "private"
    assert not el(34, "link", "Раздел", href="https://sub.lms.test/x").external
    assert el(4, "link", "Модуль 2", href="https://lms.test/course/2").blocked == ""
    assert el(5, label="Отправить").submit
    assert el(6, "file").submit
    # запрещённое не считается сдачей: такой элемент не нажимается никогда
    assert not el(7, label="Удалить и отправить").submit
    # вариант ответа с «опасным» текстом — это вопрос, не кнопка
    assert el(8, "radio", "Удалить файл командой rm").blocked == ""
    assert not el(9, "text", "Проверочное слово").submit


def test_plan_actions(tmp_path):
    (tmp_path / "essay.txt").write_text("x")
    elements = [
        el(1, "radio", "391"),
        el(2, "text", "Ответ"),
        el(3, label="Выйти"),
        el(4, label="Отправить"),
        el(5, "select", "Город"),
        el(6, label="Далее", disabled=True),
        el(7, "file"),
        el(8, label="Подтвердить"),
    ]
    actions = [
        Action(action="click", element=1),
        Action(action="type", element=2, value="Астана"),
        Action(action="click", element=3),
        Action(action="type", element=1, value="x"),
        Action(action="select", element=5, value="Астана"),
        Action(action="click", element=6),
        Action(action="click", element=99),
        Action(action="scroll", value="down"),
        Action(action="scroll", value="left"),
        Action(action="upload", element=7, value="../../etc/passwd"),
        Action(action="click", element=4),
        Action(action="click", element=8),  # после сдачи — тоже ждёт подтверждения
    ]
    plan = plan_actions(actions, elements, tmp_path)
    assert [(a.action, a.element) for a in plan.run] == [("click", 1), ("type", 2), ("select", 5), ("scroll", None)]
    assert [a.element for a in plan.pending_submit] == [4, 8]
    errors = [r.error for r in plan.rejected]
    assert errors == [
        "элемент [3] недоступен",
        "элемент [1] — не поле ввода",
        "элемент [6] неактивен",
        "элемент [99] не найден",
        "прокрутка: value — up или down",
        "файл не найден в рабочей папке",
    ]

    upload = plan_actions([Action(action="upload", element=7, value="essay.txt")], elements, tmp_path)
    assert upload.pending_submit and not upload.run  # загрузка файла — это сдача


def test_safe_upload_path(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("a")
    (tmp_path / "secret.txt").write_text("s")
    assert safe_upload_path("a.txt", ws) == (ws / "a.txt").resolve()
    for bad in ("../secret.txt", str(tmp_path / "secret.txt"), "", "missing.txt", "."):
        assert safe_upload_path(bad, ws) is None, bad


def test_element_for_worker_has_no_href():
    line = el(4, "link", "Модуль 2", href="https://lms.test/course/2").for_worker()
    assert line == "[4] link: Модуль 2" and "lms.test" not in line
    assert "(недоступен)" in el(1, label="Выйти").for_worker()
    assert "подтверждения" in el(5, label="Отправить").for_worker()


def test_decision_schema():
    d = Decision.model_validate_json(json.dumps({
        "status": "submit_ready", "answers": ["1: 391"],
        "actions": [{"action": "click", "element": 3}], "comment": "",
    }))
    assert d.actions[0].element == 3


def test_guard_script_renders_policy():
    js = guard_script("course")
    assert "__BITOWI_POLICY__" not in js and '"mode": "course"' in js


def test_for_course_keeps_grades_drops_answers():
    raw = (
        "Вопрос 1. Сколько будет 17·23?\nВыберите правильный ответ: один вариант\n"
        "Неверно\nБаллов: 0 из 1\nПравильный ответ: 391\nThe correct answer is: 391\n"
        "Подробнее: https://lms.test/q/1 на Moodle"
    )
    out = sanitize.for_course(raw, banned=["Moodle"])
    assert "Неверно" in out and "Баллов: 0 из 1" in out
    assert "Выберите правильный ответ: один вариант" in out  # это условие, не показанный ответ
    assert "391" not in out.split("17·23?")[1]
    assert "lms.test" not in out and "Moodle" not in out
