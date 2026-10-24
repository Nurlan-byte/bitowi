"""Course browser and courier on the mini LMS course: real Chromium, DirectExecutor, no subscriptions."""

import pytest

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")

from app.browser.session import BrowserSessions  # noqa: E402
from app.browser.sites import profile_for  # noqa: E402
from app.course.browser import CourseBrowser  # noqa: E402
from app.course.courier import Courier, DirectExecutor  # noqa: E402
from app.course.models import Action, Screen  # noqa: E402
from app.course.policy import plan_actions  # noqa: E402
from tests.lms_server import LMS_PASSWORD, LMS_USER, lms, reset, stats  # noqa: E402,F401

pytestmark = pytest.mark.e2e

TRAPS = ("Выйти", "Профиль", "Форум курса", "Удалить аккаунт")


@pytest.fixture
def lms_settings(settings, lms):
    reset(lms)
    settings.fake_lms_host = lms.split("://", 1)[1]
    settings.fake_lms_user = LMS_USER
    settings.fake_lms_password = LMS_PASSWORD
    settings.browser_timeout = 15
    return settings


@pytest.fixture
async def courier(lms_settings, lms, tmp_path):
    url = lms + "/course"
    storage = await BrowserSessions(lms_settings).ensure_session(profile_for(url, lms_settings), url)
    browser = CourseBrowser(lms_settings, url, tmp_path / "profile", storage)
    await browser.start()
    ws = tmp_path / "workspace"
    ws.mkdir()
    yield Courier(lms_settings, browser, DirectExecutor(), tmp_path / "run", ws)
    await browser.close()


def find(screen: Screen, label: str) -> int:
    return next(e.n for e in screen.elements if label in e.label)


async def look(courier: Courier, tmp_path) -> Screen:
    return await courier.inspect(tmp_path / "shots")


async def go(courier: Courier, tmp_path, label: str) -> Screen:
    screen = await look(courier, tmp_path)
    res = await courier.execute([Action(action="click", element=find(screen, label))])
    assert res.results[0].ok, res.results[0].error
    return await look(courier, tmp_path)


async def test_overlay_numbers_match_screenshot_and_list(courier, tmp_path):
    screen = await look(courier, tmp_path)
    page = courier.browser.page
    assert [e.n for e in screen.elements] == list(range(1, len(screen.elements) + 1))
    assert screen.shots and open(screen.shots[0], "rb").read(4) == b"\x89PNG"
    assert screen.progress == "0%"
    assert "[1]" not in screen.text and "Модуль 1. Основы" in screen.text

    # бейдж [N] стоит у элемента N, и в доступном имени тоже «[N] …» — так его видит MCP
    pairs = await page.evaluate("""() => {
        const badges = [...document.querySelectorAll('#__bitowi_overlay canvas')];
        return [...document.querySelectorAll('[data-bitowi-n]')].map((el, i) => {
            const r = el.getBoundingClientRect(), b = badges[i].getBoundingClientRect();
            return [Number(el.dataset.bitowiN), `[${badges[i].dataset.n}]`, el.getAttribute('aria-label'),
                    Math.abs(b.left - Math.max(0, r.left - 4)) < 2 && Math.abs(b.top - Math.max(0, r.top - 12)) < 2];
        });
    }""")
    assert len(pairs) == len(screen.elements)
    for (n, badge, aria, near), el in zip(pairs, screen.elements):
        assert badge == f"[{n}]" and aria.startswith(f"[{n}] ") and near and el.n == n

    by_label = {e.label: e for e in screen.elements}
    assert by_label["Выйти"].blocked == "deny"
    assert by_label["Профиль"].blocked == "deny"
    assert by_label["Форум курса"].blocked == "deny"
    assert by_label["Удалить аккаунт"].blocked == "deny"
    assert not by_label["Полезные материалы"].blocked and by_label["Полезные материалы"].external
    assert not by_label["Продолжить курс"].blocked and not by_label["Продолжить курс"].submit
    assert all("example.org" not in e.for_worker() for e in screen.elements)


async def test_reinspect_keeps_labels_clean(courier, tmp_path):
    first = await look(courier, tmp_path)
    second = await look(courier, tmp_path)
    assert [e.label for e in first.elements] == [e.label for e in second.elements]


async def test_long_page_several_shots(lms_settings, lms, tmp_path):
    lms_settings.course_viewport = "800x150"
    lms_settings.course_max_shots = 3
    url = lms + "/course"
    storage = await BrowserSessions(lms_settings).ensure_session(profile_for(url, lms_settings), url)
    browser = CourseBrowser(lms_settings, url, tmp_path / "p", storage)
    await browser.start()
    try:
        screen = await browser.inspect(tmp_path / "shots", lms_settings.course_max_shots)
        assert len(screen.shots) == 3 and screen.more_below
        await browser.scroll("down")
        assert await browser.page.evaluate("scrollY") > 0
    finally:
        await browser.close()


async def test_walk_module_and_quiz_needs_allowance(courier, tmp_path, lms):
    await go(courier, tmp_path, "Продолжить курс")
    screen = await go(courier, tmp_path, "Далее")  # видео
    screen = await go(courier, tmp_path, "Отметить как просмотренное")
    assert "Просмотрено" in screen.text
    screen = await go(courier, tmp_path, "Далее")  # тест 1
    assert "Тест 1" in screen.title

    radio = next(e.n for e in screen.elements if e.kind == "radio" and e.label == "Астана")
    text = next(e.n for e in screen.elements if e.kind == "text")
    check = find(screen, "Проверить")
    actions = [
        Action(action="click", element=radio),
        Action(action="type", element=text, value="Иртыш"),
        Action(action="click", element=check),
    ]
    plan = plan_actions(actions, screen.elements, courier.workspace)
    assert [a.element for a in plan.pending_submit] == [check] and len(plan.run) == 2

    res = await courier.execute(plan.run)
    assert all(r.ok for r in res.results)
    # курьер «ошибся» и нажал сдачу без разрешения — guard не пустил
    res = await courier.execute(plan.pending_submit)
    assert res.results[0].error == f"элемент [{check}] недоступен"
    assert stats(lms)["quiz_checks"] == 0

    res = await courier.execute(plan.pending_submit, allow_submit=True)
    assert res.results[0].ok
    screen = await look(courier, tmp_path)
    assert "Тест пройден" in screen.text and "Баллы: 2 из 2" in screen.text
    assert any("Тест пройден" in m for m in screen.messages)
    assert stats(lms)["quiz_checks"] == 1
    # разрешение одноразовое: снято после вызова
    assert await courier.browser.page.evaluate("sessionStorage.getItem('__bitowiAllow')") is None


async def test_traps_never_fire(courier, tmp_path, lms):
    screen = await look(courier, tmp_path)
    actions = [Action(action="click", element=find(screen, t)) for t in TRAPS]
    # мимо policy, как будто курьер отклонился от плана: остановить должен guard в странице
    res = await courier.execute(actions, allow_submit=True)
    assert [r.ok for r in res.results] == [False] * len(TRAPS)
    assert courier.browser.page.url == lms + "/course" and not res.offsite
    assert stats(lms)["traps"] == {"logout": 0, "profile": 0, "forum": 0, "delete": 0}


async def test_local_address_blocked_and_rolled_back(courier, tmp_path, lms):
    browser = courier.browser
    other = lms.replace("127.0.0.1", "localhost") + "/course"  # тот же сервер, но не origin курса — локальный адрес
    await browser.page.evaluate("(u) => { location.href = u }", other)
    await browser.page.wait_for_timeout(300)
    assert browser.page.url == lms + "/course"
    assert any("localhost" in n and "локальный адрес" in n for n in browser.take_notes())

    # без маршрута кода — закрывает прокси (403), вкладка откатывается на страницу курса
    await browser.context.unroute("**/*")
    await browser.page.goto(other)
    assert "адрес закрыт" in await browser.page.inner_text("body")
    assert (await browser.check_domain()).startswith(other.rsplit("/", 1)[0])
    assert browser.page.url == lms + "/course"


async def test_stale_numbers_are_not_clicked(courier, tmp_path, lms):
    screen = await look(courier, tmp_path)
    await courier.browser.goto(lms + "/course/item/r1")  # страница сменилась без нового осмотра
    res = await courier.execute([Action(action="click", element=find(screen, "Продолжить курс"))])
    assert res.results[0].error == "страница сменилась — номера устарели"


async def test_exam_submission_with_confirmation(courier, tmp_path, lms):
    await courier.browser.goto(lms + "/course/item/exam")
    screen = await look(courier, tmp_path)
    radio = next(e.n for e in screen.elements if e.kind == "radio" and e.label == "391")
    text = next(e.n for e in screen.elements if e.kind == "text")
    finish = find(screen, "Завершить попытку")
    assert next(e for e in screen.elements if e.n == finish).submit
    await courier.execute([Action(action="click", element=radio), Action(action="type", element=text, value="2")])

    res = await courier.execute([Action(action="click", element=finish)])
    assert not res.results[0].ok and "сводка" not in await courier.browser.page.title()

    await courier.execute([Action(action="click", element=finish)], allow_submit=True)
    screen = await look(courier, tmp_path)
    assert "сводка" in screen.title
    await courier.execute([Action(action="click", element=find(screen, "Отправить всё и завершить"))], allow_submit=True)
    screen = await look(courier, tmp_path)  # окно подтверждения открылось — у его кнопки свой номер
    confirm = [e.n for e in screen.elements if "Отправить всё и завершить" in e.label][-1]
    assert stats(lms)["exam_submits"] == 0
    await courier.execute([Action(action="click", element=confirm)], allow_submit=True)
    screen = await look(courier, tmp_path)
    assert "Экзамен сдан: 2 из 2" in screen.text
    assert stats(lms)["exam_submits"] == 1


def test_course_pages_need_login(lms):
    from tests.test_e2e_fake_lms import _anon

    for path in ("/course", "/course/item/q1", "/course/item/exam", "/course/scheme.svg"):
        status, body = _anon(lms + path)
        assert status == 303 and "4721" not in body and "Иртыш" not in body, path


async def test_hidden_and_labelledby_switches(courier, tmp_path, lms):
    """Moodle radio with aria-labelledby, hidden radio in a label, display:none radio, hidden checkbox."""
    await courier.browser.goto(lms + "/course/radios")
    screen = await look(courier, tmp_path)
    radios = [e for e in screen.elements if e.kind == "radio"]
    boxes = [e for e in screen.elements if e.kind == "checkbox"]
    assert len(radios) == 9 and len(boxes) == 3
    assert [e.label for e in radios[:3]] == ["a. Замещение", "b. Обмен", "c. Соединение"]  # текст из aria-labelledby
    assert all(e.label in ("Замещение", "Обмен", "Соединение") for e in radios[3:] + boxes)
    assert all(e.checked is False for e in radios + boxes)

    # номер — в доступном имени у всех, в том числе у подписи скрытого переключателя
    names = await courier.browser.page.evaluate(
        "() => [...document.querySelectorAll('[data-bitowi-n]')].map(el => [el.getAttribute('aria-label'), "
        "el.hasAttribute('aria-labelledby'), el.getAttribute('role')])"
    )
    for e in radios + boxes:
        aria, labelledby, _ = names[e.n - 1]
        assert aria.startswith(f"[{e.n}] ") and not labelledby

    picks = [radios[1], radios[5], radios[6], boxes[0], boxes[2]]
    res = await courier.execute([Action(action="click", element=e.n) for e in picks])
    assert all(r.ok for r in res.results), [r.error for r in res.results]
    screen = await look(courier, tmp_path)
    checked = {e.n for e in screen.elements if e.checked}
    assert checked == {e.n for e in picks}
    # после повторного осмотра исходная разметка восстановлена и снова заменена — без накопления
    assert await courier.browser.page.evaluate("document.getElementById('m1').getAttribute('aria-labelledby')") is None
    await courier.execute([Action(action="click", element=find(screen, "Проверить"))], allow_submit=True)
    assert stats(lms)["radios"] == {"m": ["2"], "h": ["3"], "d": ["1"], "c": ["1", "3"]}
