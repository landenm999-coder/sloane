"""What she may do in conversation: the commands he could type, run for him.

"Put oat milk on the list and remind me at 7 to take the trash out" should get
done, not just acknowledged. So a reply may carry commands ("do"), and the bot
runs them through the same handlers his own typed commands go through -- same
parsing, same rules, same hard lines (none of these come near one).

What keeps this safe:

* Only for his own messages. A brief, a job, the inbox, anything with INGESTED
  text in the prompt: no actions, whatever the model wrote.
* Only these commands, and none of their destructive forms (RULES, per
  command): nothing that drops, clears, cancels, forgets or undoes. Those he
  types himself.
* Only what he asked for, in that message or by saying yes to her offer:
  `grounded()` checks the command's words against his, in code.
* At most MAX_ACTIONS per reply, one line each, and every result is shown.
"""

from __future__ import annotations

import re

from sloane.contract import MAX_ACTIONS

# Built-in commands she may run for him (sloane/telegram.py answers them).
# Not /done: marking an assignment handed in hides a real deadline from FACTS,
# and that is his call to type.
BUILTIN: dict[str, str] = {
    "remind": "/remind <when> <what> -- e.g. /remind tomorrow 7am bring the lab",
    "promise": "/promise <what> [to <name>] [by <when>]",
}
# Skill commands, when that skill is loaded.
SKILLS: dict[str, str] = {
    "list": "/list add <list>: <item>, <item>  ·  /list done <list> <item or number>",
    "countdown": "/countdown <what> <date>",
    "card": "/card <deck>: <question> :: <answer>",
    "habit": "/habit add <name>",
    "did": "/did <habit> [yesterday]",
    "client": ("/client add <name> [$amount] [follow up <day>: <what>]  ·  "
               "/client <name> <stage | follow up <day>: <what> | note <text> | $<amount> | done>"),
    "estimate": "/estimate <assignment words> <duration, e.g. 90m>",
    "focus": "/focus <minutes> <what>  ·  /focus stop",
    "birthday": "/birthday <name> <date>",
    "spent": "/spent <amount> <what>",
    "budget": "/budget <amount>",
    "followup": "/followup <what he'll do> [<day>]  ·  /followup done <words from it> (when he says it's done)",
    "remember": "/remember <something he told you to keep in mind, in his words> -- kept at once",
    "idea": "/idea <something he wants you to be able to do> -- into the workshop, where you plan and build it",
    "college": ("/college add <school> <EA|ED|RD> <deadline>  ·  /college <school> done <checklist item>  ·  "
                "/college <school> submitted | admitted | deferred | waitlisted | denied | committed"),
    "roleplay": "/roleplay [area] -- starts a DECA practice role-play (marketing, finance, hospitality ...)",
    "workout": "/workout <what> [distance] [minutes] [yesterday] -- e.g. /workout run 3 mi 28 min  ·  /workout goal <n a week>",
    "watch": "/watch <ticker or name> -- onto his markets watchlist, e.g. /watch NVDA",
    "research": "/research <question> -- when he asks you to look into something properly: minutes of "
                "searching and reading, then a report with sources sent to him",
    "monitor": "/monitor <what to watch for, in his words> -- she tells him once when it happens, e.g. "
               "/monitor NVDA above 150  ·  /monitor https://store.example/item for in stock  ·  "
               "/monitor the iPhone 18 release date is announced",
}
MAX_LENGTH = 300

_NAME = re.compile(r"^/([a-z]+)(?:@\w+)?(?:\s|$)", re.I)
_AMOUNT = re.compile(r"^\$?\d{1,6}(?:\.\d{1,2})?$")


def _first_not(*verbs: str):  # noqa: ANN202
    return lambda a: bool(a) and a[0] not in verbs


def _last_not(*verbs: str):  # noqa: ANN202
    return lambda a: bool(a) and a[-1] not in verbs


def _budget(a: list[str]) -> bool:
    return len(a) == 1 and bool(_AMOUNT.match(a[0])) and float(a[0].lstrip("$")) > 0


# Which forms of each command she may run: the adding, setting and marking
# ones. Every skill names its destructive verbs differently ("/countdown cancel",
# "/habit stop", "/budget none"), so each gets its own rule rather than a
# shared list of scary words. `a` is the words after the command, lowercased.
RULES: dict[str, object] = {
    "remind": bool,
    "promise": bool,
    "list": lambda a: not a or a[0] != "clear",
    "countdown": _first_not("drop", "remove", "done", "cancel", "delete"),
    "card": bool,
    "habit": lambda a: bool(a) and a[0] in {"add", "new", "track", "did", "done"},
    "did": bool,
    "client": lambda a: bool(a) and (a[0] in {"add", "new"} or a[-1] not in {"drop", "archive", "remove"}),
    "estimate": bool,
    "focus": lambda a: True,
    "birthday": _last_not("forget", "remove", "clear", "delete"),
    "spent": lambda a: bool(a) and bool(_AMOUNT.match(a[0])),
    "budget": _budget,
    "followup": bool,
    "remember": lambda a: len(a) >= 2,
    "idea": lambda a: len(a) >= 2,
    # Ticking, adding and recording where an application stands. Not dropping a
    # school, skipping a checklist item or reopening one: those he types.
    "college": lambda a: bool(a) and not set(a) & {"drop", "archive", "remove", "delete", "skip", "skipped",
                                                  "optional", "undo", "untick", "unskip", "open", "reopen"},
    "roleplay": lambda a: len(a) <= 4,
    # Logging one or setting the goal; never taking one back.
    "workout": _first_not("undo", "delete", "remove", "oops"),
    "watch": bool,
    # Starting a run; reading one back (a number) is his to type.
    "research": lambda a: bool(a) and not (len(a) == 1 and a[0].isdigit()),
    # Setting one up; never stopping one.
    "monitor": lambda a: bool(a) and a[0] not in {"stop", "end", "cancel", "delete", "remove", "off", "list"},
}


def available(skill_commands: frozenset[str] | set[str]) -> dict[str, str]:
    """The commands she may run right now, with how to write them."""
    out = dict(BUILTIN)
    out.update({name: usage for name, usage in SKILLS.items() if name in skill_commands})
    return out


def check(command: str, allowed: dict[str, str]) -> str | None:
    """The command, tidied, if she may run it; None if not."""
    text = (command or "").strip()
    if not text or "\n" in text or len(text) > MAX_LENGTH:
        return None
    if not text.startswith("/"):
        text = "/" + text
    found = _NAME.match(text)
    name = found.group(1).lower() if found else ""
    if name not in allowed or name not in RULES:
        return None
    args = text[found.end():].lower().replace(":", " ").split()
    return text if RULES[name](args) else None


# -- only what he asked for ---------------------------------------------------------------

_YES = re.compile(r"^\s*(?:y|yes|yeah|yep|yup|ya|sure|ok|okay|k|please|pls|do it|go ahead|sounds good|"
                  r"yes please|yeah do it|do that|perfect|bet)\b[\s!.,]*(?:do it|please|thanks|thank you)?[\s!.]*$", re.I)
_STOP = {"the", "a", "an", "to", "for", "on", "at", "in", "of", "my", "and", "me", "it", "is", "add", "done",
         "new", "track", "pm", "am", "tonight", "today", "tomorrow", "list", "up", "follow", "with", "by"}


_CONTROL = {"stop", "end", "star", "paus"}  # stems of control words, not content


def _stems(text: str) -> set[str]:
    """Words, cut to their first four letters, so 'focusing' meets 'focus'."""
    return {w[:4] for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP}


# Words that change what a command records, and the stems he'd have used to say
# so. Naming Boulder isn't saying the Boulder application went in, and marking it
# submitted silences its deadline: "/college boulder submitted" needs "sent",
# "submitted" or the like in what he said.
_SENT = {"subm", "sent", "send", "appl", "turn"}
_MEANS: dict[str, dict[str, set[str]]] = {
    "college": {
        "submitted": _SENT, "sent": _SENT, "applied": _SENT,
        "admitted": {"admi", "acce", "got", "into"}, "accepted": {"admi", "acce", "got", "into"},
        "got": {"got"},
        "deferred": {"defe"},
        "waitlisted": {"wait"}, "waitlist": {"wait"},
        "denied": {"deni", "deny", "reje", "didn"}, "rejected": {"deni", "deny", "reje", "didn"},
        "committed": {"comm", "enro", "depo", "goin", "chos", "chose", "pick"},
    },
}


def grounded(command: str, message: str, offer: str = "") -> bool:
    """Did he ask for this? Its words must come from his message -- or, when
    his message is a plain yes, from the offer of hers he is answering.

    Heuristic, and deliberately one-sided: a real request can fail it (then
    she says what she would have run), but a command drawn from nowhere can't
    pass it. It is what makes "only what he asked for" code, not a prompt.
    """
    command = (command or "").strip()
    found = _NAME.match(command)
    if not found:
        return False
    name = found.group(1).lower()
    rest = command[found.end():]
    # What was asked for is the payload: for "/list add grocery: milk" that is
    # the milk, not the grocery list he may merely have mentioned.
    if name in {"list", "card"} and ":" in rest:
        rest = rest.split(":", 1)[1]
    args = _stems(rest) - _CONTROL
    if not args:  # "/focus stop": the command itself carries the meaning
        args = _stems(name)
    said = _stems(message)
    if args & said:
        heard = said
    elif _YES.match(message or "") and args & _stems(offer):
        heard = said | _stems(offer)
    else:
        return False
    for word in re.findall(r"[a-z]+", rest.lower()):
        need = _MEANS.get(name, {}).get(word)
        if need is not None and not need & heard:
            return False
    return True


LOOKUP = """\
You can look things up. When answering needs current information you can't \
have -- news, prices, scores, weather elsewhere, opening hours, anything after \
your training -- add a key "look" with a short web search query, and make \
speech a few words saying you're checking ("Checking."). You'll get the results \
and answer again. Don't look up what FACTS, CONVERSATION or your own knowledge \
already answers, and never look up anything about Landen himself."""


def instructions(allowed: dict[str, str]) -> str:
    """The part of her system prompt that says she can act, and how."""
    usage = "\n".join(f"- {line}" for line in allowed.values())
    # With the workshop, "I can't do that" isn't the end of it: she can learn it.
    cannot = (
        "- If nothing here does what he wants, say so and never claim you did it. If it's something \
you could do with a new ability of your own (tracking something, a new feed, a new kind of reminder, \
a panel), offer to build it: \"I can't do that yet. Want me to build it? It'll be in the Workshop for \
you to OK.\" If he's already asking for the ability (\"can you start tracking my water?\", \"build \
yourself a way to...\"), put it in with /idea straight away. Never offer to build what you mustn't do \
(buying, moving money, posting as him, anything on the lines above)."
        if "idea" in allowed else
        "- If nothing here does what he wants, say so; never claim you did it."
    )
    return f"""\
You can act, not only answer. When Landen asks you to do something one of these \
commands does -- in his message, or by saying yes to something you offered in \
CONVERSATION -- or tells you something one of them records ("I spent 14 on \
lunch", "I sent my Boulder app"), add a third key to your JSON, "do": a list of \
the commands, exactly as he would type them. At most {MAX_ACTIONS}.

{usage}

Rules:
- Only what he asked for. Never on your own initiative: if you think a reminder \
would help, offer it ("Want a reminder at 6?") and wait for his yes.
- When he tells you he did something a command records ("finished my Boulder \
essays", "spent 14 on lunch"), record it -- that is him telling you, not your \
initiative, so don't ask first. Say you've marked it.
- Nothing destructive: you can't drop, clear, forget or undo anything. If he \
asks for that, tell him the command to type.
- Say what you're doing in speech, briefly and in the present tense ("On it: \
oat milk's going on the list."). The results of each command are shown to him \
under your reply, so don't invent details the command will report.
- Only the "do" list does anything. Never say you've marked, added or set \
something unless its command is in "do".
{cannot}"""
