"""Mini LMS for end-to-end tests: login form, quizzes (radio / checkbox / text), attempts, results.

Correct answers are known (QUIZZES). /_stats counts submissions and «Показать ответ» clicks,
so tests can assert the extraction step never pressed them.

The worker can reach localhost from python3, so nothing that reveals answers is served without
a login (FAKE_LMS_USER / FAKE_LMS_PASSWORD, no default password) and service endpoints need
the X-LMS-Token header (FAKE_LMS_TOKEN).
"""

import html
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

MAX_ATTEMPTS = 3


def _creds() -> tuple[str, str]:
    return os.environ.get("FAKE_LMS_USER", "student"), os.environ.get("FAKE_LMS_PASSWORD", "")


def _token_ok(request: Request) -> bool:
    token = os.environ.get("FAKE_LMS_TOKEN", "")
    return bool(token) and secrets.compare_digest(request.headers.get("X-LMS-Token", ""), token)

QUIZZES: dict[int, dict] = {
    1: {
        "title": "Тест 1. Разминка",
        "questions": [
            {"id": "q1", "type": "radio", "text": "Сколько будет 17·23?", "options": ["381", "391", "401", "371"], "correct": ["2"]},
            {"id": "q2", "type": "checkbox", "text": "Какие из чисел простые?", "options": ["2", "4", "7", "9"], "correct": ["1", "3"]},
            {"id": "q3", "type": "text", "text": "Как называется столица Казахстана? Ответ одним словом.", "options": [], "correct": ["Астана"]},
        ],
    },
    # уже отвеченный тест: на странице прошлый ответ, ✓/✗, баллы и показанный правильный ответ
    2: {
        "title": "Тест 2. Повторение",
        "answered": True,
        "questions": [
            {"id": "q1", "type": "radio", "text": "Какое из чисел простое?", "options": ["4", "6", "7", "9"], "correct": ["3"], "previous": ["4"]},
            {"id": "q2", "type": "text", "text": "Сколько сторон у шестиугольника? Ответ числом.", "options": [], "correct": ["6"], "previous": ["6"]},
        ],
    },
    # большой тест: 17 вопросов — проверка деления на части
    3: {
        "title": "Тест 3. Таблица умножения",
        "questions": [
            {"id": f"q{i}", "type": "text", "text": f"Сколько будет {i}·{i + 1}? Ответ числом.", "options": [], "correct": [str(i * (i + 1))]}
            for i in range(1, 18)
        ],
    },
}

# без /docs и /openapi.json — ничего лишнего анонимам
app = FastAPI(title="Мини-LMS", docs_url=None, redoc_url=None, openapi_url=None)
SESSIONS: set[str] = set()
STATS = {"submit": 0, "show_answer": 0, "logins": 0}
ATTEMPTS: dict[int, int] = {}


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:sans-serif;max-width:760px;margin:24px auto}} .q{{border:1px solid #ccc;padding:12px;margin:12px 0}}
.correct{{color:green}} .wrong{{color:#b00}}</style></head>
<body><header><a href="/">Мини-LMS</a></header><h1>{html.escape(title)}</h1>{body}
</body></html>"""
    )


def logged_in(request: Request) -> bool:
    return request.cookies.get("sid") in SESSIONS


def need_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


@app.get("/")
async def index(request: Request):
    if not logged_in(request):
        return need_login(request)
    links = "".join(f'<li><a href="/quiz/{i}">{html.escape(q["title"])}</a></li>' for i, q in QUIZZES.items())
    links += '<li><a href="/course">Курс: Знакомство с Казахстаном</a></li>'
    return page("Мои курсы", f"<ul>{links}</ul>")


@app.get("/login")
async def login_form(next: str = "/"):
    return page(
        "Вход",
        f"""<form method="post" action="/login"><input type="hidden" name="next" value="{html.escape(next)}">
<p><label>Логин <input name="username"></label></p>
<p><label>Пароль <input name="password" type="password"></label></p>
<button type="submit">Войти</button></form>""",
    )


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    user, password = _creds()
    ok = bool(password) and form.get("username") == user and secrets.compare_digest(str(form.get("password") or ""), password)
    if not ok:
        return page("Вход", '<p class="wrong">Неверный логин или пароль.</p><p><a href="/login">Ещё раз</a></p>')
    sid = secrets.token_hex(16)
    SESSIONS.add(sid)
    STATS["logins"] += 1
    nxt = str(form.get("next") or "/")
    resp = RedirectResponse(nxt if nxt.startswith("/") else "/", status_code=303)
    resp.set_cookie("sid", sid, httponly=True)
    return resp


def render_question(n: int, q: dict, answered: bool) -> str:
    body = f'<div class="q" id="{q["id"]}"><p><b>Вопрос {n}</b></p><p>{html.escape(q["text"])}</p>'
    prev = q.get("previous", [])
    if q["type"] in ("radio", "checkbox"):
        for i, opt in enumerate(q["options"], 1):
            checked = " checked" if answered and str(i) in prev else ""
            disabled = " disabled" if answered else ""
            body += (
                f'<p><label><input type="{q["type"]}" name="{q["id"]}" value="{i}"{checked}{disabled}> '
                f"{html.escape(opt)}</label></p>"
            )
    else:
        value = html.escape(prev[0]) if answered and prev else ""
        body += f'<p><input type="text" name="{q["id"]}" value="{value}"{" disabled" if answered else ""}></p>'
    if answered:
        ok = sorted(prev) == sorted(q["correct"])
        mark = '<span class="correct">✓ Верно</span>' if ok else '<span class="wrong">✗ Неверно</span>'
        correct = ", ".join(q["options"][int(c) - 1] if q["options"] else c for c in q["correct"])
        body += f"<p>{mark}</p><p>Баллов: {1 if ok else 0} из 1</p><p>Правильный ответ: {html.escape(correct)}</p>"
    return body + "</div>"


@app.get("/quiz/{quiz_id}")
async def quiz(request: Request, quiz_id: int):
    if not logged_in(request):
        return need_login(request)
    q = QUIZZES.get(quiz_id)
    if not q:
        return page("Не найдено", "<p>Нет такого теста.</p>")
    answered = q.get("answered", False)
    left = 0 if answered else MAX_ATTEMPTS - ATTEMPTS.get(quiz_id, 0)
    items = "".join(render_question(n, item, answered) for n, item in enumerate(q["questions"], 1))
    buttons = (
        ""
        if answered
        else '<button type="submit" class="submit">Отправить</button> '
        f'<button type="submit" formaction="/quiz/{quiz_id}/show-answer" class="show">Показать ответ</button>'
    )
    return page(
        q["title"],
        f'<p>Осталось попыток: {left}</p><form method="post" action="/quiz/{quiz_id}/submit">{items}{buttons}</form>',
    )


@app.post("/quiz/{quiz_id}/submit")
async def submit(request: Request, quiz_id: int):
    if not logged_in(request):
        return need_login(request)
    q = QUIZZES[quiz_id]
    if ATTEMPTS.get(quiz_id, 0) >= MAX_ATTEMPTS or q.get("answered"):
        return page("Результат", "<p>Попыток не осталось.</p>")
    STATS["submit"] += 1
    ATTEMPTS[quiz_id] = ATTEMPTS.get(quiz_id, 0) + 1
    form = await request.form()
    score = 0
    for item in q["questions"]:
        given = [str(v).strip() for v in form.getlist(item["id"])]
        if item["type"] == "text":
            score += int(bool(given) and given[0].lower() == item["correct"][0].lower())
        else:
            score += int(sorted(given) == sorted(item["correct"]))
    left = MAX_ATTEMPTS - ATTEMPTS[quiz_id]
    verdict = "Все ответы верны" if score == len(q["questions"]) else "Есть ошибки"
    return page("Результат", f"<p id='result'>{verdict}: {score} из {len(q['questions'])}.</p><p>Осталось попыток: {left}</p>")


@app.post("/quiz/{quiz_id}/show-answer")
async def show_answer(request: Request, quiz_id: int):
    if not logged_in(request):
        return need_login(request)
    q = QUIZZES.get(quiz_id)
    if not q:
        return page("Не найдено", "<p>Нет такого теста.</p>")
    STATS["show_answer"] += 1
    items = "".join(
        f"<li>{html.escape(item['text'])} — {html.escape(', '.join(item['correct']))}</li>" for item in q["questions"]
    )
    return page("Ответы", f"<ol>{items}</ol>")


@app.get("/_stats")
async def stats(request: Request):
    if not _token_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({**STATS, "attempts": ATTEMPTS})


@app.post("/_reset")
async def reset(request: Request):
    if not _token_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    STATS.update(submit=0, show_answer=0, logins=0)
    ATTEMPTS.clear()
    SESSIONS.clear()
    course.reset()
    return JSONResponse({"ok": True})


# режим курса — отдельный модуль, использует page/STATS/SESSIONS отсюда
from e2e.fake_lms import course  # noqa: E402

app.include_router(course.router)
