"""The local voice: a Piper voice by name, fetched once, loaded once, spoken fast.

No network and no real voice file (Hugging Face is out of reach here): a fake
piper module stands in for the download and the synthesis. When the real
piper-tts package is installed, its API is checked against what we call.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import sys
import tempfile
import types
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.providers import tts
from sloane.providers.base import ProviderError
from sloane.providers.tts import PiperTTS
from sloane.router import Router

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- the real package, when installed, still has the calls we make -----------------------
try:
    import piper as real_piper
    from piper.download_voices import download_voice as real_download
except ImportError:
    real_piper = None
if real_piper is not None:
    check("PiperVoice.load(model_path)", list(inspect.signature(real_piper.PiperVoice.load).parameters)[:1],
          ["model_path"])
    check("synthesize_wav(text, wav_file)",
          list(inspect.signature(real_piper.PiperVoice.synthesize_wav).parameters)[:3], ["self", "text", "wav_file"])
    check("download_voice(voice, download_dir)", list(inspect.signature(real_download).parameters)[:2],
          ["voice", "download_dir"])
for name in [m for m in sys.modules if m == "piper" or m.startswith("piper.")]:
    del sys.modules[name]


# -- a fake piper ---------------------------------------------------------------------------
class FakeVoice:
    loads: list[str] = []
    silent = False

    @classmethod
    def load(cls, model_path):
        assert Path(model_path).is_file(), "loaded before it was on disk"
        cls.loads.append(str(model_path))
        return cls()

    def synthesize_wav(self, text, wav_file):
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"" if FakeVoice.silent else b"\x00\x10" * 2205)


fetches: list[str] = []
fetch_mode = {"how": "ok"}


def fake_download(voice, download_dir, force_redownload=False):
    fetches.append(voice)
    (Path(download_dir) / f"{voice}.onnx").write_bytes(b"onnx")
    if fetch_mode["how"] == "partial":
        return  # no config: a download cut short
    if fetch_mode["how"] == "error":
        raise OSError("HTTP Error 404: Not Found")
    (Path(download_dir) / f"{voice}.onnx.json").write_text("{}")


def install_fake() -> None:
    module = types.ModuleType("piper")
    module.PiperVoice = FakeVoice
    downloads = types.ModuleType("piper.download_voices")
    downloads.download_voice = fake_download
    sys.modules["piper"] = module
    sys.modules["piper.download_voices"] = downloads


def remove_fake() -> None:
    sys.modules["piper"] = None  # import piper -> ImportError
    sys.modules.pop("piper.download_voices", None)


def frames(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes()


def reset() -> None:
    FakeVoice.loads.clear()
    FakeVoice.silent = False
    fetches.clear()
    fetch_mode["how"] = "ok"
    PiperTTS._voices.clear()
    PiperTTS._fetches.clear()
    PiperTTS._failed_at.clear()


async def main() -> None:
    install_fake()
    with tempfile.TemporaryDirectory() as models:
        settings = isolated(piper_voice="en_GB-cori-medium", embed_cache_dir=models, speak_provider="piper")
        piper = PiperTTS(settings)
        check("a voice by name lives in the models volume", piper.path, Path(models) / "en_GB-cori-medium.onnx")
        check("a path is a path", PiperTTS(isolated(piper_voice="/v/x.onnx")).path, Path("/v/x.onnx"))
        check("no voice, no path", PiperTTS(isolated(piper_voice="")).path, None)

        # -- the first voice note doesn't wait for a download ------------------------------------
        reset()
        try:
            await piper.synthesize("Good evening.")
            FAILURES.append("a missing voice must not be waited for")
        except ProviderError as exc:
            check("a reply never waits on a download", exc.message, "still fetching en_GB-cori-medium")
        try:
            await piper.synthesize("Good evening.")
        except ProviderError:
            pass
        await PiperTTS._fetches["en_GB-cori-medium"]
        check("fetched once, however many replies asked", fetches, ["en_GB-cori-medium"])
        check("model and config in place", sorted(p.name for p in Path(models).iterdir() if p.is_file()),
              ["en_GB-cori-medium.onnx", "en_GB-cori-medium.onnx.json"])

        # -- then it speaks, loading the voice once -----------------------------------------------
        first = await piper.synthesize("Good evening.")
        second = await PiperTTS(settings).synthesize("Two things are due tomorrow.")
        check("speaks WAV", (frames(first.wav), first.usage.provider, first.usage.model),
              (2205, "piper", "en_GB-cori-medium"))
        check("the voice is loaded once, not per reply", len(FakeVoice.loads), 1)
        check("and shared across instances", frames(second.wav), 2205)
        FakeVoice.silent = True
        try:
            await piper.synthesize("x")
            FAILURES.append("silence must not pass as speech")
        except ProviderError as exc:
            check("an empty clip is a failure the router degrades on", exc.message, "no audio")

    # -- a download cut short never looks like a voice -----------------------------------------
    for how in ("partial", "error"):
        with tempfile.TemporaryDirectory() as models:
            reset()
            fetch_mode["how"] = how
            piper = PiperTTS(isolated(piper_voice="en_GB-alan-medium", embed_cache_dir=models))
            check(f"warm() reports a failed fetch ({how})", await piper.warm(), False)
            check(f"nothing half-written is left ({how})",
                  [p.name for p in Path(models).rglob("*") if p.is_file()], [])
            tries = len(fetches)
            for _ in range(3):
                try:
                    await piper.synthesize("x")
                except ProviderError:
                    pass
            await asyncio.sleep(0)
            check(f"a failed fetch isn't retried on every voice note ({how})", len(fetches), tries)

    # -- warm() at startup fetches and loads -------------------------------------------------------
    with tempfile.TemporaryDirectory() as models:
        reset()
        settings = isolated(piper_voice="en_GB-cori-medium", embed_cache_dir=models, speak_provider="piper")
        router = Router(settings)
        await router.prewarm_voice()
        check("prewarm fetches and loads before the first voice note", (fetches, len(FakeVoice.loads)),
              (["en_GB-cori-medium"], 1))
        audio = await router.speak("Good evening.")
        check("so the first one is already local", audio.usage.provider, "piper")
    reset()
    await Router(isolated(piper_voice="", speak_provider="groq")).prewarm_voice()
    check("no local voice set: nothing to warm", (fetches, FakeVoice.loads), ([], []))

    # -- a wrong path is said plainly ---------------------------------------------------------------
    try:
        await PiperTTS(isolated(piper_voice="/nope/voice.onnx")).synthesize("x")
        FAILURES.append("a missing file must fail")
    except ProviderError as exc:
        check("a path that isn't there", exc.message, "PIPER_VOICE does not point at an .onnx voice file")

    # -- without the package: the piper binary, as before ---------------------------------------------
    remove_fake()
    reset()
    with tempfile.TemporaryDirectory() as tmp:
        voice = Path(tmp) / "en_US-amy-medium.onnx"
        voice.write_bytes(b"onnx")
        try:
            await PiperTTS(isolated(piper_voice=str(voice), piper_bin="no-such-piper")).synthesize("x")
            FAILURES.append("no package and no binary must fail")
        except ProviderError as exc:
            check("no package, no binary", exc.message, "no-such-piper is not on PATH")
        binary = Path(tmp) / "piper"
        binary.write_text(
            f"#!{sys.executable}\n"
            "import sys, wave\n"
            "out = sys.argv[sys.argv.index('--output_file') + 1]\n"
            "sys.stdin.read()\n"
            "w = wave.open(out, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)\n"
            "w.writeframes(b'\\x00\\x01' * 1600); w.close()\n"
        )
        binary.chmod(0o755)
        audio = await PiperTTS(isolated(piper_voice=str(voice), piper_bin=str(binary))).synthesize("hello")
        check("the binary still works without the package", frames(audio.wav), 1600)
        try:
            await PiperTTS(isolated(piper_voice="en_GB-cori-medium", embed_cache_dir=tmp)).synthesize("x")
            FAILURES.append("a name needs the package to fetch")
        except ProviderError as exc:
            check("a voice by name needs the package", exc.message,
                  "fetching a voice by name needs the piper-tts package")
    sys.modules.pop("piper", None)


asyncio.run(main())
check("tts module still exports the Groq path", hasattr(tts, "GroqTTS"), True)

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("piper: voices by name fetched once and atomically, loaded once, warmed at startup, binary fallback")
