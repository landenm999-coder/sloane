"""Environment to typed settings. Nothing else in the package reads os.environ.

The two lines that matter live here as MAIN_PROVIDER and BULK_PROVIDER. Moving
from the free tier to paid is editing those and nothing else.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- storage -------------------------------------------------------------
    database_url: str = ""

    # --- the upgrade lever ---------------------------------------------------
    # claude_code | groq | anthropic
    main_provider: str = "claude_code"
    bulk_provider: str = "groq"

    # --- claude code CLI (draws on the Claude Pro subscription, not credits) --
    claude_cli: str = "claude"
    # A hung provider costs Landen this long before the router even tries the
    # next one. Two minutes of silence on a Telegram reply reads as broken, and
    # a fallback chain that slow is barely a fallback. Keep it under a minute;
    # the batched P4 work that genuinely needs longer can raise it per call.
    claude_cli_timeout: int = 45

    # --- groq ----------------------------------------------------------------
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    groq_stt_model: str = "whisper-large-v3-turbo"

    # --- anthropic api (the paid destination of the upgrade lever) -----------
    anthropic_api_key: str = ""
    anthropic_main_model: str = "claude-sonnet-5"
    anthropic_bulk_model: str = "claude-haiku-4-5"
    # Effort is supported on Sonnet 5 but rejected by Haiku 4.5. Leave unset
    # unless both configured models accept it.
    anthropic_effort: str = ""

    # --- telegram ------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: int = 0
    telegram_poll_timeout: int = 50

    # --- embeddings (local: no API, no key, no per-token cost) ---------------
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    # Where the ONNX weights live once downloaded. Point this at a mounted
    # volume: without it the ~130 MB model is re-fetched on every container
    # rebuild, and a rebuild during an outage leaves her with no recall at all.
    embed_cache_dir: str = ""
    # Refuse to start the poller until the weights are on disk. Off by default
    # so a machine that cannot reach Hugging Face still answers from FACTS.
    embed_required: bool = False

    # --- behaviour -----------------------------------------------------------
    timezone: str = "America/Denver"
    max_reply_tokens: int = 1024

    # Context budget per tier, in tokens. Tiers 1 and 2 ride in every prompt.
    budget_state: int = 1500
    budget_working_set: int = 1500
    budget_episodes: int = 2000
    budget_entities: int = 500

    # Retrieval decay: cosine similarity times 0.5 ** (age_days / half_life).
    recency_half_life_days: float = 14.0
    retrieval_limit: int = 12

    # Fixed 3-7 PM Mon-Fri rule. Work never posts a schedule; we generate it.
    shift_start_hour: int = 15
    shift_end_hour: int = 19

    quiet_start_hour: int = 0
    quiet_end_hour: int = 6
    quiet_end_minute: int = 30

    log_level: str = "INFO"

    @property
    def total_budget(self) -> int:
        return (
            self.budget_state
            + self.budget_working_set
            + self.budget_episodes
            + self.budget_entities
        )


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide settings. Cached so the .env file is read once."""
    return Settings()
