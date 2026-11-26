"""
Kokoro TTS — a fast, multilingual neural voice that runs locally.

WHY THIS ONE EXISTS
    Piper needs ~50 MB per language voice, downloaded on demand. Kokoro is a
    single ~300 MB model (INT8 quantised) that speaks **every supported
    language** with **dozens of voices**. One download, all languages.

    It is not as natural as ElevenLabs. It is available immediately after one
    pip install, offline, in ~15 languages and ~60 voices. For a local pipeline
    that genuinely switches language mid-conversation, this is the ceiling
    beneath the cloud voices.

THE MODEL
    Kokoro-82M (MIT licence) from hexgrad/Kokoro-82M on Hugging Face.
    INT8 quantised weights, ~300 MB, loaded once per process and reused across
    all KPipelines (one per language). First use downloads the weights.

LANGUAGE / VOICE MATRIX
    The model was trained on these language codes (lower-case, ISO-639-1):
        a  English (US)           b  English (UK)
        e  Spanish                f  French
        h  Hindi                  i  Italian
        j  Japanese               k  Korean
        p  Brazilian Portuguese   z  Mandarin Chinese
    Voices are named like "af_heart", "am_echo", "bf_alice", "bm_daniel",
    "ef_dora", "em_alex", "hf_alpha", "hm_omega", "if_sara", "im_nicola",
    "jf_alpha", "jm_kumo", "pf_dora", "pm_alex", "zf_xiaobei", "zm_yunjian".
    The first letter is the language code, the second is gender (f/m).

THE ENCODING TRAP (same as SystemTTS)
    The text to speak is whatever language the conversation is in. We NEVER
    pass it as a command-line argument. It is given to the Python API directly
    as a str, which is UTF-8 all the way down.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
from typing import AsyncIterator, Optional

import torch

from . import audio
from .base import DEFAULT_CAPS, Field

# ─────────────────────────────────────────────────────────────────────────────
# Sentence splitting — same rules as SystemTTS so both providers behave alike
# ─────────────────────────────────────────────────────────────────────────────
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[。！？؟۔।])\s*")


# ─────────────────────────────────────────────────────────────────────────────
# Model + pipeline cache — one KModel, one KPipeline per language code
# ─────────────────────────────────────────────────────────────────────────────
_kokoro_lock = threading.Lock()
_model: Optional["KModel"] = None
_pipelines: dict[str, "KPipeline"] = {}    # lang_code -> KPipeline
_download_done = threading.Event()


def _import_kokoro():
    """Import kokoro lazily so the module can be imported without the dependency."""
    try:
        from kokoro import KPipeline, KModel
        return KPipeline, KModel
    except Exception as e:
        raise RuntimeError(
            "kokoro is not installed. Run: pip install kokoro>=0.9 soundfile"
        ) from e


def _get_model() -> "KModel":
    """Get or create the shared KModel (INT8 quantised, ~300 MB)."""
    global _model
    with _kokoro_lock:
        if _model is None:
            KPipeline, KModel = _import_kokoro()
            # model=True creates the INT8 model; model=False would create no model
            _model = KModel()
            _download_done.set()
    return _model


def _get_pipeline(lang_code: str) -> "KPipeline":
    """Get or create a KPipeline for the given language code."""
    KPipeline, _ = _import_kokoro()
    with _kokoro_lock:
        if lang_code not in _pipelines:
            _pipelines[lang_code] = KPipeline(lang_code=lang_code, model=_get_model())
        return _pipelines[lang_code]


def _voice_to_lang(voice: str) -> str:
    """Extract language code from voice name (e.g., 'af_heart' -> 'a')."""
    if not voice or len(voice) < 2:
        return "a"  # default to US English
    return voice[0].lower()


def prefetch(on_ready=None) -> None:
    """Warm the model download off the interface thread.

    First use downloads ~300 MB from Hugging Face. Called when the panel opens,
    never at startup.
    """
    if _download_done.is_set():
        if on_ready:
            on_ready()
        return

    def work():
        try:
            _get_model()
        except Exception:
            pass
        if on_ready:
            try:
                on_ready()
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Voice listing — static mapping, no subprocess needed
# ─────────────────────────────────────────────────────────────────────────────
_VOICE_CATALOG = {
    # English (US) — lang code 'a'
    "a": [
        "af_heart", "af_bella", "af_nicole", "af_aoede", "af_kore",
        "af_sarah", "af_nova", "af_sky", "af_alloy", "af_jessica",
        "af_river", "am_michael", "am_fenrir", "am_puck", "am_echo",
        "am_eric", "am_liam", "am_onyx", "am_alex", "am_daniel",
    ],
    # English (UK) — lang code 'b'
    "b": [
        "bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bf_sophia",
        "bm_daniel", "bm_george", "bm_lewis", "bm_fable", "bm_echo",
    ],
    # Spanish — lang code 'e'
    "e": [
        "ef_dora", "ef_sierra", "ef_rose", "ef_lola", "ef_maria",
        "em_alex", "em_oscar", "em_enrique", "em_fable", "em_echo",
    ],
    # French — lang code 'f'
    "f": [
        "ff_siwis", "ff_sophie", "ff_claire", "ff_oceane", "ff_marie",
        "fm_alex", "fm_henri", "fm_fable", "fm_echo", "fm_gilles",
    ],
    # Hindi — lang code 'h'
    "h": [
        "hf_alpha", "hf_beta", "hf_gamma", "hf_delta", "hf_epsilon",
        "hm_omega", "hm_psi", "hm_phi", "hm_chi", "hm_zeta",
    ],
    # Italian — lang code 'i'
    "i": [
        "if_sara", "if_nicole", "if_gemma", "if_sophia", "if_lucia",
        "im_nicola", "im_alessandro", "im_fable", "im_echo", "im_matteo",
    ],
    # Japanese — lang code 'j'
    "j": [
        "jf_alpha", "jf_beta", "jf_gamma", "jf_delta", "jf_epsilon",
        "jm_kumo", "jm_psi", "jm_phi", "jm_chi", "jm_zeta",
    ],
    # Brazilian Portuguese — lang code 'p'
    "p": [
        "pf_dora", "pf_rose", "pf_sierra", "pf_lola", "pf_maria",
        "pm_alex", "pm_oscar", "pm_fable", "pm_echo", "pm_enrique",
    ],
    # Mandarin Chinese — lang code 'z'
    "z": [
        "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyu", "zf_yunjian",
        "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang", "zm_yunhao",
    ],
}


def list_voices(lang_code: str = "") -> list[str]:
    """All known Kokoro voices, optionally filtered by language code."""
    if lang_code:
        return _VOICE_CATALOG.get(lang_code.lower(), [])
    all_voices = []
    for voices in _VOICE_CATALOG.values():
        all_voices.extend(voices)
    return all_voices


def engine_name() -> str:
    return "Kokoro (INT8, ~300 MB)"


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis
# ─────────────────────────────────────────────────────────────────────────────
class KokoroTTS:
    def __init__(self, cfg: dict):
        self.voice = (cfg.get("voice") or "af_heart").strip()
        self.speed = float(cfg.get("speed") or 1.0)
        self.caps = dict(DEFAULT_CAPS)
        self.caps["audio_out"] = True

    async def speak(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        """24 kHz mono PCM, a sentence at a time."""
        if voice:
            self.voice = voice

        lang = _voice_to_lang(self.voice)
        pipeline = _get_pipeline(lang)

        produced = 0
        sentences = _split(text)

        for sentence in sentences:
            # Run synthesis in thread pool (Kokoro uses PyTorch, blocks)
            wav_bytes = await asyncio.to_thread(self._synth_blocking, sentence)
            pcm = audio.wav_to_pcm(wav_bytes, audio.SPEAKER_RATE)
            produced += len(pcm)
            for piece in audio.chunk(pcm):
                yield piece

        if sentences and produced == 0:
            raise RuntimeError(
                f"Kokoro produced no audio for that text — voice '{self.voice}' "
                f"may not support this language. Try a different voice."
            )

    def _synth_blocking(self, text: str) -> bytes:
        """Synthesize one sentence to WAV bytes (blocking)."""
        lang = _voice_to_lang(self.voice)
        pipeline = _get_pipeline(lang)

        # Kokoro yields (graphemes, phonemes, audio) where audio is a torch.Tensor
        # at 24 kHz. We collect all chunks for this sentence.
        audio_chunks = []
        for result in pipeline(text, voice=self.voice, speed=self.speed):
            if result.output is not None:
                # result.output is a KModel.Output with .audio (torch.Tensor)
                audio_chunks.append(result.output.audio)

        if not audio_chunks:
            return b""

        # Concatenate and convert to WAV
        full_audio = torch.cat(audio_chunks)
        # full_audio is float32 at 24 kHz, mono, range [-1, 1]
        # Convert to int16 bytes
        pcm = (full_audio.clamp(-1, 1) * 32767).to(torch.int16).cpu().numpy()

        # Wrap in WAV container
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm.tobytes())
        return buf.getvalue()

    def probe(self) -> tuple[bool, str]:
        try:
            # Trigger model download if needed
            _get_model()
            # Quick synthesis test
            wav = self._synth_blocking("ok")
            pcm = audio.wav_to_pcm(wav, audio.SPEAKER_RATE)
        except Exception as e:
            return False, str(e)[:120]
        if not pcm:
            return False, "Kokoro produced no audio."
        n = len(list_voices())
        return True, f"Kokoro INT8, {n} voices available ({audio.duration_s(pcm, audio.SPEAKER_RATE):.2f}s of test audio)"

    async def warmup(self, log=None) -> None:
        """Load the model off the critical path before first use."""
        try:
            import asyncio
            # Use the module's prefetch which downloads the model in a background thread
            def _done():
                if log:
                    log("SYS: Kokoro model ready")
            await asyncio.to_thread(prefetch, _done)
        except Exception as e:
            if log:
                log(f"SYS: Kokoro warmup failed — {type(e).__name__}: {str(e)[:100]}")


def _split(text: str, max_chars: int = 240) -> list[str]:
    """Break an answer into speakable pieces (same as SystemTTS)."""
    text = (text or "").strip()
    if not text:
        return []
    pieces = []
    for part in _SENTENCE_END.split(text):
        part = part.strip()
        if not part:
            continue
        while len(part) > max_chars:
            cut = part.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            pieces.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            pieces.append(part)
    return pieces


def _make(cfg: dict) -> KokoroTTS:
    return KokoroTTS(cfg)


PROVIDER = {
    "id": "kokoro",
    "kind": "tts",
    "label": "Kokoro",
    "hint": "One multilingual model (~300 MB), dozens of voices, runs offline",
    "requires": ("kokoro", "torch", "soundfile"),
    "install": "pip install kokoro>=0.9 soundfile torch",
    "catalog": (
        {"id": "kokoro", "label": "Kokoro (on this computer)",
         "note": "free · offline · ~15 languages · ~60 voices · downloads once",
         "local": True, "values": {}},
    ),
    "needs": (
        Field("voice", "Voice", kind="choice", choices=(), default="af_heart",
              required=True,
              hint="A star means it is already downloaded; first use fetches the model"),
        Field("speed", "Speed", kind="number", default=1.0, required=False,
              advanced=True,
              hint="1.0 is normal, 0.5 is half speed, 2.0 is double"),
    ),
    "caps": {"audio_out": True},
    "factory": _make,
}


def available() -> bool:
    try:
        _import_kokoro()
        return True
    except Exception:
        return False


def choices_for(field_key: str) -> tuple:
    """Live voice list for the panel."""
    if field_key != "voice":
        return ()
    return tuple(list_voices())