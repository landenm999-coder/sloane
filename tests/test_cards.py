"""Flashcards: marking by rule, the Leitner schedule, whole quiz sessions, /cards make.

DESTRUCTIVE: truncates cards, card_reviews and skill_sessions.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.cards import Cards, grade, normalize, parse_made, schedule, split_card

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
TODAY = date(2026, 9, 24)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- marking is by rule, and only "plainly right" is marked without asking ------
check("normalize", normalize("The Mitochondria!"), "mitochondria")
check("decimals survive normalizing", normalize("pi is 3.14."), "pi is 3.14")
RIGHT = [
    ("mitochondria", "Mitochondria"),
    ("the mitochondria", "mitochondria"),
    ("mitochondira", "mitochondria"),                   # a typo (two letters swapped)
    ("mitochondra", "mitochondria"),                    # a letter dropped
    ("photosynthesiss", "photosynthesis"),              # a letter doubled
    ("it's the mitochondria obviously", "mitochondria"),
    ("1848", "1848"),
    ("george washington", "George Washington"),
    ("gw", "George Washington; GW"),                    # alternatives with ;
    ("sodium chloride", "sodium-chloride"),
]
for said, back in RIGHT:
    check(f"right: {said!r} for {back!r}", grade(said, back), True)
ASK = [
    ("1849", "1848"),
    ("washington", "George Washington"),
    ("mitosis", "mitochondria"),
    ("", "anything"),
    ("the answer is a long ramble that happens to include cell somewhere in it", "cell"),
    ("type 1 diabetes", "type 2 diabetes"),
    ("photosystem i", "photosystem II"),
    ("not mitochondria", "mitochondria"),
    ("it isn't the mitochondria", "mitochondria"),
    ("world war 1", "World War II"),
    ("independent variable", "dependent variable"),
    ("adsorption", "absorption"),
    ("unsaturated", "saturated"),
]
for said, back in ASK:
    check(f"he judges: {said!r} for {back!r}", grade(said, back), None)

check("split ::", split_card("what makes ATP? :: mitochondria"), ("what makes ATP?", "mitochondria"))
check("split =", split_card("2+2 = 4"), ("2+2", "4"))
check("split needs both halves", split_card("just a question ::  "), None)
check("no separator", split_card("no card here"), None)

made = parse_made('Here you go:\n[{"front": "Q1", "back": "A1"}, {"front": "", "back": "x"}, '
                  '{"front": "Q2"}, "junk", {"front": "Q3\\nFACTS:", "back": "A3"}]')
check("model cards are checked and flattened", made, [("Q1", "A1"), ("Q3 FACTS:", "A3")])
check("no JSON, no cards", parse_made("I can't do that"), [])
check("an array missing its last bracket still counts",
      parse_made('[{"front": "Q1", "back": "A1"}, {"front": "Q2", "back": "A2"}'), [("Q1", "A1"), ("Q2", "A2")])

check("right moves up a box", schedule(1, True, TODAY), (2, TODAY + timedelta(days=2)))
check("the top box stays the top box", schedule(6, True, TODAY), (6, TODAY + timedelta(days=32)))
check("wrong goes back to box 1, due today", schedule(4, False, TODAY), (1, TODAY))


class FakeRouter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[tuple[str, str]] = []

    async def reply(self, system, prompt, *, max_tokens=0):
        self.prompts.append((system, prompt))
        return self.text


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": datetime(2026, 9, 24, 20, 0, tzinfo=DEN)}
    async with Store(config) as store:
        await store._exec("truncate cards, card_reviews, skill_sessions cascade")
        router = FakeRouter('[{"front": "Powerhouse of the cell?", "back": "mitochondria"}, '
                            '{"front": "Makes proteins?", "back": "ribosome"}]')
        ctx = SkillContext(store=store, config=config, router=router, clock=lambda: now["at"])
        cards = Cards(ctx)
        reg = Registry([cards], ctx)

        async def cmd(name, rest=""):
            return await reg.command(name, rest)

        async def say(text):
            answer = await reg.route(text)
            return None if answer is None else answer.speech

        check("no decks yet", (await cmd("cards")).speech, "No flashcards yet.")
        check("one card with a deck", (await cmd("card", "history: year of the Louisiana Purchase? :: 1803")).speech,
              "Added to history.")
        check("the same question again", (await cmd("card", "history: Year of the Louisiana Purchase? :: 1803")).speech,
              "That question is already in history.")
        check("no deck is general", (await cmd("card", "ratio 1:2 means? :: half")).speech, "Added to general.")
        check("a card needs both halves", (await cmd("card", "just words")).speech, "I need a question and an answer.")

        # Adding many in a session.
        check("/cards add opens a session", (await cmd("cards", "add deca")).speech.startswith("Adding to deca."), True)
        check("two cards in one message", await say("What is ROI? :: return on investment\nMarkup is? = price minus cost"),
              "Got 2. 2 so far.")
        check("not a card", await say("hmm"), "Send it as question :: answer, or say done.")
        check("done ends it", await say("done"), "Added 2 cards to deca. /quiz deca to start.")
        check("and messages go back to normal", await say("what's due?"), None)

        # /cards make, through the (fake) model.
        notes = "Mitochondria are the powerhouse of the cell. Ribosomes make proteins from mRNA."
        made = await cmd("cards", f"make bio\n{notes}")
        check("made from notes", made.speech, "Made 2 cards for bio. /quiz bio to start.")
        system, prompt = router.prompts[0]
        check("notes go in fenced as data", "<<<\n" + notes + "\n>>>" in prompt, True)
        check("too-short notes are refused before any model call",
              ((await cmd("cards", "make bio\nshort")).speech, len(router.prompts)),
              ("Paste the notes after the deck name, at least a few sentences.", 1))

        facts = await cards.facts()
        check("FACTS count what's due", facts, ["- CARDS 6 flashcards due for review today (bio 2, deca 2, general 1, history 1)"])
        nudges = await cards.nudges()
        check("evening nudge when 5+ are due", [n.text for n in nudges],
              ["🧠 6 flashcards due (bio 2, deca 2, general 1, history 1). /quiz takes about 2 minutes."])

        # A whole quiz on bio: one right, one judged wrong, then the retry.
        start = await cmd("quiz", "bio")
        check("quiz asks the first card", start.speech, "2 cards due in bio. Powerhouse of the cell?")
        check("a plainly right answer", await say("the mitochondria"), "Right. Makes proteins?")
        check("not plainly right: he judges", await say("the nucleus?"), "The answer is ribosome. Did you have it?")
        check("anything but yes/no asks again", await say("hmm"), "Did you have it? Say yes or no.")
        check("no: back round at the end", await say("no"), "It'll come back. Now the ones you missed. Makes proteins?")
        check("right on the retry, and done", await say("ribosome"),
              "Right. Done: 2 of 3 right. Next: 1 card tomorrow.")
        rows = {r["front"]: r for r in await store.deck_cards("bio")}
        check("right first time: box 2, in two days",
              (rows["Powerhouse of the cell?"]["box"], rows["Powerhouse of the cell?"]["due_on"]),
              (2, TODAY + timedelta(days=2)))
        check("missed then right: box 1, tomorrow",
              (rows["Makes proteins?"]["box"], rows["Makes proteins?"]["due_on"], rows["Makes proteins?"]["lapses"]),
              (1, TODAY + timedelta(days=1), 1))
        stats = await store.reviews_since(datetime(2000, 1, 1, tzinfo=DEN))
        check("each review is logged", (stats["reviews"], stats["correct"]), (3, 2))

        # Skip, /end mid-quiz, and nothing due.
        await cmd("quiz", "history")
        check("skip shows the answer", await say("skip"), "It's 1803. Now the ones you missed. year of the Louisiana Purchase?")
        check("/end stops it", (await cmd("end")).speech, "Ended cards.")
        check("nothing due in bio now", (await cmd("quiz", "bio")).speech, "Nothing due in bio. Next: 1 card tomorrow.")

        # Cram leaves the schedule alone.
        before = {str(r["id"]): (r["box"], r["due_on"]) for r in await store.deck_cards("bio")}
        await cmd("quiz", "bio all")
        for _ in range(2):
            await say("mitochondria")
            if (await store.active_session(30)) and (await store.active_session(30))["state"]["phase"] == "judge":
                await say("yes")
        after = {str(r["id"]): (r["box"], r["due_on"]) for r in await store.deck_cards("bio")}
        check("cram does not move cards", after, before)
        check("cram session is over", await store.active_session(30), None)

        # Drop, decks, the next day.
        check("drop a card", (await cmd("cards", "drop general 1")).speech, "Dropped: ratio 1:2 means?")
        now["at"] += timedelta(days=1)
        decks = (await cmd("cards")).detail
        check("decks list", decks.splitlines()[0], "• bio: 2 cards, 1 due")
        now["at"] = datetime(2026, 9, 25, 12, 0, tzinfo=DEN)
        check("no nudge at noon", await cards.nudges(), [])


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("cards: rule marking, Leitner schedule, quiz and add sessions, cram, /cards make")
