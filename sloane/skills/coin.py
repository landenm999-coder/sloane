"""Coin: heads or tails.

    /coin
    "flip a coin"  ·  "toss a coin"  ·  "heads or tails?"  ·  "coin flip"

The whole message has to be the request, so "flip a coin to decide who pays
for lunch" and anything about money still go to the agent.
No model, no storage: `secrets` makes it fair and unpredictable.
"""

from __future__ import annotations

import re
import secrets

from sloane.skills import Answer, Skill, SkillContext

SIDES = ("Heads", "Tails")

_ASK = re.compile(
    r"(?:(?:please|can\s+you|could\s+you)\s+)*"
    r"(?:(?:flip|toss)\s+(?:me\s+)?a\s+coin(?:\s+for\s+me)?|heads\s+or\s+tails|coin\s+(?:flip|toss))"
    r"(?:\s+please)?[.!?\s]*",
    re.I,
)


def flip() -> str:
    return secrets.choice(SIDES)


class Coin(Skill):
    name = "coin"
    help = ("`/coin` — flip a coin (or say \"flip a coin\")",)
    commands = frozenset({"coin"})

    async def command(self, name: str, rest: str) -> Answer | None:
        return Answer(flip())

    async def match(self, text: str) -> Answer | None:
        if _ASK.fullmatch(text.replace("’", "'").strip()) is None:
            return None
        return Answer(flip())


def build(ctx: SkillContext) -> Skill:
    return Coin(ctx)
