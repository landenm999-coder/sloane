"""Environment to typed settings. Nothing else in the package reads os.environ.

The two lines that matter live here as MAIN_PROVIDER and BULK_PROVIDER. Moving
from the free tier to paid is editing those and nothing else.
"""

from __future__ import annotations

from functools import lru_cache

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
    groq_model: str = "openai/gpt-oss-120b"
    groq_stt_model: str = "whisper-large-v3-turbo"

    # --- a local model (providers/local.py; LOCAL_MODELS.md) ---------------------
    # Any OpenAI-compatible server on hardware he owns -- Ollama on a Raspberry
    # Pi, llama.cpp, LM Studio. Blank: not configured, and the lanes skip it.
    # MAIN_PROVIDER=local or BULK_PROVIDER=local puts it first in that lane;
    # set but not chosen, it is the last fallback when every cloud lane fails.
    local_base_url: str = ""   # e.g. http://raspberrypi.local:11434/v1 (Ollama)
    local_model: str = ""      # e.g. llama3.2:3b
    local_api_key: str = ""    # most local servers need none
    # A Pi is slow: give it longer than the cloud lanes get.
    local_timeout: int = 180
    # Voice notes transcribed locally too (a Whisper server with the OpenAI
    # audio endpoint, e.g. speaches). Blank: Groq's Whisper, as before. Its
    # address if it isn't the same server as the model (it usually isn't).
    local_stt_model: str = ""
    local_stt_base_url: str = ""

    # --- voice out (P3) -------------------------------------------------------
    # groq | piper. The other is the fallback. Either failing leaves a text
    # reply, never no reply.
    speak_provider: str = "groq"
    groq_tts_model: str = "canopylabs/orpheus-v1-english"
    groq_tts_voice: str = "hannah"
    piper_bin: str = "piper"
    # A Piper voice: a name like en_GB-cori-medium (British; fetched once into
    # EMBED_CACHE_DIR) or a path to an .onnx on the box. SPEAK_PROVIDER=piper
    # makes it her main voice rather than the fallback.
    piper_voice: str = ""
    ffmpeg_bin: str = "ffmpeg"
    # Groq's free TTS allowance is about 100 requests a day, and a long reply
    # can take two. Past this she answers in text and says so in the log.
    daily_speak_budget: int = 80
    # Scheduled briefs to deliver as voice notes, comma-separated job names,
    # e.g. "morning_brief". Empty: all text. A voice failure still sends text.
    voice_briefs: str = ""

    @property
    def voice_brief_names(self) -> frozenset[str]:
        return frozenset(n.strip() for n in self.voice_briefs.split(",") if n.strip())

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
    # Canvas → Calendar → Calendar Feed. Used only without a token: due dates,
    # no grades or turned-in state. A credential, like the calendar's.
    canvas_feed_url: str = ""

    # The calendar's secret .ics URL is itself the credential -- anyone holding
    # it can read the whole calendar -- so it is treated like a password.
    calendar_ics_url: str = ""

    # How far either side of today to sync and to answer FACTS from.
    sync_past_days: int = 7
    sync_future_days: int = 45
    # Shifts are generated, not scraped; this is how far ahead.
    shift_weeks_ahead: int = 6

    # --- gmail (P4) ------------------------------------------------------------
    # An OAuth "Desktop app" client and a refresh token minted once by
    # scripts/gmail_auth.py. Scopes: gmail.readonly + gmail.compose -- read and
    # write drafts/sends, never delete. All three blank means no inbox job.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    # Overridable so tests can point the client at a local stub.
    gmail_token_url: str = ""
    gmail_api_base: str = ""
    # Mail from these domains (or their subdomains) is school. She may save a
    # draft reply for him to send; she may never send one herself. That is the
    # "contact school_staff" hard line, enforced on the address, not the label.
    school_email_domains: str = "dcsdk12.org"
    # What counts as new. Promotions, social and bulk updates never reach triage.
    inbox_query: str = (
        "is:unread newer_than:2d -category:promotions -category:social -category:updates"
    )
    # One bulk call triages up to this many; the rest wait for the next run.
    inbox_batch: int = 40
    # Draft replies proposed per run. Each is a main-lane call and a button
    # press for Landen; more than a few is noise, not help.
    inbox_max_drafts: int = 3

    @property
    def school_domains(self) -> tuple[str, ...]:
        return tuple(
            d.strip().lower().lstrip("@")
            for d in self.school_email_domains.split(",")
            if d.strip()
        )

    # --- capture intake -------------------------------------------------------
    # Bearer token for POST /capture, the one endpoint meant to be reached from
    # off the box (from the Capture app, over Tailscale). Unset, or shorter than
    # 32 characters, means the endpoint is off. Generate one with:
    #   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    capture_token: str = ""
    # Web origins allowed to call /capture from a browser (the Capture app on
    # his phone), comma-separated, e.g. "https://capture.example.app". Empty:
    # no cross-origin access, which is right until Capture is connected.
    # Scoped to the token-checked endpoints only; see sloane/cors.py.
    cors_origins: str = ""

    # --- the control room: /app (sloane/web.py) ----------------------------------
    # A password for the dashboard where he sees everything, runs things and
    # talks to her. Unset, or shorter than 32 characters, and /app is off.
    # Reach it the way Capture does: tailscale serve (DEPLOY section 7e).
    #   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
    dashboard_token: str = ""

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

    # --- backups -----------------------------------------------------------------
    # Where the nightly JSON export goes. Empty means <EMBED_CACHE_DIR>/backups,
    # which in Docker is the `models` volume, so it survives rebuilds.
    backup_dir: str = ""
    backup_keep: int = 14

    # --- skills (sloane/skills/) ----------------------------------------------
    # Comma-separated skill names to switch off without removing their code.
    skills_disabled: str = ""

    @property
    def disabled_skills(self) -> frozenset[str]:
        return frozenset(n.strip() for n in self.skills_disabled.split(",") if n.strip())

    # Weather (skills/weather.py): "latitude,longitude", e.g. "39.52,-104.76"
    # for Parker. Blank means no weather skill. Open-Meteo needs no key.
    weather_location: str = ""
    weather_units: str = "fahrenheit"  # or celsius
    # Overridable so tests can point it at a local stub.
    weather_api_base: str = "https://api.open-meteo.com/v1"

    # Study plan (skills/plan.py): when his own time starts and ends, "HH:MM".
    # School days start after school; the shift and its commute are carved out.
    plan_school_day_start: str = "15:00"
    plan_weekend_start: str = "09:00"
    plan_bedtime: str = "22:30"
    plan_commute_minutes: int = 30

    # Money (skills/money.py): his hourly pay, for "about $240 earned this
    # week" from the shifts. 0 leaves earnings out.
    pay_rate: float = 0.0

    # --- the workshop: features she builds on herself (sloane/workshop.py) ----------
    # A fine-grained GitHub token for her own repo only (Contents and Pull
    # requests read/write; Actions and Checks read-only). Blank: the Workshop
    # still holds ideas and plans, but nothing is built.
    github_token: str = ""
    github_repo: str = "landenm999-coder/sloane"
    # Overridable so tests can point them at a stub and a local git remote.
    github_api_base: str = "https://api.github.com"
    workshop_remote: str = ""
    # Her clone of her own code (a volume). Never the code that is running.
    workshop_dir: str = "/var/lib/sloane/workshop"
    # Shared with the box (compose mounts /opt/sloane/.deploy here). She writes
    # request.json when he accepts; the host's sloane-upgrade unit deploys it,
    # checks her health, rolls back on failure and writes result.json.
    deploy_dir: str = "/var/lib/sloane/deploy"
    # The model her builds use: cheaper than a chat model, still a strong coder.
    workshop_model: str = "sonnet"
    # One build's coding session, and GitHub CI's run, in minutes.
    build_minutes: int = 40
    ci_minutes: int = 30
    # At night (the `workshop` job): builds in all, and how many may be her own ideas.
    workshop_nightly: bool = True
    nightly_builds: int = 2
    nightly_own_ideas: int = 1

    @property
    def workshop_ready(self) -> bool:
        return bool(self.github_token and self.github_repo)

    # --- thinking on her own (jobs/briefs.py `think`) ------------------------------
    # A few times a day she looks over everything and says the one thing worth
    # saying, if there is one. Off: only the briefs, the heartbeat and replies.
    think: bool = True

    # --- behaviour -----------------------------------------------------------
    # What she calls him when she addresses him: his name, or "sir" if he likes.
    address_as: str = "Landen"
    # Web lookups for what she can't know from here (news, prices, scores):
    # his own messages only, through the Claude CLI's search tools alone.
    web_lookup: bool = True
    timezone: str = "America/Denver"
    # Room for a real answer when he wants one: a drafted pitch, a talk-through.
    max_reply_tokens: int = 2048

    # Context budget per tier, in tokens. Tiers 1 and 2 ride in every prompt.
    budget_state: int = 1500
    budget_working_set: int = 1500
    budget_episodes: int = 2000
    # FACTS: school rows first, then the skills' lines with what is left.
    budget_entities: int = 2000
    # The running conversation: what "it", "that" and "why" refer to.
    # A day of it, like a person who was there yesterday too.
    budget_conversation: int = 2500
    conversation_hours: int = 24
    conversation_messages: int = 40

    # Retrieval decay: cosine similarity times 0.5 ** (age_days / half_life).
    recency_half_life_days: float = 14.0
    retrieval_limit: int = 12
    # Of those, how many seats go to the most relevant rows however old they
    # are (store.search_episodes): what makes a month-old conversation findable.
    recall_lasting: int = 4

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
    # Loopback unless told otherwise. The container sets 0.0.0.0 and compose
    # publishes it on 127.0.0.1 only; run bare, /facts and /state must not be
    # on the LAN.
    bind_host: str = "127.0.0.1"

    @property
    def total_budget(self) -> int:
        return (
            self.budget_state
            + self.budget_working_set
            + self.budget_episodes
            + self.budget_entities
            + self.budget_conversation
        )


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide settings. Cached so the .env file is read once."""
    return Settings()
