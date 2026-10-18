from app import sanitize
from app.notebook import CLIENT, EXECUTOR, Notebook, extract_answer


def test_notebook_roles(tmp_path):
    nb = Notebook(tmp_path / "t" / "notebook.md")
    nb.append(CLIENT, "Задание")
    nb.append(EXECUTOR, "Ответ")
    text = nb.read()
    assert text.index("**Заказчик:**") < text.index("**Исполнитель:**")
    assert "---" in text


def test_extract_answer():
    assert extract_answer("### Решение\nшаги\n\n### ОТВЕТ\n391\n") == "391"
    assert extract_answer("### ОТВЕТ\n2\n\n### Примечание\nx") == "2"
    assert extract_answer("просто 5") == "просто 5"


def test_for_worker_strips_urls_tags_and_banned():
    raw = "Задание с https://lms.example.kz/q/1 и www.site.ru, <private>url</private> [[служебное]] на Stepik"
    out = sanitize.for_worker(raw, banned=["Stepik"])
    for bad in ("https", "example", "www.site.ru", "url", "служебное", "Stepik"):
        assert bad not in out


def test_for_worker_keeps_math():
    assert sanitize.for_worker("Сколько будет 17*23? Ответ числом.") == "Сколько будет 17*23? Ответ числом."


def test_for_boss_strips_ai_phrases():
    out = sanitize.for_boss("Как языковая модель, я думаю так. As an AI language model, sure.\n### ОТВЕТ\n391")
    assert "модель" not in out and "AI" not in out
    assert out.endswith("391")


def test_for_worker_strips_grades_and_shown_answers():
    raw = (
        "Вопрос 1.\nКакое число простое?\n✗ Неверно\nВаш ответ: 9\nПравильный ответ: 7\n"
        "Баллов: 0 из 1\nMark 0.00 out of 1.00\nThe correct answer is: 7\nОсталось попыток: 0\nВерно\n"
    )
    out = sanitize.for_worker(raw)
    assert "Какое число простое?" in out
    for leak in ("✗", "Неверно", "Ваш ответ", "Правильный ответ", "Баллов", "Mark", "correct answer", "попыток"):
        assert leak not in out, leak
    assert "Верно" not in out.splitlines()


def test_for_worker_keeps_condition_words():
    text = "Points A and B lie on a line.\nВерно ли, что 2 — простое?\nScore the essay from 1 to 5."
    assert sanitize.for_worker(text) == text


def test_parse_answers_numbered_and_multiline():
    from app.answers import parse_answers

    reply = "### ОТВЕТ\n1: 2\n2) 1, 3\n3. def f():\n    return 1\n"
    assert parse_answers(reply, [1, 2, 3]) == {1: "2", 2: "1, 3", 3: "def f():\n    return 1"}
    assert parse_answers("### ОТВЕТ\n391", [1]) == {1: "391"}
    assert parse_answers("### ОТВЕТ\n16: 32\n17: 34", [16, 17]) == {16: "32", 17: "34"}
    assert parse_answers("### ОТВЕТ\n5: 10", [5]) == {5: "10"}
