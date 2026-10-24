"""Filtering HTTP proxy of the course browser (127.0.0.1, random port).

Chromium sends every request through it (--proxy-server, loopback included): HTTPS and WSS as CONNECT,
plain HTTP and WS in absolute form. The proxy resolves the host itself, checks every IP against
NetPolicy and connects to the checked address — no gap between the check and the connection (DNS
rebinding), every redirect step and every WebSocket goes through the same check.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from app.course.netpolicy import NetPolicy

log = logging.getLogger(__name__)

Resolver = Callable[[str, int], Awaitable[list[str]]]
CONNECT_TIMEOUT = 15


async def system_resolve(host: str, port: int) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


class FilteringProxy:
    def __init__(self, policy: NetPolicy, resolver: Resolver = system_resolve) -> None:
        self.policy = policy
        self.resolver = resolver
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        # отказы: (хост, порт, причина) — для журнала и пометок воркеру
        self.blocked: list[tuple[str, int, str]] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — висящие туннели закроются сами
                pass
            self.server = None

    def take_blocked(self) -> list[tuple[str, int, str]]:
        out, self.blocked = self.blocked, []
        return out

    async def _open(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | str:
        """Connection to a checked IP of host, or the refusal reason."""
        ips = await self.resolver(host, port)
        why = self.policy.endpoint_block(host, port, ips)
        if why:
            self.blocked.append((host, port, why))
            log.warning("proxy blocked %s:%s — %s", host, port, why)
            return why
        for ip in ips:
            try:
                return await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                continue
        return "нет соединения"

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        try:
            line, *headers = head.decode("latin-1").split("\r\n")
            method, target, version = line.split(" ", 2)
        except ValueError:
            await self._refuse(writer, 400, "bad request")
            return
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            await self._tunnel(reader, writer, host.strip("[]").lower(), int(port or 443))
        else:
            await self._forward(reader, writer, method, target, version, headers)

    async def _tunnel(self, reader, writer, host: str, port: int) -> None:
        upstream = await self._open(host, port)
        if isinstance(upstream, str):
            await self._refuse(writer, 403, upstream)
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe(reader, writer, *upstream)

    async def _forward(self, reader, writer, method: str, target: str, version: str, headers: list[str]) -> None:
        parts = urlsplit(target)
        if parts.scheme not in ("http", "ws") or not parts.hostname:
            await self._refuse(writer, 400, "only absolute http URLs")
            return
        host, port = parts.hostname.lower(), parts.port or 80
        upstream = await self._open(host, port)
        if isinstance(upstream, str):
            await self._refuse(writer, 403, upstream)
            return
        up_reader, up_writer = upstream
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        upgrade = any(h.lower().startswith("upgrade:") for h in headers)
        kept = [h for h in headers if h and not h.lower().startswith(("proxy-connection:", "proxy-authorization:"))]
        if not upgrade:
            # один запрос на соединение: следующий запрос браузера (возможно, на другой хост) пройдёт проверку заново
            kept = [h for h in kept if not h.lower().startswith(("connection:", "keep-alive:"))] + ["Connection: close"]
        up_writer.write((f"{method} {path} {version}\r\n" + "\r\n".join(kept) + "\r\n\r\n").encode("latin-1"))
        await up_writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    @staticmethod
    async def _refuse(writer: asyncio.StreamWriter, code: int, why: str) -> None:
        body = f"bitowi: адрес закрыт ({why})".encode()
        writer.write(
            f"HTTP/1.1 {code} Blocked\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()

    @staticmethod
    async def _pipe(c_reader, c_writer, u_reader, u_writer) -> None:
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(copy(c_reader, u_writer), copy(u_reader, c_writer))
