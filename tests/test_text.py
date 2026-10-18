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
