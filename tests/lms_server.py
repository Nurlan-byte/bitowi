"""Mini LMS in a background thread for e2e/live tests."""

import json
import os
import secrets
import socket
import threading
import time
import urllib.request

import pytest

# пароль и токен — случайные на каждый прогон, как и должно быть на сервере
LMS_USER = "student"
LMS_PASSWORD = secrets.token_hex(8)
LMS_TOKEN = secrets.token_hex(8)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def lms():
    import uvicorn

    os.environ.update(FAKE_LMS_USER=LMS_USER, FAKE_LMS_PASSWORD=LMS_PASSWORD, FAKE_LMS_TOKEN=LMS_TOKEN)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config("e2e.fake_lms.app:app", host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/rules", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def _req(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"X-LMS-Token": LMS_TOKEN})


def stats(base: str) -> dict:
    return json.loads(urllib.request.urlopen(_req(base + "/_stats")).read())


def reset(base: str) -> None:
    urllib.request.urlopen(_req(base + "/_reset", "POST"))
