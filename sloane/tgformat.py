"""Her detail is written in Markdown; Telegram shows HTML. This translates the
little that matters and nothing more.

`**bold**`, `` `code` ``, fenced code blocks, `[links](https://...)`, headings and
list bullets become Telegram HTML, and a table becomes one bullet per row. Everything else is escaped, and a marker
only turns into a tag when it has a partner on the same line, so the output is
always well-formed, even for a reply that is still streaming in with half a
`**` on the end. Telegram refusing it anyway is handled by the caller, which
sends the plain text instead.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
# Only **: underscores are too common in names and code (snake_case, __init__).
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_SLOT = "\x00{}\x00"
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RULE_CELL = re.compile(r"^\s*:?-+:?\s*$")


def _is_rule(line: str) -> bool:
    """A table's header rule: every cell only dashes, with optional colons."""
    cells = [c for c in line.strip().strip("|").split("|")]
    return "-" in line and bool(cells) and all(_RULE_CELL.match(c) for c in cells)


def _tables(text: str) -> str:
    """Markdown tables as bullet lines: a phone can't show columns, and pipes
    read as noise. The header row goes (the rows carry the meaning)."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _TABLE_ROW.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        block = []
        while i < len(lines) and (_TABLE_ROW.match(lines[i]) or _is_rule(lines[i])):
            block.append(lines[i])
            i += 1
        if len(block) > 1 and _is_rule(block[1]):
            block = block[2:]  # the header and its rule
        for row in block:
            if _is_rule(row):
                continue
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            cells = [c for c in cells if c]
            if not cells:
                continue
            first = cells[0] if "**" in cells[0] else f"**{cells[0]}**"
            out.append("- " + " · ".join([first, *cells[1:]]))
    return "\n".join(out)


def _link(label: str, url: str) -> str:
    """A link that can't hide where it goes: the real domain rides beside it.
    Email previews and web results pass through here too, and "Verify your
    account" pointing somewhere else is the oldest trick there is."""
    domain = urlsplit(url).hostname or url
    domain = domain.removeprefix("www.")
    return (f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>'
            f" ({html.escape(domain, quote=False)})")


def to_html(text: str) -> str:
    """Markdown-ish text as Telegram HTML (parse_mode=HTML)."""
    slots: list[str] = []

    def keep(fragment: str) -> str:
        slots.append(fragment)
        return _SLOT.format(len(slots) - 1)

    text = (text or "").replace("\x00", "")
    text = _FENCE.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip(), quote=False)}</pre>"), text)
    text = _tables(text)
    text = _CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)
    text = _LINK.sub(lambda m: keep(_link(m.group(1), m.group(2))), text)
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
