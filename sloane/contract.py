"""Every reply is a Reply(speech, detail).

`speech` is what gets read aloud: at most two sentences, no markdown, no lists,
no URLs. `detail` is what gets shown. Holding this line in P0 is what makes P3
a transport swap instead of a rewrite, so nothing may bypass parse().

Models do not reliably emit one shape. parse() handles five, in order of how
much we trust them, and never raises: the worst case degrades to treating the
whole output as detail and deriving speech from it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_SPEECH_SENTENCES = 2
# Commands one reply may carry. More than this is not a request, it's a script.
MAX_ACTIONS = 5
MAX_LOOKUPS = 3  # web searches one reply may ask for, run together

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.+?)```", re.DOTALL)
_FIRST_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
# A bold label may close its asterisks on either side of the colon --
# "**Detail:**" and "**Detail**:" are both common -- so both are optional.
_LABEL = r"(?:\*\*)?%s(?:\*\*)?\s*[:\-]\s*(?:\*\*)?"
_LABELLED = re.compile(
    r"^\s*" + (_LABEL % r"(?:speech|say|aloud)") + r"(?P<speech>.*?)"
    r"(?:\n\s*" + (_LABEL % r"(?:detail|details|body|more)") + r"(?P<detail>.*))?$",
    re.IGNORECASE | re.DOTALL,
)

# Tool-call markup her model sometimes writes although `claude -p` gives it no
# tools: "<invoke name="noop"></invoke>" before the JSON, "<answer></answer>"
# mid-reply, even "<invoire>". Never part of what she says. Only these names,
# so "<3" or "a < b" in a real reply is left alone.
_STRAY = re.compile(r"</?(?:invo\w*|answer|function_calls?|parameters?|tool_\w+|antml:\w+)\b[^<>]*>", re.I)
# A stray tag still being written at the end of a stream ("<invo").
_STRAY_OPEN = re.compile(r"</?(?:i(?:n(?:v(?:o\w*)?)?)?|a(?:n(?:s(?:w(?:er?)?)?)?)?)?$", re.I)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_MD_MARKS = re.compile(r"(\*\*|__|\*|_|`+|~~|^\s{0,3}#{1,6}\s*)", re.MULTILINE)
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Reply:
    """The only shape a reply may take.

    `actions` are commands she proposes to run because he asked for them
    ("/remind 7pm take the trash out"). The contract only carries them; the bot
    decides whether they may run (sloane/actions.py), and only for his own
    messages.
    """

    speech: str
    detail: str
    actions: tuple[str, ...] = ()
    # A web search she needs before she can answer ("look"). The agent runs it
    # and asks again with the results as INGESTED; never shown to him as-is.
    lookup: str = ""
    # All of them, when one question needs several facts ("look" as a list, at
    # most MAX_LOOKUPS): run together, answered from at once. `lookup` is the first.
    lookups: tuple[str, ...] = ()
    # Built from outside text (email, web results): remembered as untrusted,
    # and never shown back to her as plain CONVERSATION.
    tainted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.speech, str) or not isinstance(self.detail, str):
            raise TypeError("speech and detail must both be strings")


def unstray(text: str) -> str:
    """The model's output without stray tool-call tags; the blank lines they leave, closed up."""
    if "<" not in text:
        return text
    cleaned = _STRAY.sub("", text)
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+", "\n\n", cleaned) if cleaned != text else text


def sentences(text: str) -> list[str]:
    """Split on sentence terminators. Pragmatic, not linguistic."""
    parts = [p.strip() for p in _SENTENCE_END.split(text.strip()) if p.strip()]
    return parts


def clean_speech(text: str, max_sentences: int = MAX_SPEECH_SENTENCES) -> str:
    """Make text safe to read aloud.

    Links become their label, URLs go, markdown marks go, list structure
    flattens into prose, and the result is capped at two sentences.
    """
    out = _MD_LINK.sub(r"\1", text or "")
    out = _URL.sub("", out)
    out = _LIST_MARK.sub("", out)
    out = _MD_MARKS.sub("", out)
    out = out.replace("|", " ")
    out = _WS.sub(" ", out).strip()
    picked = sentences(out)[:max_sentences]
    return " ".join(picked).strip() if picked else out


def _from_mapping(data: dict) -> Reply | None:
    """Pull speech/detail out of a decoded object, whatever it calls them."""
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    speech = lowered.get("speech") or lowered.get("say") or lowered.get("aloud")
    detail = (
        lowered.get("detail")
        or lowered.get("details")
        or lowered.get("body")
        or lowered.get("more")
    )
    if speech is None and detail is None:
        return None
    speech_text = "" if speech is None else str(speech)
    detail_text = "" if detail is None else str(detail)
    if not speech_text:
        speech_text = detail_text
    if not detail_text:
        detail_text = speech_text
    raw_actions = lowered.get("do") or lowered.get("actions") or ()
    if isinstance(raw_actions, str):
        raw_actions = [raw_actions]
    actions = tuple(
        a.strip() for a in raw_actions if isinstance(a, str) and a.strip()
    )[:MAX_ACTIONS] if isinstance(raw_actions, (list, tuple)) else ()
    look = lowered.get("look") or lowered.get("lookup") or ""
    looks = [look] if isinstance(look, str) else list(look) if isinstance(look, (list, tuple)) else []
    lookups = tuple(dict.fromkeys(" ".join(q.split())[:200] for q in looks if isinstance(q, str) and q.strip()))
    lookups = lookups[:MAX_LOOKUPS]
    return Reply(speech=clean_speech(speech_text), detail=detail_text.strip(), actions=actions,
                 lookup=lookups[0] if lookups else "", lookups=lookups)


def _try_json(blob: str) -> Reply | None:
    try:
        return _from_mapping(json.loads(blob))
    except (ValueError, TypeError):
        return None


def _each_object(text: str) -> Reply | None:
    """The first JSON object in `text` that is a reply, read where it starts and no further.

    For a reply followed by more text with a brace in it (a second object, the
    "{}" a stray tag leaves): the greedy first-to-last-brace match spans all of
    it and reads as nothing, and the command she wrote was lost.
    """
    decoder = json.JSONDecoder()
    at = text.find("{")
    while at >= 0:
        try:
            data, end = decoder.raw_decode(text, at)
        except ValueError:
            at = text.find("{", at + 1)
            continue
        found = _from_mapping(data) if isinstance(data, dict) else None
        if found is not None:
            return found
        at = text.find("{", end)
    return None


def closed(text: str) -> str | None:
    """`text` with the string and brackets it left open closed; None if nothing
    was left open, or if what's there is malformed rather than merely short.

    A model sometimes stops one brace short of its JSON object -- measured on
    the CLI, about one scenario in three -- and without this the whole object
    would be shown (and read aloud) as raw text.
    """
    stack: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    if not stack:
        return None
    body = text[:-1] if in_string and escaped else text
    return body + ('"' if in_string else "") + "".join(reversed(stack))


def loads_lenient(text: str, opener: str = "{") -> object | None:
    """json.loads of the first JSON object (or, with opener "[", array) in
    `text`, closed if it stops short. Every parser of a model's JSON uses this."""
    start = (text or "").find(opener)
    if start < 0:
        return None
    blob = text[start:]
    end = blob.rfind("}" if opener == "{" else "]")
    for candidate in (blob[: end + 1] if end >= 0 else None, closed(blob)):
        if candidate is None:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _partial_string(raw: str, key: str) -> str:
    """The value of "key" in a JSON object that may still be arriving.

    Decodes as far as the text goes and stops cleanly at a half-received
    escape, so a streamed reply can be shown while it is being written.
    """
    found = re.search(r'"%s"\s*:\s*"' % re.escape(key), raw)
    if not found:
        return ""
    out: list[str] = []
    i = found.end()
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            break
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(raw):
            break  # an escape cut in half
        code = raw[i + 1]
        if code == "u":
            digits = raw[i + 2:i + 6]
            if len(digits) < 4:
                break
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                pass
            i += 6
            continue
        out.append(_ESCAPES.get(code, code))
        i += 2
    return "".join(out)


def partial_reply(raw: str) -> tuple[str, str]:
    """(speech, detail) so far, from a model reply that is still streaming in.

    Only for showing progress: the finished reply still goes through parse(),
    which is what the contract promises. Prose (she sometimes skips the JSON)
    is shown as it comes, as speech.
    """
    text = _STRAY_OPEN.sub("", unstray(raw or "")).lstrip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if not text:
        return "", ""
    # Prose, then the JSON after all ("Here is the reply as intended: {..."): once
    # the JSON starts, it's the reply.
    start = text.find('{"')
    if start > 0:
        text = text[start:]
    if not text.startswith("{"):
        # She answered in prose, not the JSON shape: show it as it comes (parse()
        # still decides what the finished reply is).
        return text, ""
    return _partial_string(text, "speech"), _partial_string(text, "detail")


def parse(raw: str) -> Reply:
    """Coerce any model output into a Reply. Never raises.

    Shape 1  bare JSON object
    Shape 2  a JSON object inside a ``` fence
    Shape 3  prose preamble, then a JSON object
    Shape 4  labelled plain text -- "Speech: ... Detail: ..."
    Shape 5  unstructured prose -- detail is all of it, speech is derived
    """
    text = unstray(raw or "").strip()
    if not text:
        return Reply(speech="", detail="")

    # 1 -- the whole output is JSON.
    found = _try_json(text)
    if found is not None:
        return found

    # 2 -- JSON inside a fence. Try each fence; a model may emit prose fences too.
    for block in _FENCE.findall(text):
        found = _try_json(block.strip())
        if found is not None:
            return found

    # 3 -- a JSON object embedded in prose. Greedy, so nested braces survive.
    match = _FIRST_OBJECT.search(text)
    if match is not None:
        found = _try_json(match.group(0)) or _each_object(text)
        if found is not None:
            return found

    # 3b -- an object that stops a brace (or a string) short of its end.
    data = loads_lenient(text)
    if isinstance(data, dict):
        found = _from_mapping(data)
        if found is not None:
            return found

    # 4 -- labelled plain text.
    labelled = _LABELLED.match(text)
    if labelled is not None:
        speech = (labelled.group("speech") or "").strip()
        detail = (labelled.group("detail") or "").strip()
        if speech:
            return Reply(
                speech=clean_speech(speech),
                detail=detail or speech,
            )

    # 5 -- no structure at all. Speech is the readable head; detail is the rest.
    # A plan or a list in prose leads with a sentence and then a blank line:
    # that sentence is what he hears, and the rest is shown under it once, not
    # the whole text flattened into speech and then repeated.
    head, _, rest = text.partition("\n\n")
    speech = clean_speech(head)
    if rest.strip() and speech == clean_speech(head, max_sentences=99):
        return Reply(speech=speech, detail=rest.strip())
    return Reply(speech=clean_speech(text) if not rest.strip() else speech, detail=text)
