"""A local model (Ollama, llama.cpp, LM Studio) over the OpenAI API: answers, streams, fails plainly.

Against a stub server on loopback, like the other network tests: the sandbox
has no Ollama, and the contract is the OpenAI wire format, not a product.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.providers.base import ProviderError
from sloane.providers.local import LocalProvider

FAILURES: list[str] = []
SEEN: list[dict] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class Stub(BaseHTTPRequestHandler):
    """/v1/chat/completions (whole or streamed), /v1/models, /v1/audio/transcriptions."""

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models":
            self._json(200, {"data": [{"id": "llama3.2:3b"}, {"id": "qwen2.5:7b"}]})
        else:
            self._json(404, {"error": "no"})

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v1/audio/transcriptions":
            SEEN.append({"stt": b"whisper-small" in raw, "auth": self.headers.get("Authorization")})
            self._json(200, {"text": " remind me at five "})
            return
        body = json.loads(raw)
        SEEN.append({**body, "auth": self.headers.get("Authorization")})
        if body["model"] == "broken":
            self._json(500, {"error": "model failed to load"})
            return
        if not body.get("stream"):
            self._json(200, {"model": body["model"], "choices": [{"message": {"content": '{"speech": "Hi."}'}}],
                             "usage": {"prompt_tokens": 12, "completion_tokens": 4}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in ('{"speech": ', '"Hello', ' there."}'):
            event = {"model": body["model"], "choices": [{"delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        local = LocalProvider(isolated(local_base_url=base + "/", local_model="llama3.2:3b"))
        check("set up", local.configured, True)
        whole = await local.complete("be brief", "hi", max_tokens=50)
        check("a whole answer", whole.text, '{"speech": "Hi."}')
        check("accounted as local, with the model it named", (whole.usage.provider, whole.usage.model,
                                                             whole.usage.completion_tokens),
              ("local", "llama3.2:3b", 4))
        check("sent as system + user, no key when none is set",
              ([m["role"] for m in SEEN[-1]["messages"]], SEEN[-1]["auth"]), (["system", "user"], None))

        seen: list[str] = []

        async def on_text(text: str) -> None:
            seen.append(text)

        streamed = await local.stream("be brief", "hi", on_text=on_text)
        check("streamed, the words arrive as written", seen,
              ['{"speech": ', '{"speech": "Hello', '{"speech": "Hello there."}'])
        check("and the whole answer is returned", streamed.text, '{"speech": "Hello there."}')
        check("asked to stream", SEEN[-1]["stream"], True)

        check("what it serves", await local.models(), ["llama3.2:3b", "qwen2.5:7b"])

        broken = LocalProvider(isolated(local_base_url=base, local_model="broken"))
        for label, call in (("whole", broken.complete("s", "p")), ("streamed", broken.stream("s", "p", on_text=on_text))):
            try:
                await call
                FAILURES.append(f"a server error ({label}) must raise")
            except ProviderError as exc:
                check(f"a server error ({label}) says so", "500" in exc.message and "load" in exc.message, True)

        gone = LocalProvider(isolated(local_base_url="http://127.0.0.1:9/v1", local_model="x", local_timeout=3))
        try:
            await gone.complete("s", "p")
            FAILURES.append("an unreachable box must raise")
        except ProviderError as exc:
            check("an unreachable box is a plain failure", exc.message.startswith("can't reach the local model"), True)

        unset = LocalProvider(isolated())
        check("unset is not configured", unset.configured, False)
        try:
            await unset.complete("s", "p")
            FAILURES.append("unset must raise if asked anyway")
        except ProviderError as exc:
            check("and says what to set", "LOCAL_BASE_URL" in exc.message, True)

        voice = LocalProvider(isolated(local_base_url="http://127.0.0.1:9/v1", local_model="x",
                                       local_stt_model="whisper-small", local_stt_base_url=base, local_api_key="k"))
        check("a local Whisper transcribes", await voice.transcribe(b"OggS..."), "remind me at five")
        check("at its own address, with its model and the key when one is set",
              (SEEN[-1]["stt"], SEEN[-1]["auth"]), (True, "Bearer k"))
    finally:
        server.shutdown()


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("local: answers, streams, lists its models, transcribes, and fails plainly")
