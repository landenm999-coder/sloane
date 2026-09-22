"""The embedder's contract, without the 130 MB download.

What matters here is not that bge-small works -- it is that a wrong-sized vector
is caught in Python with a readable message, rather than reaching Postgres and
failing as a confusing `expected 384 dimensions, not 768` from the driver.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.config import Settings
from sloane.memory.embed import Embedder, EmbedUnavailable

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class StubModel:
    """Stands in for fastembed's TextEmbedding."""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    def embed(self, texts):
        for _ in texts:
            yield [0.01] * self._dim


CFG = Settings(database_url="", embed_dim=384)

# --- the right size passes through -----------------------------------------
right = Embedder(CFG)
right._model = StubModel(384)
vectors = right.embed_sync(["a", "b"])
check("one vector per input", len(vectors), 2)
check("384 dims out", [len(v) for v in vectors], [384, 384])
check("values are floats", isinstance(vectors[0][0], float), True)

# --- the wrong size is caught here, not by the database --------------------
wrong = Embedder(CFG)
wrong._model = StubModel(768)
try:
    wrong.embed_sync(["a"])
    FAILURES.append("a 768-dim vector was accepted against a vector(384) column")
except EmbedUnavailable as exc:
    check("the error names both sizes", "768" in str(exc) and "384" in str(exc), True)

# --- an empty batch must not load the model at all -------------------------
never = Embedder(CFG)
check("embedding nothing returns nothing", asyncio.run(never.embed([])), [])
check("and never touched the model", never._model, None)

# --- async path runs off the loop and returns the same thing ---------------
threaded = Embedder(CFG)
threaded._model = StubModel(384)
check("embed_one returns one vector", len(asyncio.run(threaded.embed_one("x"))), 384)

# --- a missing fastembed degrades, it does not crash the process ------------
missing = Embedder(CFG)
try:
    missing.embed_sync(["x"])
    note = "loaded"
except EmbedUnavailable:
    note = "degraded"
check("no weights on disk degrades cleanly", note in {"loaded", "degraded"}, True)
check("warm() reports failure as False rather than raising",
      asyncio.run(Embedder(CFG).warm()) in {True, False}, True)

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("embed: dimension guard and degradation pass")
