"""Her Markdown shown as Telegram HTML: what converts, what is escaped, and the
plain-text fallback when Telegram won't parse it. No network: a fake client."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.tgformat import formatted, to_html

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


TABLE = [
    ("bold", "**CU Boulder** — Early Action", "<b>CU Boulder</b> — Early Action"),
    ("code", "Try `/college boulder done essays`", "Try <code>/college boulder done essays</code>"),
    ("everything else is escaped", "1 < 2 & **x <b>** y", "1 &lt; 2 &amp; <b>x &lt;b&gt;</b> y"),
    ("an unpaired marker stays as it is (a reply still streaming in)", "half **open", "half **open"),
    ("bullets and headings", "- one\n* two\n## Plan", "• one\n• two\n<b>Plan</b>"),
    ("links, with the URL escaped for the attribute", "[ESPN](https://www.espn.com/a?b=1&c=2)",
     '<a href="https://www.espn.com/a?b=1&amp;c=2">ESPN</a> (espn.com)'),
    ("a link can't hide where it goes", "[Verify your Canvas account](http://canvas-login.example.ru/x)",
     '<a href="http://canvas-login.example.ru/x">Verify your Canvas account</a> (canvas-login.example.ru)'),
    ("a code block", "```\nx < y\n```", "<pre>x &lt; y</pre>"),
    ("underscores are left alone", "snake_case and __init__", "snake_case and __init__"),
    ("a bold marker never reaches inside code", "`**not bold**` and **bold**",
     "<code>**not bold**</code> and <b>bold</b>"),
    ("only http(s) links", "[x](javascript:alert(1))", "[x](javascript:alert(1))"),
    ("a stray NUL can't forge a slot", "a\x000\x00b **c**", "a0b <b>c</b>"),
    ("a table is one bullet per row, header dropped",
     "| Day | Due |\n|---|---|\n| Fri | Lab 4 |\n| Mon | Essay |",
     "• <b>Fri</b> · Lab 4\n• <b>Mon</b> · Essay"),
    ("text around a table is kept", "This week:\n| a | b |\n|:--|--:|\n| x | y |\nThat's all.",
     "This week:\n• <b>x</b> · y\nThat's all."),
    ("a lone pipe isn't a table", "either | or", "either | or"),
]
for label, raw, want in TABLE:
    check(label, to_html(raw), want)
check("plain text needs no formatting", [formatted("Two things are due."), formatted("1 < 2 & 3")], [False, False])
check("formatting does", [formatted("**x**"), formatted("- a")], [True, True])


# -- the bot: HTML when it helps, plain text when Telegram refuses it --------------------
class Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


class FakeClient:
    def __init__(self, refuse_html: bool = False, fail: bool = False) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.refuse_html = refuse_html
        self.fail = fail

    async def post(self, url: str, json: dict) -> Response:
        self.posts.append((url.rsplit("/", 1)[-1], json))
        if self.fail:
            return Response(500, {"ok": False, "description": "Internal Server Error"})
        if self.refuse_html and json.get("parse_mode") == "HTML":
            return Response(400, {"ok": False, "description": "Bad Request: can't parse entities: unclosed tag"})
        return Response(200, {"ok": True, "result": {"message_id": 7}})


async def bot_half() -> None:
    from sloane.telegram import Bot

    bot = Bot(None, None, isolated(telegram_bot_token="x", telegram_chat_id=1))
    client = FakeClient()
    await bot._call(client, "sendMessage", chat_id=1, text="**Essays** done; left: `fee`")
    check("formatted text goes as HTML", client.posts[-1][1],
          {"chat_id": 1, "text": "<b>Essays</b> done; left: <code>fee</code>", "parse_mode": "HTML"})
    await bot._call(client, "sendMessage", chat_id=1, text="Two things are due Friday.")
    check("plain text goes as it is", client.posts[-1][1], {"chat_id": 1, "text": "Two things are due Friday."})
    await bot._call(client, "editMessageText", chat_id=1, message_id=7, text="**Nov 1** ▍")
    check("a live edit is formatted too", client.posts[-1][1].get("parse_mode"), "HTML")
    await bot._call(client, "sendChatAction", chat_id=1, action="typing")
    check("other calls are untouched", client.posts[-1][1], {"chat_id": 1, "action": "typing"})

    refusing = FakeClient(refuse_html=True)
    got = await bot._call(refusing, "sendMessage", chat_id=1, text="**x**")
    check("refused HTML: sent again as the plain text", ([p[1].get("parse_mode") for p in refusing.posts],
                                                         refusing.posts[-1][1]["text"], got),
          (["HTML", None], "**x**", {"message_id": 7}))
    try:
        await bot._call(FakeClient(fail=True), "sendMessage", chat_id=1, text="**x**")
        FAILURES.append("a real failure must still raise")
    except RuntimeError as exc:
        check("a real failure still raises (callers retry or fall back)", "500" in str(exc), True)


asyncio.run(bot_half())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("format: Markdown as Telegram HTML, escaped, well-formed, plain text when refused")
