"""
core/voice_wake_word.py — Wake Word Training & Detection
Custom wake word training and detection for voice activation.
"""

from __future__ import annotations

import json
import logging
import os
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class WakeWordModel:
    """Wake word model configuration."""
    name: str
    phrases: list[str]  # Training phrases
    threshold: float = 0.5
    sensitivity: float = 0.5
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    audio_samples: list[list[float]] = field(default_factory=list)  # Stored audio features
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectionResult:
    """Result of wake word detection."""
    detected: bool
    wake_word: str | None = None
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)
    audio_features: list[float] | None = None


class WakeWordTrainer:
    """
    Wake word trainer using template matching with MFCC features.
    For production, use a proper keyword spotting model (Porcupine, Snowboy, or custom CNN).
    """

    def __init__(self, model_dir: str = "models/wake_word"):
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.models: dict[str, WakeWordModel] = {}
        self._load_models()

        # Audio processing parameters
        self.sample_rate = 16000
        self.frame_size = 400  # 25ms at 16kHz
        self.hop_size = 160    # 10ms at 16kHz
        self.n_mfcc = 13
        self.n_fft = 512

    def _load_models(self) -> None:
        """Load wake word models from disk."""
        for file in self.model_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text())
                model = WakeWordModel(**data)
                self.models[model.name] = model
            except Exception as e:
                logger.warning(f"Failed to load wake word model from {file}: {e}")

    def _save_model(self, model: WakeWordModel) -> None:
        """Save wake word model to disk."""
        try:
            file = self.model_dir / f"{model.name}.json"
            data = {
                "name": model.name,
                "phrases": model.phrases,
                "threshold": model.threshold,
                "sensitivity": model.sensitivity,
                "created_at": model.created_at,
                "updated_at": model.updated_at,
                "audio_samples": model.audio_samples,
                "metadata": model.metadata
            }
            file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save wake word model {model.name}: {e}")

    def _compute_mfcc(self, audio: np.ndarray) -> np.ndarray:
        """Compute MFCC features from audio."""
        from scipy.fft import rfft, dct

        if len(audio) < self.frame_size:
            audio = np.pad(audio, (0, self.frame_size - len(audio)))

        # Pre-emphasis
        pre_emphasis = 0.97
        emphasized = np.append(audio[0], audio[1:] - pre_emphasis * audio[:-1])

        # Framing
        frames = []
        for i in range(0, len(emphasized) - self.frame_size, self.hop_size):
            frame = emphasized[i:i + self.frame_size]
            if len(frame) == self.frame_size:
                frames.append(frame)

        if not frames:
            return np.zeros((1, self.n_mfcc))

        frames = np.array(frames)
        frames *= np.hanning(self.frame_size)

        # FFT and power spectrum
        mag_frames = np.abs(rfft(frames, n=self.n_fft))
        pow_frames = (1.0 / self.n_fft) * (mag_frames ** 2)

        # Mel filterbank
        n_mels = 40
        low_freq = 0
        high_freq = self.sample_rate / 2

        mel_points = np.linspace(
            2595 * np.log10(1 + low_freq / 700),
            2595 * np.log10(1 + high_freq / 700),
            n_mels + 2
        )
        hz_points = 700 * (10 ** (mel_points / 2595) - 1)
        bin_points = np.floor((self.n_fft + 1) * hz_points / self.sample_rate).astype(int)

        fbank = np.zeros((n_mels, self.n_fft // 2 + 1))
        for m in range(1, n_mels + 1):
            f_m_minus = bin_points[m - 1]
            f_m = bin_points[m]
            f_m_plus = bin_points[m + 1]

            for k in range(f_m_minus, f_m):
                fbank[m - 1, k] = (k - bin_points[m - 1]) / (bin_points[m] - bin_points[m - 1])
            for k in range(f_m, f_m_plus):
                fbank[m - 1, k] = (bin_points[m + 1] - k) / (bin_points[m + 1] - bin_points[m])

        filter_banks = np.dot(pow_frames, fbank.T)
        filter_banks = np.where(filter_banks == 0, np.finfo(float).eps, filter_banks)
        filter_banks = 20 * np.log10(filter_banks)

        # DCT to get MFCCs
        mfcc = dct(filter_banks, type=2, axis=1, norm='ortho')[:, :self.n_mfcc]

        # Cepstral liftering
        n_frames, n_coeffs = mfcc.shape
        n = np.arange(n_coeffs)
        lift = 1 + (22 / 2) * np.sin(np.pi * n / 22)
        mfcc *= lift

        return mfcc

    def _normalize_features(self, features: np.ndarray) -> np.ndarray:
        """Normalize features to unit length."""
        norm = np.linalg.norm(features, axis=1, keepdims=True)
        norm[norm == 0] = 1
        return features / norm

    def add_training_sample(self, model_name: str, audio: np.ndarray) -> bool:
        """Add a training audio sample to a wake word model."""
        if model_name not in self.models:
            logger.error(f"Model {model_name} not found")
            return False

        model = self.models[model_name]
        mfcc = self._compute_mfcc(audio)
        # Average over time to get a single feature vector
        avg_features = np.mean(mfcc, axis=0).tolist()
        model.audio_samples.append(avg_features)
        model.updated_at = time.time()
        self._save_model(model)
        logger.info(f"Added training sample to {model_name} (total: {len(model.audio_samples)})")
        return True

    def train_model(self, name: str, phrases: list[str], audio_samples: list[np.ndarray],
                    threshold: float = 0.5, sensitivity: float = 0.5) -> WakeWordModel:
        """Train a new wake word model."""
        if not audio_samples:
            raise ValueError("At least one audio sample required")

        model = WakeWordModel(
            name=name,
            phrases=phrases,
            threshold=threshold,
            sensitivity=sensitivity
        )

        for audio in audio_samples:
            mfcc = self._compute_mfcc(audio)
            avg_features = np.mean(mfcc, axis=0).tolist()
            model.audio_samples.append(avg_features)

        self.models[name] = model
        self._save_model(model)
        logger.info(f"Trained wake word model: {name} with {len(audio_samples)} samples")
        return model

    def delete_model(self, name: str) -> bool:
        """Delete a wake word model."""
        if name in self.models:
            del self.models[name]
            file = self.model_dir / f"{name}.json"
            if file.exists():
                file.unlink()
            return True
        return False

    def list_models(self) -> list[dict[str, Any]]:
        """List all wake word models."""
        return [
            {
                "name": m.name,
                "phrases": m.phrases,
                "threshold": m.threshold,
                "sensitivity": m.sensitivity,
                "sample_count": len(m.audio_samples),
                "created_at": m.created_at,
                "updated_at": m.updated_at
            }
            for m in self.models.values()
        ]


class WakeWordDetector:
    """Real-time wake word detector."""

    def __init__(self, trainer: WakeWordTrainer):
        self.trainer = trainer
        self.active_models: dict[str, WakeWordModel] = {}
        self.callback: Callable[[DetectionResult], None] | None = None
        self.buffer: list[float] = []
        self.buffer_duration = 1.5  # seconds of audio to keep
        self.buffer_size = int(self.buffer_duration * self.trainer.sample_rate)

    def set_callback(self, callback: Callable[[DetectionResult], None]) -> None:
        """Set callback for detection events."""
        self.callback = callback

    def add_model(self, model_name: str) -> bool:
        """Activate a model for detection."""
        if model_name in self.trainer.models:
            self.active_models[model_name] = self.trainer.models[model_name]
            return True
        return False

    def remove_model(self, model_name: str) -> bool:
        """Deactivate a model."""
        if model_name in self.active_models:
            del self.active_models[model_name]
            return True
        return False

    def process_audio(self, audio_chunk: np.ndarray) -> list[DetectionResult]:
        """Process incoming audio chunk for wake word detection."""
        # Add to buffer
        self.buffer.extend(audio_chunk.tolist())
        if len(self.buffer) > self.buffer_size:
            self.buffer = self.buffer[-self.buffer_size:]

        # Only detect if we have enough audio
        if len(self.buffer) < self.trainer.frame_size:
            return []

        results = []
        audio_array = np.array(self.buffer, dtype=np.float32)

        for model_name, model in self.active_models.items():
            if not model.audio_samples:
                continue

            # Compute features for current buffer
            mfcc = self.trainer._compute_mfcc(audio_array)
            current_features = self.trainer._normalize_features(mfcc)

            # Compare with stored templates
            best_confidence = 0.0
            for template in model.audio_samples:
                template_arr = np.array(template).reshape(1, -1)
                template_norm = self.trainer._normalize_features(template_arr)

                # Compute cosine similarity
                similarities = np.dot(current_features, template_norm.T)
                max_sim = np.max(similarities)
                best_confidence = max(best_confidence, max_sim)

            # Apply threshold and sensitivity
            adjusted_threshold = model.threshold * (2.0 - model.sensitivity)
            detected = best_confidence >= adjusted_threshold

            if detected:
                result = DetectionResult(
                    detected=True,
                    wake_word=model_name,
                    confidence=float(best_confidence),
                    audio_features=current_features[-1].tolist()  # Last frame
                )
                results.append(result)

                if self.callback:
                    self.callback(result)

        return results


# Global instances
_trainer: WakeWordTrainer | None = None
_detector: WakeWordDetector | None = None


def get_wake_word_trainer() -> WakeWordTrainer:
    """Get or create the wake word trainer singleton."""
    global _trainer
    if _trainer is None:
        _trainer = WakeWordTrainer()
    return _trainer


def get_wake_word_detector() -> WakeWordDetector:
    """Get or create the wake word detector singleton."""
    global _detector
    if _detector is None:
        _detector = WakeWordDetector(get_wake_word_trainer())
    return _detector