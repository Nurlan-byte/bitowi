"""Subprocess runner for agent CLIs: no shell, stdin prompt, timeout, kill on timeout/cancel."""

import asyncio
import logging
import os
import re
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from app.agents.base import AgentError, AgentTimeout

log = logging.getLogger(__name__)

_LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|quota exceeded|too many requests|\b429\b|limit reached|out of credits",
    re.IGNORECASE,
)


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


def looks_like_limit(text: str) -> bool:
    return bool(_LIMIT_RE.search(text))


def resolve_bin(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise AgentError(f"Команда не найдена: {name}")
    return found


def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, sig)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        # PermissionError: часть группы — процессы другого пользователя (sudo);
        # их останавливает timeout внутри обёртки
        pass


async def _stop(proc: asyncio.subprocess.Process, grace: float = 10.0) -> None:
    """SIGTERM first (sudo relays it to the command), SIGKILL after `grace` seconds."""
    if proc.returncode is not None:
        return
    _signal(proc, signal.SIGTERM if sys.platform != "win32" else 0)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        _signal(proc, signal.SIGKILL if sys.platform != "win32" else 0)
        await proc.wait()


async def run(cmd: list[str], *, cwd: Path, timeout: float, stdin: str | None = None) -> RunResult:
    """Run a command and return its output. Raises AgentTimeout on timeout."""
    log.info("run %s (cwd=%s, timeout=%ss)", Path(cmd[0]).name, cwd, timeout)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=sys.platform != "win32",
    )
    data = stdin.encode("utf-8") if stdin is not None else None
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout=timeout)
    except asyncio.TimeoutError:
        await _stop(proc)
        raise AgentTimeout(f"{Path(cmd[0]).name}: таймаут {timeout} с")
    except asyncio.CancelledError:
        await asyncio.shield(_stop(proc))
        raise
    return RunResult(
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )
