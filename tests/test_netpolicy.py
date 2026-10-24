"""Network policy of the course browser: address table, literal/endpoint checks, filtering proxy, launch flags."""

import asyncio
import inspect

import pytest

from app.course.netpolicy import NetPolicy, is_private_ip, split_course_input
from app.course.proxy import FilteringProxy

# --- таблица адресов ---------------------------------------------------------

CLOSED_IPS = [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.0.21",
    "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "fd12:3456::1", "fe80::1",
    "::ffff:127.0.0.1", "::ffff:192.168.0.1", "[::1]", "fe80::1%eth0", "224.0.0.1",
]
OPEN_IPS = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"]


@pytest.mark.parametrize("ip", CLOSED_IPS)
def test_private_ips_closed(ip):
    assert is_private_ip(ip)


@pytest.mark.parametrize("ip", OPEN_IPS)
def test_public_ips_open(ip):
    assert not is_private_ip(ip)


def test_hostnames_are_not_ips():
    assert not is_private_ip("example.com")
    assert not is_private_ip("")


# --- проверка по адресу без DNS ---------------------------------------------

COURSE = "https://lms.example.org/course/1"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/", "http://localhost:5432/", "http://app.localhost/", "http://192.168.0.1/",
    "https://10.1.2.3/", "http://[::1]:8000/", "ws://127.0.0.1:9222/devtools", "http://169.254.169.254/",
])
def test_literal_local_urls_closed(url):
    assert NetPolicy(COURSE).literal_block(url) == "локальный адрес"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "chrome://settings", "view-source:https://a.com"])
def test_blocked_schemes(url):
    assert NetPolicy(COURSE).literal_block(url).startswith("схема")


@pytest.mark.parametrize("url", ["https://www.coursera.org/learn/x", "https://cdn.example.net/app.js", COURSE])
def test_public_urls_open(url):
    assert NetPolicy(COURSE).literal_block(url) == ""


def test_course_origin_on_lan_is_exempt():
    policy = NetPolicy("http://192.168.0.50:8000/course")
    assert policy.literal_block("http://192.168.0.50:8000/page") == ""
    # тот же хост, другой порт — уже не исключение
    assert policy.literal_block("http://192.168.0.50:5432/") == "локальный адрес"


def test_private_allow_list():
    policy = NetPolicy(COURSE, private_allow="10.0.0.5:8443, lab.local:80")
    assert policy.literal_block("https://10.0.0.5:8443/") == ""
    assert policy.literal_block("https://10.0.0.6:8443/") == "локальный адрес"


# --- проверка после DNS (подмена DNS / rebinding) ----------------------------

def test_endpoint_name_resolving_to_private_ip_closed():
    policy = NetPolicy(COURSE)
    assert policy.endpoint_block("rebind.example.com", 443, ["127.0.0.1"]) == "имя указывает на локальный адрес"
    # хотя бы один приватный адрес в ответе DNS закрывает имя целиком
    assert policy.endpoint_block("mixed.example.com", 443, ["8.8.8.8", "10.0.0.1"]) == "имя указывает на локальный адрес"
    assert policy.endpoint_block("v6.example.com", 443, ["::ffff:192.168.1.1"]) == "имя указывает на локальный адрес"


def test_endpoint_unresolved_closed_and_public_open():
    policy = NetPolicy(COURSE)
    assert policy.endpoint_block("nowhere.example", 443, []) == "имя не резолвится"
    assert policy.endpoint_block("cdn.example.net", 443, ["93.184.216.34"]) == ""


def test_endpoint_course_exempt_even_if_private():
    policy = NetPolicy("http://lms.lan:8080/")
    assert policy.endpoint_block("lms.lan", 8080, ["192.168.0.50"]) == ""
    assert policy.endpoint_block("lms.lan", 22, ["192.168.0.50"]) == "имя указывает на локальный адрес"


# --- «домены:» и разбор /course ----------------------------------------------

def test_domains_limit_navigation_only_to_listed():
    policy = NetPolicy(COURSE, domains=["github.com"])
    assert policy.navigation_allowed("https://github.com/x")
    assert policy.navigation_allowed("https://gist.github.com/x")
    assert policy.navigation_allowed("https://other.example.org/")  # тот же сайт, что и курс
    assert not policy.navigation_allowed("https://evil.com/")
    assert NetPolicy(COURSE).navigation_allowed("https://evil.com/")  # без списка — любые публичные


def test_split_course_input():
    url, instructions, domains = split_course_input(
        "https://lms.example.org/c | только тесты | домены: GitHub.com, https://colab.research.google.com/x"
    )
    assert url == "https://lms.example.org/c"
    assert instructions == "только тесты"
    assert domains == ["github.com", "colab.research.google.com"]


# --- фильтрующий прокси ------------------------------------------------------

async def _upstream():
    """Local stand-in for an allowed upstream: answers any request with «ok»."""
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _proxy(course_url: str, table: dict[str, list[str]]):
    async def resolver(host: str, port: int) -> list[str]:
        return table.get(host, [])

    proxy = FilteringProxy(NetPolicy(course_url), resolver)
    await proxy.start()
    return proxy


async def _ask(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return data


async def test_proxy_forwards_to_exempt_course_origin():
    server, up = await _upstream()
    proxy = await _proxy(f"http://course.test:{up}/", {"course.test": ["127.0.0.1"]})
    try:
        resp = await _ask(proxy.port, f"GET http://course.test:{up}/ HTTP/1.1\r\nHost: course.test\r\n\r\n".encode())
        assert resp.startswith(b"HTTP/1.1 200") and resp.endswith(b"ok")
        assert proxy.take_blocked() == []
    finally:
        await proxy.close()
        server.close()


async def test_proxy_refuses_name_resolving_to_private_ip_http():
    server, up = await _upstream()
    proxy = await _proxy("https://lms.example.org/", {"rebind.test": ["127.0.0.1"]})
    try:
        resp = await _ask(proxy.port, f"GET http://rebind.test:{up}/ HTTP/1.1\r\nHost: rebind.test\r\n\r\n".encode())
        assert resp.startswith(b"HTTP/1.1 403")
        assert proxy.take_blocked() == [("rebind.test", up, "имя указывает на локальный адрес")]
    finally:
        await proxy.close()
        server.close()


async def test_proxy_refuses_connect_to_private_ip():
    proxy = await _proxy("https://lms.example.org/", {"rebind.test": ["10.0.0.7"]})
    try:
        assert (await _ask(proxy.port, b"CONNECT rebind.test:443 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 403")
        assert (await _ask(proxy.port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 403")
        assert (await _ask(proxy.port, b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 403")
    finally:
        await proxy.close()


async def test_proxy_connect_tunnel_to_exempt_origin():
    server, up = await _upstream()
    proxy = await _proxy(f"http://course.test:{up}/", {"course.test": ["127.0.0.1"]})
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(f"CONNECT course.test:{up} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"GET / HTTP/1.1\r\nHost: course.test\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 5)).endswith(b"ok")
        writer.close()
    finally:
        await proxy.close()
        server.close()


async def test_proxy_refuses_websocket_upgrade_to_private_ip():
    proxy = await _proxy("https://lms.example.org/", {"ws.rebind.test": ["192.168.0.21"]})
    try:
        resp = await _ask(proxy.port, (
            b"GET ws://ws.rebind.test:9222/devtools HTTP/1.1\r\nHost: ws.rebind.test\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        ))
        assert resp.startswith(b"HTTP/1.1 403")
    finally:
        await proxy.close()


async def test_proxy_refuses_unresolvable_and_relative_targets():
    proxy = await _proxy("https://lms.example.org/", {})
    try:
        assert (await _ask(proxy.port, b"GET http://nowhere.test/ HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 403")
        assert (await _ask(proxy.port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")).startswith(b"HTTP/1.1 400")
        assert (await _ask(proxy.port, b"GET file:///etc/passwd HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 400")
    finally:
        await proxy.close()


async def test_proxy_listens_on_loopback_only():
    proxy = await _proxy("https://lms.example.org/", {})
    try:
        assert proxy.server.sockets[0].getsockname()[0] == "127.0.0.1"
    finally:
        await proxy.close()


# --- запуск браузера ---------------------------------------------------------

def test_course_browser_launch_settings():
    from app.course.browser import CourseBrowser

    src = inspect.getsource(CourseBrowser.start)
    for needle in (
        "--proxy-server=http://127.0.0.1:", "--proxy-bypass-list=<-loopback>", "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", 'service_workers="block"',
        "accept_downloads=False", "route_web_socket", "--remote-debugging-address=127.0.0.1",
    ):
        assert needle in src, needle
