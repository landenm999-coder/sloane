"""Settings that ignore the ambient environment.

pydantic-settings falls back to os.environ for any field the caller does not
pass, so a test that omits one silently picks up whatever the developer has
exported. That is how a suite starts passing on an empty machine and failing on
a configured one -- or worse, how a test quietly starts talking to Landen's
real Canvas.

Every external-service field is blanked here unless a test asks for it.
"""

from __future__ import annotations

from sloane.config import Settings

# Anything that points at a real service or credential. Blank by default so a
# test has to opt in, by name, to touching the outside world.
EXTERNAL: dict[str, object] = {
    "database_url": "",
    "groq_api_key": "",
    "anthropic_api_key": "",
    "anthropic_effort": "",
    "canvas_base_url": "",
    "canvas_token": "",
    "calendar_ics_url": "",
    "telegram_bot_token": "",
    "telegram_chat_id": 0,
    "embed_cache_dir": "",
    "gmail_client_id": "",
    "gmail_client_secret": "",
    "gmail_refresh_token": "",
    "gmail_token_url": "",
    "gmail_api_base": "",
    "capture_token": "",
    "weather_location": "",
}


def isolated(**overrides: object) -> Settings:
    """Build Settings from explicit values only."""
    return Settings(**{**EXTERNAL, **overrides})
