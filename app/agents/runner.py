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


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


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
        _kill(proc)
        await proc.wait()
        raise AgentTimeout(f"{Path(cmd[0]).name}: таймаут {timeout} с")
    except asyncio.CancelledError:
        _kill(proc)
        await proc.wait()
        raise
    return RunResult(
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )
