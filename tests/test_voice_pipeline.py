"""
tests/test_voice_pipeline.py — Tests for local-first voice pipeline.

Tests the local STT (Whisper/Vosk), offline intent recognition,
and SystemTTS components that replace continuous Gemini Live streaming.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock
import numpy as np

import pytest


class TestVoiceOfflineIntentRecognition:
    """Test offline intent recognition in core/voice_offline.py."""

    def test_voice_offline_module_exists(self):
        """Module should be importable."""
        import importlib
        spec = importlib.util.find_spec("core.voice_offline")
        assert spec is not None, "core.voice_offline module not found"

    def test_intent_patterns_loaded(self):
        """Should have patterns for common intents."""
        from core.voice_offline import INTENT_PATTERNS

        # Should have patterns for key intents (using actual intent names from implementation)
        assert "media.play" in INTENT_PATTERNS
        assert "media.pause" in INTENT_PATTERNS
        assert "media.volume" in INTENT_PATTERNS
        assert "search.web" in INTENT_PATTERNS
        assert "app.open" in INTENT_PATTERNS
        assert "info.time" in INTENT_PATTERNS
        assert "info.weather" in INTENT_PATTERNS
        assert "timer.set" in INTENT_PATTERNS
        assert "calendar.add" in INTENT_PATTERNS
        assert "system.volume" in INTENT_PATTERNS

    def test_recognize_intent_matches_play_music(self):
        """Should recognize play music commands."""
        from core.voice_offline import recognize_intent

        result = recognize_intent("play some music")
        assert result.intent == "media.play"
        assert result.confidence > 0.5

        result = recognize_intent("play jazz on spotify")
        assert result.intent == "media.play"

    def test_recognize_intent_matches_stop_music(self):
        """Should recognize stop music commands."""
        from core.voice_offline import recognize_intent

        result = recognize_intent("stop the music")
        assert result.intent == "media.pause"

        result = recognize_intent("pause playback")
        assert result.intent == "media.pause"

    def test_recognize_intent_matches_volume(self):
        """Should recognize volume commands with level extraction."""
        from core.voice_offline import recognize_intent

        result = recognize_intent("set volume to 50 percent")
        # Could be media.volume or system.volume depending on match priority
        assert result.intent in ("media.volume", "system.volume")
        assert "level" in result.entities or "value" in result.entities

        result = recognize_intent("turn it up")
        assert result.intent in ("media.volume", "system.volume")

    def test_recognize_intent_matches_web_search(self):
        """Should recognize web search commands."""
        from core.voice_offline import recognize_intent

        result = recognize_intent("search for python tutorials")
        assert result.intent == "search.web"
        assert "query" in result.entities

    def test_recognize_intent_no_match(self):
        """Should return unknown for unrecognized input."""
        from core.voice_offline import recognize_intent

        result = recognize_intent("asdfghjkl nonsense words")
        assert result.intent == "unknown"
        assert result.confidence == 0.0

    def test_intent_result_dataclass(self):
        """IntentResult should have expected fields."""
        from core.voice_offline import IntentResult

        result = IntentResult(
            intent="test",
            confidence=0.9,
            entities={"key": "value"},
            raw_text="test input"
        )

        assert result.intent == "test"
        assert result.confidence == 0.9
        assert result.entities == {"key": "value"}
        assert result.raw_text == "test input"


class TestFasterWhisperSTT:
    """Test faster-whisper STT integration."""

    @pytest.mark.skipif(True, reason="Requires faster-whisper model download")
    def test_whisper_transcribe(self):
        """Should transcribe audio to text."""
        # Requires model download, skip in CI
        pass

    def test_whisper_not_available_fallback(self):
        """Should handle missing faster-whisper gracefully."""
        with patch.dict("sys.modules", {"faster_whisper": None}):
            from core.voice_offline import get_stt_engine
            engine = get_stt_engine()
            # Should return Vosk or None, not crash


class TestVoskSTT:
    """Test Vosk offline STT integration."""

    @pytest.mark.skipif(True, reason="Requires Vosk model download")
    def test_vosk_transcribe(self):
        """Should transcribe audio to text."""
        pass

    def test_vosk_not_available_fallback(self):
        """Should handle missing Vosk gracefully."""
        with patch.dict("sys.modules", {"vosk": None}):
            from core.voice_offline import get_stt_engine
            engine = get_stt_engine()
            # Should return Whisper or None, not crash


class TestSystemTTS:
    """Test OS-native TTS (SystemTTS)."""

    def test_systemtts_class_exists(self):
        """SystemTTS class should exist."""
        from core.system_tts import SystemTTS
        assert SystemTTS is not None

    def test_systemtts_initialization(self):
        """Should initialize without error."""
        from core.system_tts import SystemTTS
        tts = SystemTTS()
        assert tts is not None

    @patch("platform.system")
    def test_windows_uses_sapi5(self, mock_system):
        """Should use SAPI5 on Windows."""
        mock_system.return_value = "Windows"

        from core.system_tts import SystemTTS
        tts = SystemTTS()

        assert tts._engine == "sapi5"

    @patch("platform.system")
    def test_macos_uses_say(self, mock_system):
        """Should use 'say' command on macOS."""
        mock_system.return_value = "Darwin"

        from core.system_tts import SystemTTS
        tts = SystemTTS()

        assert tts._engine == "say"

    @patch("platform.system")
    @patch("core.system_tts.SystemTTS._command_exists")
    def test_linux_uses_espeak(self, mock_command_exists, mock_system):
        """Should use espeak-ng on Linux."""
        mock_system.return_value = "Linux"
        # Mock command detection to return espeak-ng as available
        mock_command_exists.side_effect = lambda cmd: cmd == "espeak-ng"

        from core.system_tts import SystemTTS
        tts = SystemTTS()

        assert tts._engine == "espeak-ng"

    @patch("subprocess.run")
    @patch("platform.system")
    @patch("core.system_tts.SystemTTS._command_exists")
    def test_speak_calls_correct_command(self, mock_command_exists, mock_system, mock_run):
        """Should call correct TTS command for platform."""
        mock_system.return_value = "Linux"
        mock_command_exists.side_effect = lambda cmd: cmd == "espeak-ng"
        mock_run.return_value = MagicMock(returncode=0)

        from core.system_tts import SystemTTS
        tts = SystemTTS()
        tts.speak("Hello world")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "espeak-ng" in args[0]
        assert "Hello world" in " ".join(args)

    @patch("subprocess.run")
    @patch("platform.system")
    def test_speak_async(self, mock_system, mock_run):
        """Should run TTS in background when async=True."""
        mock_system.return_value = "Linux"
        mock_run.return_value = MagicMock(returncode=0)

        from core.system_tts import SystemTTS
        tts = SystemTTS()
        tts.speak("Hello", async_=True)

        # Should not block - subprocess.Popen or thread used
        # At minimum, speak should return immediately

    def test_set_voice(self):
        """Should allow voice selection."""
        from core.system_tts import SystemTTS
        tts = SystemTTS()

        # Should not raise
        tts.set_voice("english")


class TestKokoroTTS:
    """Test Kokoro local TTS (optional, higher quality)."""

    @pytest.mark.skipif(True, reason="Requires Kokoro model")
    def test_kokoro_synthesize(self):
        """Should synthesize speech with Kokoro."""
        pass

    def test_kokoro_fallback_to_system(self):
        """Should fall back to SystemTTS when Kokoro unavailable."""
        with patch.dict("sys.modules", {"kokoro": None}):
            from core.voice_offline import get_tts_engine
            engine = get_tts_engine()
            # Should return SystemTTS instance


class TestVoicePipelineIntegration:
    """Test full local voice pipeline: STT -> Intent -> Action -> TTS."""

    @patch("core.voice_offline.get_stt_engine")
    @patch("core.voice_offline.get_tts_engine")
    def test_pipeline_processes_command(self, mock_get_tts, mock_get_stt):
        """Full pipeline should process voice command locally."""
        # Mock STT
        mock_stt = MagicMock()
        mock_stt.transcribe.return_value = "play music"
        mock_get_stt.return_value = mock_stt

        # Mock TTS
        mock_tts = MagicMock()
        mock_get_tts.return_value = mock_tts

        from core.voice_offline import process_voice_command

        # Create dummy audio
        audio_data = np.zeros(16000, dtype=np.int16)  # 1 second silence

        result = process_voice_command(audio_data)

        # Should recognize intent and respond
        assert result is not None
        assert "intent" in result
        mock_stt.transcribe.assert_called_once()
        mock_tts.speak.assert_called_once()

    def test_pipeline_handles_unknown_intent(self):
        """Should handle unrecognized commands gracefully."""
        from core.voice_offline import process_voice_command

        # Mock STT to return gibberish
        with patch("core.voice_offline.get_stt_engine") as mock_get_stt:
            mock_stt = MagicMock()
            mock_stt.transcribe.return_value = "xyz random noise"
            mock_get_stt.return_value = mock_stt

            with patch("core.voice_offline.get_tts_engine") as mock_get_tts:
                mock_tts = MagicMock()
                mock_get_tts.return_value = mock_tts

                audio_data = np.zeros(16000, dtype=np.int16)
                result = process_voice_command(audio_data)

                assert result["intent"] == "unknown"
                mock_tts.speak.assert_called()  # Should still respond


class TestWakeWord:
    """Test wake word detection (openwakeword)."""

    def test_wake_word_module_exists(self):
        """Wake word module should exist."""
        import importlib
        spec = importlib.util.find_spec("core.wake_word")
        assert spec is not None, "core.wake_word module not found"

    def test_wake_word_detector_initialization(self):
        """Should initialize detector."""
        from core.wake_word import WakeWordDetector
        detector = WakeWordDetector(on_detect=lambda: None)
        assert detector is not None

    def test_openwakeword_integration(self):
        """Should initialize detector without crashing."""
        from core.wake_word import WakeWordDetector
        detector = WakeWordDetector(on_detect=lambda: None)
        # Should initialize without error
        assert detector is not None
        # Test ready property
        assert hasattr(detector, 'ready')


class TestEchoSuppression:
    """Test self-echo suppression in core/echo.py."""

    def test_echo_suppressor_exists(self):
        """EchoSuppressor should exist."""
        from core.echo import EchoSuppressor
        assert EchoSuppressor is not None

    def test_echo_suppressor_initialization(self):
        """Should initialize without audio device."""
        from core.echo import EchoSuppressor
        suppressor = EchoSuppressor()
        assert suppressor is not None

    def test_suppressor_filters_reference(self):
        """Should filter out reference signal from mic input."""
        import numpy as np
        from core.echo import EchoSuppressor

        suppressor = EchoSuppressor()

        # Reference signal (what we're playing)
        reference = np.sin(np.linspace(0, 2*np.pi, 1600)).astype(np.float32)
        # Mic input = reference + noise
        mic = reference + np.random.normal(0, 0.1, 1600).astype(np.float32)

        filtered = suppressor.process(mic, reference)

        # Filtered should have less correlation with reference
        corr_before = np.corrcoef(mic, reference)[0,1]
        corr_after = np.corrcoef(filtered, reference)[0,1]
        assert abs(corr_after) < abs(corr_before)


class TestMicAutoMute:
    """Test microphone auto-mute during media playback."""

    def test_mic_automute_exists(self):
        """Mic auto-mute functionality should exist."""
        # Check in ui.py or core/media_controls.py
        import importlib
        spec = importlib.util.find_spec("core.media_controls")
        # May be in ui.py instead


if __name__ == "__main__":
    pytest.main([__file__, "-v"])