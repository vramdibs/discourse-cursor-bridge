"""Run cursor-agent in headless mode with a queue, timeout and early-exit.

cursor-agent is invoked with --output-format stream-json so it emits one
JSON object per line. The final object has type=="result" and contains the
answer in `result`. We watch stdout line by line and kill the process as
soon as we see that final object - this side-steps the known issue where
`cursor-agent --print` fails to exit on its own.

Executions are serialized through a single asyncio.Lock so concurrent
webhooks do not race in the shared workspace / shared MCP servers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from dataclasses import dataclass


log = logging.getLogger(__name__)


@dataclass
class AgentResult:
    ok: bool
    text: str
    raw_stdout: str
    raw_stderr: str
    exit_code: int | None
    duration_sec: float
    timed_out: bool
    is_error: bool = False
    usage: dict | None = None


class AgentRunner:
    def __init__(
        self,
        binary: str,
        workspace: str,
        timeout_sec: int,
        model: str = "",
    ):
        self._binary = binary
        self._workspace = workspace
        self._timeout = timeout_sec
        self._model = model
        self._lock = asyncio.Lock()

    async def run(self, prompt: str) -> AgentResult:
        async with self._lock:
            return await self._run_locked(prompt)

    async def _run_locked(self, prompt: str) -> AgentResult:
        argv = [
            self._binary,
            "-p",
            "--output-format", "stream-json",
            "--force",
            "--trust",
            "--workspace", self._workspace,
        ]
        if self._model:
            argv += ["--model", self._model]
        argv += [prompt]

        log.info("starting cursor-agent (model=%s, timeout=%ds)", self._model or "default", self._timeout)
        loop = asyncio.get_running_loop()
        started = loop.time()

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace,
            start_new_session=True,
        )

        stdout_lines: list[str] = []
        stderr_chunks: list[bytes] = []
        result_text: str | None = None
        is_error = False
        usage: dict | None = None
        timed_out = False
        early_exit = False

        async def drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                stderr_chunks.append(chunk)

        async def read_stdout() -> None:
            nonlocal result_text, is_error, usage, early_exit
            assert proc.stdout is not None
            while True:
                line_b = await proc.stdout.readline()
                if not line_b:
                    return
                line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.strip():
                    continue
                stdout_lines.append(line)
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "result":
                    result_text = obj.get("result") if isinstance(obj.get("result"), str) else None
                    is_error = bool(obj.get("is_error"))
                    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
                    early_exit = True
                    return

        stderr_task = asyncio.create_task(drain_stderr())
        stdout_task = asyncio.create_task(read_stdout())

        try:
            await asyncio.wait_for(stdout_task, timeout=self._timeout)
        except asyncio.TimeoutError:
            timed_out = True
            log.warning("cursor-agent stdout timeout after %ds, killing pid=%s", self._timeout, proc.pid)

        await _terminate_process_group(proc)
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("cursor-agent did not die after kill, giving up")

        duration = loop.time() - started
        stdout = "\n".join(stdout_lines)
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        exit_code = proc.returncode

        if result_text is None and not timed_out:
            result_text = _fallback_text_from_stream(stdout_lines)

        ok = (not timed_out) and (not is_error) and bool(result_text and result_text.strip())
        log.info(
            "cursor-agent finished: ok=%s early_exit=%s exit_code=%s duration=%.1fs is_error=%s usage=%s",
            ok, early_exit, exit_code, duration, is_error, usage,
        )

        return AgentResult(
            ok=ok,
            text=result_text or "",
            raw_stdout=stdout,
            raw_stderr=stderr,
            exit_code=exit_code,
            duration_sec=duration,
            timed_out=timed_out,
            is_error=is_error,
            usage=usage,
        )


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        import os
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass
    try:
        import os
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            return


def _fallback_text_from_stream(lines: list[str]) -> str | None:
    """If we never got a `type==result` line, try to recover the last
    assistant text. Useful when the stream truncates."""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "assistant":
            message = obj.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    parts = [
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    text = "\n".join(t for t in parts if t).strip()
                    if text:
                        return text
    return None
