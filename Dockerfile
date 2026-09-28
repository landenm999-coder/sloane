# Sloane. Built for Oracle Cloud Always Free, which is ARM (Ampere A1), so
# everything here has to resolve to an aarch64 wheel -- psycopg, onnxruntime
# and tokenizers all publish one, which is why no compiler is installed.
#
# Node is here for one reason: MAIN_PROVIDER=claude_code shells out to the
# Claude Code CLI, which bills against Landen's Pro subscription instead of API
# credits. That is the whole reason this runs at $0/mo.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl for the healthcheck and the Node install; ca-certificates for TLS to
# Telegram, Groq, Canvas and Supabase; git for the workshop's clone of her repo. postgresql-client gives the box `psql`
# so the migrations can be applied without installing anything else. ffmpeg
# turns speech into the OGG/Opus Telegram plays as a voice note (P3).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates postgresql-client ffmpeg git \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force \
 && apt-get purge -y --auto-remove \
 && rm -rf /var/lib/apt/lists/*

# Not root. The two named volumes inherit their ownership from these paths the
# first time they mount, which is what stops the usual permission fight.
RUN useradd --create-home --uid 10001 sloane \
 && mkdir -p /home/sloane/.claude /var/lib/sloane/models /var/lib/sloane/workshop /var/lib/sloane/deploy \
 && chown -R sloane:sloane /home/sloane /var/lib/sloane

WORKDIR /app

# Dependencies first, so a code change does not re-download 300 MB of wheels.
COPY --chown=sloane:sloane requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=sloane:sloane . .

USER sloane
ENV HOME=/home/sloane \
    PATH=/home/sloane/.local/bin:$PATH \
    BIND_HOST=0.0.0.0

# BIND_HOST=0.0.0.0 is inside the container only; compose publishes the port on
# the host's 127.0.0.1. Loopback only. Nothing about Sloane needs an inbound port: Telegram is long
# polling, which is outbound. See DEPLOY.md.
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "-m", "sloane.main"]
