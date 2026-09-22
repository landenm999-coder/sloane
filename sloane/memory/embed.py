"""Local embeddings. No API, no key, no per-token cost.

bge-small-en-v1.5 through fastembed, 384 dimensions, on the box. Groq has no
embedding API, and paying per token to embed every message Landen sends would be
the one line item that scales with use. The model is ~130 MB on disk and loads
once per process.

fastembed is imported lazily: import-time tests and `python scripts/doctor.py`
must work on a machine that has not downloaded the weights yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from sloane.config import Settings, settings as default_settings

log = logging.getLogger(__name__)


class EmbedUnavailable(RuntimeError):
    """fastembed is missing or the model could not load.

    Callers treat this the way they treat any memory failure: log it, skip the
    embedding, still answer. An episode with a null embedding is invisible to
    similarity search but still counts as recent history.
    """


class Embedder:
    def __init__(self, config: Settings | None = None) -> None:
        self._config = config or default_settings()
        self._model = None

    @property
    def dim(self) -> int:
        return self._config.embed_dim

    @property
    def cache_dir(self) -> str | None:
        return self._config.embed_cache_dir or None

    def cached(self) -> bool:
        """True if the weights are already on disk, so no fetch is needed."""
        if not self.cache_dir:
            return False
        root = Path(self.cache_dir)
        return root.is_dir() and any(root.rglob("*.onnx"))

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbedUnavailable("fastembed is not installed") from exc
        try:
            self._model = TextEmbedding(
                model_name=self._config.embed_model,
                cache_dir=self.cache_dir,
            )
        except Exception as exc:  # noqa: BLE001 - download and load can fail many ways
            raise EmbedUnavailable(
                f"could not load {self._config.embed_model}: {exc}. "
                "The weights are fetched from Hugging Face on first use; "
                "that host must be reachable once, and EMBED_CACHE_DIR should "
                "point at a volume so a rebuild does not re-fetch them."
            ) from exc
        return self._model

    def embed_sync(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        vectors = [[float(x) for x in v] for v in model.embed(list(texts))]
        for v in vectors:
            if len(v) != self.dim:
                raise EmbedUnavailable(
                    f"{self._config.embed_model} returned {len(v)} dims, "
                    f"expected {self.dim}; the schema's vector({self.dim}) would reject it"
                )
        return vectors

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed off the event loop. ONNX inference is CPU-bound and blocking."""
        if not texts:
            return []
        return await asyncio.to_thread(self.embed_sync, texts)

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]

    async def warm(self) -> bool:
        """Load the weights ahead of the first real request.

        Returns whether recall is available this run. False is survivable: she
        answers from FACTS and tier 3 stays empty. It is never faked -- a
        stand-in embedder would return meaningless similarity, and surfacing a
        random old episode as relevant context is worse than no context.
        """
        try:
            vector = await self.embed_one("warm")
        except EmbedUnavailable as exc:
            log.warning("recall disabled, embeddings unavailable: %s", exc)
            return False
        log.info(
            "embeddings ready: %s, %s dims%s",
            self._config.embed_model,
            len(vector),
            f", cached in {self.cache_dir}" if self.cache_dir else " (no EMBED_CACHE_DIR set)",
        )
        return True
