"""
core/voice_pipeline.py — Local-First Voice Pipeline

Orchestrates the complete offline voice flow:
    Microphone (VAD) → Local STT (Whisper/Vosk) → Local LLM (ModelRouter) → Local TTS (SystemTTS/Kokoro)

Gemini Live API is an explicit opt-in mode only (JARVIS_GEMINI_LIVE=on or voice command).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from core.stt import WhisperSTT, VoskSTT
from core.tts import create_tts_player, TTSPlayer
from core.voice_offline import get_offline_recognizer, OfflineIntent
from orchestrator.model_router.router import ModelRouter, RouteOptions, ModelTask

log = logging.getLogger("jarvis.voice_pipeline")


@dataclass
class VoiceConfig:
    """Configuration for the voice pipeline."""
    stt_engine: str = "whisper"           # "whisper" | "vosk"
    whisper_model: str = "base"           # tiny, base, small, medium, large
    tts_engine: str = "system_tts"        # "system_tts" | "kokoro" | "edgetts" | "elevenlabs"
    tts_voice: str = ""
    tts_rate: int = 0
    language: str = "en"
    vad_threshold: float = 0.01           # VAD energy threshold
    silence_timeout_ms: int = 800         # End of utterance detection
    sample_rate: int = 16000              # STT sample rate
    use_local_llm: bool = True            # Force local LLM (ModelRouter with require_local=True)
    gemini_live_enabled: bool = False     # Explicit opt-in for Gemini Live streaming


class LocalVoicePipeline:
    """
    Local-first voice processing pipeline.

    Flow:
    1. Audio capture with VAD (Voice Activity Detection)
    2. Local STT transcription (Whisper or Vosk)
    3. Offline intent recognition (pattern matching for common commands)
    4. If intent not recognized → Local LLM via ModelRouter (require_local=True)
    5. Local TTS playback (SystemTTS or Kokoro)

    Gemini Live is NOT used unless explicitly enabled.
    """

    def __init__(self, config: Optional[VoiceConfig] = None, model_router: Optional[ModelRouter] = None):
        self.config = config or VoiceConfig()
        self.model_router = model_router
        self._stt = None
        self._tts_player: Optional[TTSPlayer] = None
        self._offline_recognizer = get_offline_recognizer()
        self._running = False
        self._audio_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._audio_queue: "asyncio.Queue[np.ndarray]" = asyncio.Queue(maxsize=50)
        self._callbacks: dict[str, list[Callable]] = {
            "on_transcript": [],
            "on_intent": [],
            "on_response": [],
            "on_error": [],
        }

        # VAD state
        self._vad_buffer: list[np.ndarray] = []
        self._silence_frames = 0
        self._speech_started = False
        self._frames_per_buffer = 512  # ~32ms at 16kHz
        self._silence_frame_limit = max(1, int(self.config.silence_timeout_ms * self.config.sample_rate / 1000 / self._frames_per_buffer))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def initialize(self) -> None:
        """Initialize STT, TTS, and ModelRouter. Blocking — call from background thread."""
        log.info("[VoicePipeline] Initializing local-first voice pipeline…")

        # STT
        self._init_stt()

        # TTS
        self._init_tts()

        # ModelRouter (if not injected)
        if self.model_router is None:
            self.model_router = ModelRouter()

        log.info("[VoicePipeline] Ready (local-first mode)")

    def _init_stt(self) -> None:
        if self.config.stt_engine == "vosk":
            model_path = os.environ.get("VOSK_MODEL_PATH")
            self._stt = VoskSTT(model_path=model_path, language=self.config.language)
        else:
            self._stt = WhisperSTT(model_name=self.config.whisper_model, language=self.config.language)

    def _init_tts(self) -> None:
        tts_config = {
            "tts_engine": self.config.tts_engine,
            "tts_voice": self.config.tts_voice,
            "tts_rate": self.config.tts_rate,
        }
        self._tts_player = create_tts_player(tts_config)

    def start(self) -> None:
        """Start the audio capture and processing loops."""
        if self._running:
            return
        self._running = True

        # Audio capture thread (runs sounddevice callback)
        self._audio_thread = threading.Thread(target=self._audio_capture_loop, daemon=True, name="voice-capture")
        self._audio_thread.start()

        # Processing thread (STT → Intent → LLM → TTS)
        self._process_thread = threading.Thread(target=self._processing_loop, daemon=True, name="voice-process")
        self._process_thread.start()

        log.info("[VoicePipeline] Started")

    def stop(self) -> None:
        """Stop the pipeline."""
        self._running = False
        if self._audio_thread:
            self._audio_thread.join(timeout=2.0)
        if self._process_thread:
            self._process_thread.join(timeout=2.0)
        if self._tts_player:
            self._tts_player.stop()
        log.info("[VoicePipeline] Stopped")

    # ── Audio Capture (VAD) ──────────────────────────────────────────────────

    def _audio_capture_loop(self) -> None:
        """Capture audio from microphone with VAD, push complete utterances to queue."""
        import sounddevice as sd

        def callback(indata, frames, time_info, status):
            if status:
                log.debug(f"[VoicePipeline] Audio status: {status}")
            if not self._running:
                return

            # Convert to mono float32
            audio = indata[:, 0].astype(np.float32) if indata.ndim > 1 else indata.astype(np.float32)

            # Simple energy-based VAD
            energy = np.sqrt(np.mean(audio**2) + 1e-12)

            if energy > self.config.vad_threshold:
                self._speech_started = True
                self._silence_frames = 0
                self._vad_buffer.append(audio.copy())
            elif self._speech_started:
                self._vad_buffer.append(audio.copy())
                self._silence_frames += 1
                if self._silence_frames >= self._silence_frame_limit:
                    # End of utterance
                    utterance = np.concatenate(self._vad_buffer)
                    self._vad_buffer.clear()
                    self._speech_started = False
                    self._silence_frames = 0
                    # Push to processing queue (non-blocking)
                    try:
                        self._audio_queue.put_nowait(utterance)
                    except asyncio.QueueFull:
                        log.warning("[VoicePipeline] Audio queue full, dropping utterance")
            # Else: silence before speech started — ignore

        try:
            with sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self._frames_per_buffer,
                callback=callback,
            ):
                while self._running:
                    time.sleep(0.1)
        except Exception as e:
            log.error(f"[VoicePipeline] Audio capture error: {e}")
            self._emit("on_error", f"Audio capture failed: {e}")

    # ── Processing Loop ────────────────────────────────────────────────────────

    def _processing_loop(self) -> None:
        """Process utterances: STT → Intent → LLM → TTS."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def process():
            while self._running:
                try:
                    utterance = await asyncio.wait_for(self._audio_queue.get(), timeout=0.5)
                    await self._process_utterance(utterance)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    log.error(f"[VoicePipeline] Processing error: {e}")
                    self._emit("on_error", str(e))

        try:
            loop.run_until_complete(process())
        finally:
            loop.close()

    async def _process_utterance(self, audio: np.ndarray) -> None:
        """Process one complete utterance through the pipeline."""
        t0 = time.monotonic()

        # 1. STT Transcription
        try:
            transcript = self._stt.transcribe(audio)
        except Exception as e:
            log.error(f"[VoicePipeline] STT failed: {e}")
            self._emit("on_error", f"Transcription failed: {e}")
            return

        if not transcript.strip():
            return

        log.info(f"[VoicePipeline] Transcript: {transcript}")
        self._emit("on_transcript", transcript)

        # 2. Offline Intent Recognition (fast path for common commands)
        intent = self._offline_recognizer.recognize(transcript)
        if intent.intent != "unknown" and intent.confidence >= 0.6:
            log.info(f"[VoicePipeline] Offline intent: {intent.intent} (confidence={intent.confidence:.2f})")
            self._emit("on_intent", intent)
            # Execute the action locally (handled by caller via callback)
            return

        # 3. Local LLM via ModelRouter (require_local=True by default)
        if self.config.use_local_llm and self.model_router:
            try:
                options = RouteOptions(
                    task=ModelTask.LIGHT_REASONING,
                    require_local=True,  # KEY: never leave local unless explicitly forced
                    timeout_s=30.0,
                )
                result = self.model_router.route(transcript, options)
                if result.ok:
                    response = result.text
                    log.info(f"[VoicePipeline] Local LLM response ({result.provider}): {response[:100]}")
                    self._emit("on_response", response)

                    # 4. TTS Playback
                    await self._speak(response)
                else:
                    log.warning(f"[VoicePipeline] Local LLM failed: {result.explanation}")
                    self._emit("on_error", f"Local LLM unavailable: {result.explanation}")
                    # Fallback: try offline response
                    await self._speak("I couldn't process that locally. Please try again.")
            except Exception as e:
                log.error(f"[VoicePipeline] ModelRouter error: {e}")
                self._emit("on_error", f"LLM error: {e}")
        else:
            # No local LLM available — give offline response
            await self._speak("I'm running in offline mode. Local language model is not available.")

        log.debug(f"[VoicePipeline] Total pipeline latency: {time.monotonic() - t0:.2f}s")

    async def _speak(self, text: str) -> None:
        """Speak text via TTS player (non-blocking)."""
        if not self._tts_player:
            return

        def _run():
            try:
                self._tts_player.speak(text)
            except Exception as e:
                log.error(f"[VoicePipeline] TTS error: {e}")

        await asyncio.to_thread(_run)

    # ── Callbacks ────────────────────────────────────────────────────────────

    def on(self, event: str, callback: Callable) -> None:
        """Register a callback for pipeline events."""
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    def _emit(self, event: str, *args) -> None:
        for cb in self._callbacks.get(event, []):
            try:
                cb(*args)
            except Exception as e:
                log.debug(f"[VoicePipeline] Callback error: {e}")

    # ── Runtime Control ──────────────────────────────────────────────────────

    def set_gemini_live_mode(self, enabled: bool) -> None:
        """Enable/disable Gemini Live streaming mode (explicit opt-in)."""
        self.config.gemini_live_enabled = enabled
        log.info(f"[VoicePipeline] Gemini Live mode: {'enabled' if enabled else 'disabled'}")

    def set_local_mode(self, enabled: bool) -> None:
        """Enable/disable local-first mode."""
        self.config.use_local_llm = enabled
        log.info(f"[VoicePipeline] Local-first mode: {'enabled' if enabled else 'disabled'}")


# ── Factory ───────────────────────────────────────────────────────────────────


def create_voice_pipeline(config: dict, model_router: Optional[ModelRouter] = None) -> LocalVoicePipeline:
    """Create voice pipeline from config dict."""
    voice_config = VoiceConfig(
        stt_engine=config.get("stt_engine", "whisper"),
        whisper_model=config.get("whisper_model", "base"),
        tts_engine=config.get("tts_engine", "system_tts"),
        tts_voice=config.get("tts_voice", ""),
        tts_rate=int(config.get("tts_rate", 0)),
        language=config.get("language", "en"),
        vad_threshold=float(config.get("vad_threshold", 0.01)),
        silence_timeout_ms=int(config.get("silence_timeout_ms", 800)),
        use_local_llm=config.get("use_local_llm", True),
        gemini_live_enabled=config.get("gemini_live_enabled", False),
    )
    return LocalVoicePipeline(voice_config, model_router)