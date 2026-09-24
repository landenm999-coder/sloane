"""What she may do in conversation: the commands he could type, run for him.

"Put oat milk on the list and remind me at 7 to take the trash out" should get
done, not just acknowledged. So a reply may carry commands ("do"), and the bot
runs them through the same handlers his own typed commands go through -- same
parsing, same rules, same hard lines (none of these come near one).

What keeps this safe:

* Only for his own messages. A brief, a job, the inbox, anything with INGESTED
  text in the prompt: no actions, whatever the model wrote.
* Only these commands, and none of their destructive forms: nothing that drops,
  clears, forgets or undoes. Those he types himself.
* Only what he asked for, in that message or by saying yes to her offer. The
  persona says so; the allowlist is what holds if the model forgets.
* At most MAX_ACTIONS per reply, one line each, and every result is shown.
"""

from __future__ import annotations

import re

from sloane.contract import MAX_ACTIONS

# Built-in commands she may run for him (sloane/telegram.py answers them).
BUILTIN: dict[str, str] = {
    "remind": "/remind <when> <what> -- e.g. /remind tomorrow 7am bring the lab",
    "promise": "/promise <what> [to <name>] [by <when>]",
    "done": "/done <words from the assignment title> -- he handed it in on paper",
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
}
# Words that make a command destructive. He types those himself.
DENY = frozenset({"drop", "remove", "clear", "forget", "undo", "delete", "archive", "reset",
                  "empty", "wipe", "off", "oops", "unremind"})
MAX_LENGTH = 300

_NAME = re.compile(r"^/([a-z]+)(?:@\w+)?(?:\s|$)", re.I)


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
    if found is None or found.group(1).lower() not in allowed:
        return None
    # Where a skill takes its verb: first ("/list clear grocery", "/spent undo")
    # or last ("/client bella drop", "/birthday keegan forget"). Not the middle,
    # or "/remind 5pm drop off the package" would be refused.
    args = re.findall(r"[a-z]+", text[found.end():].lower())
    if args and (args[0] in DENY or args[-1] in DENY):
        return None
    return text


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
    return f"""\
You can act, not only answer. When Landen asks you to do something one of these \
commands does -- in his message, or by saying yes to something you offered in \
CONVERSATION -- add a third key to your JSON, "do": a list of the commands, \
exactly as he would type them. At most {MAX_ACTIONS}.

{usage}

Rules:
- Only what he asked for. Never on your own initiative: if you think a reminder \
would help, offer it ("Want a reminder at 6?") and wait for his yes.
- Nothing destructive: you can't drop, clear, forget or undo anything. If he \
asks for that, tell him the command to type.
- Say what you're doing in speech, briefly and in the present tense ("On it: \
oat milk's going on the list."). The results of each command are shown to him \
under your reply, so don't invent details the command will report.
- If nothing here does what he wants, say so; never claim you did it."""
