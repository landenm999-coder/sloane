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

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_MD_MARKS = re.compile(r"(\*\*|__|\*|_|`+|~~|^\s{0,3}#{1,6}\s*)", re.MULTILINE)
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Reply:
    """The only shape a reply may take."""

    speech: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.speech, str) or not isinstance(self.detail, str):
            raise TypeError("speech and detail must both be strings")


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
    return Reply(speech=clean_speech(speech_text), detail=detail_text.strip())


def _try_json(blob: str) -> Reply | None:
    try:
        return _from_mapping(json.loads(blob))
    except (ValueError, TypeError):
        return None


def parse(raw: str) -> Reply:
    """Coerce any model output into a Reply. Never raises.

    Shape 1  bare JSON object
    Shape 2  a JSON object inside a ``` fence
    Shape 3  prose preamble, then a JSON object
    Shape 4  labelled plain text -- "Speech: ... Detail: ..."
    Shape 5  unstructured prose -- detail is all of it, speech is derived
    """
    text = (raw or "").strip()
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
        found = _try_json(match.group(0))
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

    # 5 -- no structure at all. Everything is detail; speech is the readable head.
    return Reply(speech=clean_speech(text), detail=text)
