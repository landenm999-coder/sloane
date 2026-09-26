"""A stand-in for `claude -p`, one-shot or streamed, for the provider tests.

Not a test itself (no test_ prefix). `FakeCli().exec` replaces
asyncio.create_subprocess_exec: every call is recorded, and each process
answers the way the real CLI does -- a JSON envelope for one-shot calls, or
stream-json events (text deltas, then a result) for streamed ones.
"""

from __future__ import annotations

import asyncio
import json


class _Stdin:
    def __init__(self, proc: "FakeProc") -> None:
        self.proc = proc

    def write(self, data: bytes) -> None:
        if self.proc.cli.die_idle and self.proc.warm_age_ticks:
            raise BrokenPipeError("the process died while idle")
        if self.proc.rejected and self.proc.cli.broken_pipe:
            raise BrokenPipeError("it exited before reading its input")
        self.proc.received += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.proc.stdin_closed = True


class _Lines:
    def __init__(self, lines: list[bytes], delay: float = 0.0) -> None:
        self.lines = list(lines)
        self.delay = delay

    async def readline(self) -> bytes:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.lines.pop(0) if self.lines else b""

    async def read(self) -> bytes:
        out, self.lines = b"".join(self.lines), []
        return out


class FakeProc:
    def __init__(self, cli: "FakeCli", argv: tuple) -> None:
        self.cli = cli
        self.argv = argv
        self.received = b""
        self.stdin_closed = False
        self.killed = False
        self.warm_age_ticks = 0
        rejected = next((a for a in argv if a in cli.reject), None)
        self.rejected = rejected
        self.streaming = "--input-format" in argv
        self.stdin = _Stdin(self)
        if rejected:
            self.returncode = 1
            self.stdout = _Lines([])
            self.stderr = _Lines([f"error: unknown option '{rejected}'".encode()])
            self._oneshot = (b"", f"error: unknown option '{rejected}'".encode())
            return
        self.returncode = None
        self.stderr = _Lines([])
        text = cli.reply
        chunks = [text[i:i + cli.chunk] for i in range(0, len(text), cli.chunk)] or [""]
        events = [{"type": "system", "subtype": "init"}]
        events += [{"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "text_delta", "text": c}}}
                   for c in chunks]
        events.append({"type": "result", "subtype": "error_during_execution" if cli.error else "success",
                       "is_error": cli.error, "result": "rate limited" if cli.error else text,
                       "usage": {"input_tokens": 11, "output_tokens": 7},
                       "modelUsage": {"claude-sonnet-5": {}}})
        self.stdout = _Lines([(json.dumps(e) + "\n").encode() for e in events], delay=cli.delay)
        self._oneshot = (json.dumps({"result": text, "model": "m"}).encode(), b"")

    async def communicate(self, data: bytes) -> tuple[bytes, bytes]:
        self.received = data
        if self.returncode is None:
            self.returncode = 0
        return self._oneshot

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeCli:
    def __init__(self, reply: str = "hello", *, reject: set[str] | None = None, chunk: int = 4,
                 error: bool = False, die_idle: bool = False, delay: float = 0.0,
                 broken_pipe: bool = False) -> None:
        self.broken_pipe = broken_pipe
        self.reply = reply
        self.reject = reject or set()
        self.chunk = chunk
        self.error = error
        self.die_idle = die_idle
        self.delay = delay
        self.calls: list[tuple[list[str], dict]] = []
        self.procs: list[FakeProc] = []

    async def exec(self, *argv, **kw) -> FakeProc:
        self.calls.append((list(argv), kw))
        proc = FakeProc(self, argv)
        self.procs.append(proc)
        return proc
