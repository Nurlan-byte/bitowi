"""Mini LMS in a background thread for e2e/live tests."""

import json
import socket
import threading
import time
import urllib.request

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def lms():
    import uvicorn

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


def stats(base: str) -> dict:
    return json.loads(urllib.request.urlopen(base + "/_stats").read())


def reset(base: str) -> None:
    urllib.request.urlopen(urllib.request.Request(base + "/_reset", method="POST"))
