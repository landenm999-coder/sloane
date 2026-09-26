"""Her detail is written in Markdown; Telegram shows HTML. This translates the
little that matters and nothing more.

`**bold**`, `` `code` ``, fenced code blocks, `[links](https://...)`, headings and
list bullets become Telegram HTML. Everything else is escaped, and a marker
only turns into a tag when it has a partner on the same line, so the output is
always well-formed, even for a reply that is still streaming in with half a
`**` on the end. Telegram refusing it anyway is handled by the caller, which
sends the plain text instead.
"""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
# Only **: underscores are too common in names and code (snake_case, __init__).
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_SLOT = "\x00{}\x00"


def to_html(text: str) -> str:
    """Markdown-ish text as Telegram HTML (parse_mode=HTML)."""
    slots: list[str] = []

    def keep(fragment: str) -> str:
        slots.append(fragment)
        return _SLOT.format(len(slots) - 1)

    text = (text or "").replace("\x00", "")
    text = _FENCE.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip(), quote=False)}</pre>"), text)
    text = _CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = _LINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">'
                                    f"{html.escape(m.group(1), quote=False)}</a>"), text)
    lines = []
    for line in html.escape(text, quote=False).split("\n"):
        heading = _HEADING.match(line)
        if heading:
            line = f"<b>{heading.group(1)}</b>"
        else:
            line = _BULLET.sub(lambda m: f"{m.group(1)}• ", line)
            line = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", line)
        lines.append(line)
    out = "\n".join(lines)
    return re.sub("\x00(\\d+)\x00", lambda m: slots[int(m.group(1))], out)


def formatted(text: str) -> bool:
    """Would to_html add anything? If not, the plain text goes as it is."""
    return to_html(text) != html.escape(text or "", quote=False)
