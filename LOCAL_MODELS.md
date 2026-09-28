# Local models: Sloane on hardware you own

Some of Sloane already runs on the box with no cloud at all:

- her memory search (the bge-small embedder)
- her British voice, if you set `PIPER_VOICE`

This guide adds the rest: a language model, and optionally speech-to-text, on
your own hardware. A Raspberry Pi 5 is the obvious choice; a PC with a graphics
card is a much faster one.

Nothing about this is required. With no local model set up, she runs exactly
as before.

## What a local model is good for

| Use | Setting | On a Pi 5 |
|---|---|---|
| **Last-resort fallback.** When Claude, Groq and Anthropic are all unreachable, she still answers. | set `LOCAL_BASE_URL` + `LOCAL_MODEL`, nothing else | yes |
| **Bulk work.** The nightly learning and diary, inbox triage: volume work where a few minutes don't matter. | `BULK_PROVIDER=local` | yes, the best fit |
| **Everything.** Chat too, no cloud model at all. | `MAIN_PROVIDER=local` | too slow for chat; use a PC with a GPU |

Why the Pi is slow for chat: every reply first reads her whole context (her
persona, your schedule, memory: several thousand words). A Pi 5 reads that at
tens of words a second, so a reply can take minutes before the first word. At
night, for the learner, nobody is waiting.

Rough speeds for writing, on a Pi 5 with 4-bit models: a 1B model at about
15 words/s, a 3B at 5–8, a 7–8B at 2–3 (the 7–8B needs the 16 GB Pi).

## Set up Ollama on the Pi (15 minutes)

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Set a
   hostname (say `raspberrypi`), your Wi-Fi, and SSH in the imager's settings.
2. SSH in, then install Ollama and pull a model:

   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ollama pull llama3.2:3b        # or qwen2.5:3b; qwen2.5:7b on a 16 GB Pi
   ```

3. Let Sloane's box reach it. Ollama listens only on the Pi itself by default:

   ```bash
   sudo systemctl edit ollama
   #   add these two lines, save, exit:
   #   [Service]
   #   Environment="OLLAMA_HOST=0.0.0.0:11434"
   sudo systemctl restart ollama
   ```

   Ollama has **no password**, so keep it off the internet. Put the Pi on your
   Tailscale network (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`)
   and use its Tailscale name below. Never port-forward 11434 on your router.

4. On Sloane's box, in `/opt/sloane/.env`:

   ```bash
   LOCAL_BASE_URL=http://raspberrypi:11434/v1     # the Pi's Tailscale name
   LOCAL_MODEL=llama3.2:3b
   BULK_PROVIDER=local                            # optional: nightly work on the Pi
   LOCAL_TIMEOUT=600                              # a Pi reading a whole day takes a while
   ```

   Then `sudo systemctl restart sloane`, and check it:

   ```bash
   cd /opt/sloane && sudo docker compose run --rm sloane python scripts/doctor.py
   ```

   The `local model` line should say it answered, and how fast.

## Or: run all of Sloane on the Pi

Her image is built for 64-bit ARM, so a **Pi 5 with 8 GB** can be her whole
box instead of Oracle or your PC. The installer works on Raspberry Pi OS
(64-bit):

```bash
curl -fsSL https://raw.githubusercontent.com/landenm999-coder/sloane/main/scripts/install.sh | bash
```

Her memory lives in Supabase, so moving her from one box to another loses
nothing. With Ollama on the same Pi, use
`LOCAL_BASE_URL=http://host.docker.internal:11434/v1` (her container reaches
the Pi through that name), and keep `MAIN_PROVIDER=claude_code` for chat.

## Local speech-to-text (optional)

Voice notes go to Groq's Whisper by default. For a local Whisper, run a server
with the OpenAI audio API, such as [speaches](https://github.com/speaches-ai/speaches),
next to Ollama:

```bash
LOCAL_STT_MODEL=Systran/faster-whisper-small
LOCAL_STT_BASE_URL=http://raspberrypi:8000/v1
```

On a Pi, `small` transcribes a 15-second note in a few seconds; `base` is
quicker and a little less accurate.

## Troubleshooting

| Doctor says | Fix |
|---|---|
| `can't list the local models ... is the server up` | `systemctl status ollama` on the Pi; check `OLLAMA_HOST` (step 3) and that both machines are on Tailscale |
| `'llama3.2:3b' isn't on that server` | `ollama pull llama3.2:3b` on the Pi |
| the `settings` line flags `LOCAL_BASE_URL` | it needs `http://…:11434/v1`, and not `localhost` (inside her container that's the container itself) |
| `slow for chat` | expected on a Pi; keep it on `BULK_PROVIDER`, or use a PC with a GPU for `MAIN_PROVIDER` |
