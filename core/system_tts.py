"""
core/system_tts.py — OS-Native Text-to-Speech (fully offline, zero dependencies)

Uses built-in platform TTS engines:
- Windows: SAPI5 (Speech API) via COM
- macOS: `say` command
- Linux: `espeak-ng` (preferred) or `espeak` / `spd-say`

No API keys, no model downloads, no network calls. Always available.
"""

import platform
import subprocess
import threading
import sys
from typing import Optional


class SystemTTS:
    """
    OS-native text-to-speech wrapper.

    Automatically detects platform and uses the appropriate backend:
    - Windows: SAPI5 (Microsoft Speech API)
    - macOS: built-in `say` command
    - Linux: `espeak-ng` (preferred), `espeak`, or `spd-say`

    All backends are fully offline and require no configuration.
    """

    def __init__(self, voice: Optional[str] = None, rate: int = 0):
        """
        Initialize SystemTTS.

        Args:
            voice: Optional voice name/identifier (platform-specific)
            rate: Speech rate adjustment (platform-specific, 0 = default)
        """
        self._voice = voice
        self._rate = rate
        self._engine = self._detect_engine()
        self._lock = threading.Lock()

    def _detect_engine(self) -> str:
        """Detect the best available TTS engine for the current platform."""
        system = platform.system()

        if system == "Windows":
            # SAPI5 is always available on Windows
            return "sapi5"
        elif system == "Darwin":
            # `say` is always available on macOS
            return "say"
        elif system == "Linux":
            # Prefer espeak-ng, fall back to espeak, then spd-say
            for cmd in ("espeak-ng", "espeak", "spd-say"):
                if self._command_exists(cmd):
                    return cmd
            # Last resort: festival (rarely installed)
            if self._command_exists("festival"):
                return "festival"
            return "none"
        else:
            return "none"

    def _command_exists(self, cmd: str) -> bool:
        """Check if a command exists in PATH."""
        import shutil
        return shutil.which(cmd) is not None

    def speak(self, text: str, async_: bool = False) -> None:
        """
        Speak the given text.

        Args:
            text: Text to speak
            async_: If True, run in background (non-blocking)
        """
        if not text or not text.strip():
            return

        if self._engine == "none":
            print(f"[SystemTTS] No TTS engine available, skipping: {text[:50]}")
            return

        def _speak():
            try:
                if self._engine == "sapi5":
                    self._speak_sapi5(text)
                elif self._engine == "say":
                    self._speak_say(text)
                elif self._engine in ("espeak-ng", "espeak"):
                    self._speak_espeak(text)
                elif self._engine == "spd-say":
                    self._speak_spd_say(text)
                elif self._engine == "festival":
                    self._speak_festival(text)
            except Exception as e:
                print(f"[SystemTTS] Error: {e}")

        if async_:
            threading.Thread(target=_speak, daemon=True).start()
        else:
            _speak()

    def _speak_sapi5(self, text: str) -> None:
        """Speak using Windows SAPI5."""
        try:
            import comtypes.client
            import comtypes.gen.SpeechLib as SpeechLib

            # Create SAPI5 voice
            speaker = comtypes.client.CreateObject("SAPI.SpVoice")

            # Set voice if specified
            if self._voice:
                voices = speaker.GetVoices()
                for i in range(voices.Count):
                    voice = voices.Item(i)
                    desc = voice.GetDescription()
                    if self._voice.lower() in desc.lower():
                        speaker.Voice = voice
                        break

            # Set rate (-10 to 10, 0 = normal)
            if self._rate:
                speaker.Rate = max(-10, min(10, self._rate))

            speaker.Speak(text)
        except ImportError:
            # Fallback to PowerShell if comtypes not available
            self._speak_sapi5_powershell(text)

    def _speak_sapi5_powershell(self, text: str) -> None:
        """Fallback SAPI5 via PowerShell (no comtypes needed)."""
        # Escape single quotes for PowerShell
        escaped = text.replace("'", "''")
        ps_script = f"""
        Add-Type -AssemblyName System.Speech
        $speak = New-Object System.Speech.Synthesis.SpeechSynthesizer
        """
        if self._voice:
            ps_script += f"$speak.SelectVoice('{self._voice}')\n"
        if self._rate:
            ps_script += f"$speak.Rate = {max(-10, min(10, self._rate))}\n"
        ps_script += f"$speak.Speak('{escaped}')"

        subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            check=False,
            timeout=30.0,
        )

    def _speak_say(self, text: str) -> None:
        """Speak using macOS `say` command."""
        cmd = ["say"]
        if self._voice:
            cmd.extend(["-v", self._voice])
        if self._rate:
            # say rate is words per minute, default ~175
            cmd.extend(["-r", str(175 + self._rate * 10)])
        cmd.append(text)
        subprocess.run(cmd, capture_output=True, check=False, timeout=30.0)

    def _speak_espeak(self, text: str) -> None:
        """Speak using espeak or espeak-ng."""
        cmd = [self._engine]
        if self._voice:
            cmd.extend(["-v", self._voice])
        if self._rate:
            # espeak rate is words per minute, default ~175
            cmd.extend(["-s", str(175 + self._rate * 10)])
        cmd.append(text)
        subprocess.run(cmd, capture_output=True, check=False, timeout=30.0)

    def _speak_spd_say(self, text: str) -> None:
        """Speak using speech-dispatcher (spd-say)."""
        cmd = ["spd-say"]
        if self._voice:
            cmd.extend(["-v", self._voice])
        if self._rate:
            # spd-say rate is -100 to 100
            cmd.extend(["-r", str(self._rate)])
        cmd.extend(["-w", text])  # -w = wait until done
        subprocess.run(cmd, capture_output=True, check=False, timeout=30.0)

    def _speak_festival(self, text: str) -> None:
        """Speak using festival (fallback)."""
        # Festival uses a different interface - pipe text to it
        subprocess.run(
            ["festival", "--tts"],
            input=text.encode(),
            capture_output=True,
            check=False,
            timeout=30.0,
        )

    def set_voice(self, voice: str) -> None:
        """Set the voice to use."""
        self._voice = voice

    def set_rate(self, rate: int) -> None:
        """Set the speech rate."""
        self._rate = rate

    @property
    def engine(self) -> str:
        """Return the active engine name."""
        return self._engine

    def is_available(self) -> bool:
        """Check if any TTS engine is available."""
        return self._engine != "none"


def get_system_tts(voice: Optional[str] = None, rate: int = 0) -> SystemTTS:
    """Factory function to create a SystemTTS instance."""
    return SystemTTS(voice=voice, rate=rate)