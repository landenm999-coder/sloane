"""The coin skill: Heads or Tails, and rules that don't over-reach. No database."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.coin import Coin

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


async def main() -> None:
    ctx = SkillContext(store=None, config=isolated(), router=None)
    reg = Registry([Coin(ctx)], ctx)

    check("/coin is registered", "coin" in reg.command_names, True)
    check("/help lists it", any("/coin" in line for line in reg.help_lines()), True)

    seen = set()
    for _ in range(200):
        answer = await reg.command("coin", "")
        check("/coin answers Heads or Tails", answer.speech in {"Heads", "Tails"}, True)
        seen.add(answer.speech)
    check("both sides come up", seen, {"Heads", "Tails"})

    for text in ["flip a coin", "Flip a coin?", "toss a coin", "heads or tails?", "coin flip",
                 "Sloane, flip a coin", "can you flip a coin please", "flip me a coin"]:
        answer = await reg.route(text)
        check(f"ours: {text!r}", answer is not None and answer.speech in {"Heads", "Tails"}, True)

    for text in ["flip a coin to decide who pays for lunch", "spent 5 on a coin", "how much is a coin worth",
                 "bitcoin price", "heads up, the shift moved", "I lost a coin flip", "what's on my calendar?"]:
        check(f"not ours: {text!r}", await reg.route(text), None)


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("coin: /coin and plain phrases give Heads or Tails; rules that don't over-reach")
