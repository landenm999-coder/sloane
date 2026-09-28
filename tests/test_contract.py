"""The contract parser against the five shapes a model actually emits."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.contract import MAX_SPEECH_SENTENCES, Reply, clean_speech, parse, sentences

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def case(label: str, raw: str, speech: str, detail: str) -> None:
    reply = parse(raw)
    check(f"{label} / speech", reply.speech, speech)
    check(f"{label} / detail", reply.detail, detail)


# --- a model that stops one brace short -------------------------------------
# Measured on the CLI: about one JSON object in three came back without its
# closing brace. Unrepaired, the whole object was his reply, read aloud.
short = parse('{"speech": "Two things are due Friday.", "detail": "Stat and the essay.", '
              '"do": ["/remind 7pm start the essay"]')
check("an object missing its last brace is still the reply",
      (short.speech, short.detail, short.actions),
      ("Two things are due Friday.", "Stat and the essay.", ("/remind 7pm start the essay",)))
check("and one cut off mid-string keeps what arrived",
      parse('Sure: {"speech": "Hi.", "detail": "The lab is due at 11:59').detail, "The lab is due at 11:59")

# --- shape 1: bare JSON ------------------------------------------------------
case(
    "shape 1 bare json",
    '{"speech": "Two things are due Friday.", "detail": "Stat p. 214 and the CO History essay."}',
    "Two things are due Friday.",
    "Stat p. 214 and the CO History essay.",
)

# --- shape 2: fenced JSON ----------------------------------------------------
case(
    "shape 2 fenced json",
    '```json\n{"speech": "You work at three.", "detail": "3-7 PM, same as every weekday."}\n```',
    "You work at three.",
    "3-7 PM, same as every weekday.",
)

# --- shape 3: prose preamble, then JSON --------------------------------------
case(
    "shape 3 preamble then json",
    'Sure, here is the reply you asked for:\n\n{"speech": "Physics lab is late.", '
    '"detail": "It was due Tuesday and is still marked missing."}',
    "Physics lab is late.",
    "It was due Tuesday and is still marked missing.",
)

# --- shape 4: labelled plain text -------------------------------------------
case(
    "shape 4 labelled",
    "Speech: Nothing is due tomorrow.\nDetail: Jewelry I has a critique next Monday.",
    "Nothing is due tomorrow.",
    "Jewelry I has a critique next Monday.",
)
case(
    "shape 4 bold labels",
    "**Speech:** You have a shift.\n**Detail:** Three to seven, then the DECA deck.",
    "You have a shift.",
    "Three to seven, then the DECA deck.",
)

# --- shape 5: unstructured prose --------------------------------------------
case(
    "shape 5 prose",
    "You have two assignments due Friday. Both are in Stat Reasoning. "
    "The second one is the longer of the two.",
    "You have two assignments due Friday. Both are in Stat Reasoning.",
    "You have two assignments due Friday. Both are in Stat Reasoning. "
    "The second one is the longer of the two.",
)

# A plan in prose: the lead sentence is speech, the rest is detail, said once.
# (It used to flatten the whole list into "speech" and repeat it all as detail.)
plan = parse("The lab report first, it's due at 11:59.\n\n**Tonight, in order:**\n1. Lab report\n2. Problem set 4")
check("prose with a lead: the lead is speech", plan.speech, "The lab report first, it's due at 11:59.")
check("and the rest is the detail, without the lead again", plan.detail,
      "**Tonight, in order:**\n1. Lab report\n2. Problem set 4")
long_lead = parse("One. Two. Three.\n\nMore here.")
check("a lead too long to say whole keeps everything in detail",
      (long_lead.speech, long_lead.detail), ("One. Two.", "One. Two. Three.\n\nMore here."))

from sloane.contract import partial_reply  # noqa: E402

check("streamed prose is shown as it comes", partial_reply("The lab report fir"), ("The lab report fir", ""))
check("streamed JSON still shows its speech", partial_reply('{"speech": "The lab', ), ("The lab", ""))
check("nothing yet is nothing", partial_reply("  "), ("", ""))

# --- the speech invariant ----------------------------------------------------
check(
    "speech caps at two sentences",
    len(sentences(parse("One. Two. Three. Four.").speech)),
    MAX_SPEECH_SENTENCES,
)
check(
    "markdown stripped from speech",
    clean_speech("**Stat** is due _Friday_ and `p. 214` is the reading."),
    "Stat is due Friday and p. 214 is the reading.",
)
check(
    "urls never reach speech",
    clean_speech("Open https://canvas.instructure.com/x now."),
    "Open now.",
)
check(
    "markdown links keep only their label",
    clean_speech("Check [the Stat reading](https://example.com/a) tonight."),
    "Check the Stat reading tonight.",
)
check(
    "lists flatten into prose",
    clean_speech("- Stat p. 214\n- CO History essay"),
    "Stat p. 214 CO History essay",
)

# --- degradation must never raise -------------------------------------------
for hostile in ["", "   ", "{", "{}", "[]", "null", '{"unrelated": 1}', "```\n```"]:
    try:
        result = parse(hostile)
        if not isinstance(result, Reply):
            FAILURES.append(f"parse({hostile!r}) returned {type(result)}")
    except Exception as exc:  # noqa: BLE001 - the whole point is that it cannot
        FAILURES.append(f"parse({hostile!r}) raised {exc!r}")

# JSON that decodes but carries no speech/detail keys falls through to shape 5,
# so the raw text is still preserved rather than silently dropped.
check("unrelated json keeps its text", parse('{"unrelated": 1}').detail, '{"unrelated": 1}')

# --- speech never carries a URL, whatever the shape -------------------------
for raw in [
    '{"speech": "See https://x.test/a", "detail": "d"}',
    "Speech: go to www.example.com now\nDetail: d",
    "Just visit https://example.com/thing for it.",
]:
    if "http" in parse(raw).speech or "www." in parse(raw).speech:
        FAILURES.append(f"URL leaked into speech from {raw!r}")

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("contract: all shapes pass")
