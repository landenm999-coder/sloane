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
    # Pin the model `claude -p` uses. Empty means the CLI's own default for the
    # subscription. A heavier model spends Landen's Pro limits faster -- which
    # is precisely the build plan's trigger for moving to the paid lane -- so
    # this is opt-in rather than defaulted to the biggest thing available.
    claude_cli_model: str = ""
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

    # --- voice out (P3) -------------------------------------------------------
    # groq | piper. The other is the fallback. Either failing leaves a text
    # reply, never no reply.
    speak_provider: str = "groq"
    groq_tts_model: str = "canopylabs/orpheus-v1-english"
    groq_tts_voice: str = "hannah"
    piper_bin: str = "piper"
    piper_voice: str = ""  # path to a Piper .onnx voice on the box
    ffmpeg_bin: str = "ffmpeg"
    # Groq's free TTS allowance is about 100 requests a day, and a long reply
    # can take two. Past this she answers in text and says so in the log.
    daily_speak_budget: int = 80

    # --- anthropic api (the paid destination of the upgrade lever) -----------
    anthropic_api_key: str = ""
    anthropic_main_model: str = "claude-sonnet-5"
    anthropic_bulk_model: str = "claude-haiku-4-5"
    # Effort is supported on Sonnet 5 but rejected by Haiku 4.5. Leave unset
    # unless both configured models accept it.
    anthropic_effort: str = ""

    # --- school (P1). All read-only. ----------------------------------------
    # Canvas. The token is a bearer credential with full read access to his
    # coursework; it is never logged and never echoed into a reply.
    canvas_base_url: str = ""
    canvas_token: str = ""

    # The calendar's secret .ics URL is itself the credential -- anyone holding
    # it can read the whole calendar -- so it is treated like a password.
    calendar_ics_url: str = ""

    # How far either side of today to sync and to answer FACTS from.
    sync_past_days: int = 7
    sync_future_days: int = 45
    # Shifts are generated, not scraped; this is how far ahead.
    shift_weeks_ahead: int = 6

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

    # Ceilings for *scheduled* work only. A message Landen sends is always
    # answered -- rationing his own questions would be the wrong failure. These
    # exist so a runaway job cannot spend the day's free tier before breakfast.
    daily_job_budget: int = 40
    daily_bulk_budget: int = 800  # Groq free tier is 1K/day; leave headroom

    # P4. Ten clean approvals in a row unlock one exact (action, target) pair;
    # trust lapses after this many days unused; any reversal re-gates it.
    trust_unlock_streak: int = 10
    trust_decay_days: int = 60

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
