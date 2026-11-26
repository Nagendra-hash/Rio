"""
core/voice_auth.py — Voice Authentication
Voiceprint matching and speaker verification for secure voice commands.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class VoicePrint:
    """A stored voiceprint for a user."""
    user_id: str
    name: str
    embeddings: list[list[float]]  # Multiple enrollment samples
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthResult:
    """Result of voice authentication."""
    authenticated: bool
    user_id: str | None = None
    confidence: float = 0.0
    method: str = "none"  # "voiceprint", "pin", "none"
    error: str | None = None


class VoiceAuthenticator:
    """Handles voice authentication using speaker embeddings."""

    def __init__(self, storage_dir: str = "memory/voice_auth"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.voiceprints: dict[str, VoicePrint] = {}
        self._load_voiceprints()

        # Threshold for voiceprint matching (cosine similarity)
        self.match_threshold = 0.75

        # Number of enrollment samples required
        self.min_enrollment_samples = 3

    def _load_voiceprints(self) -> None:
        """Load voiceprints from disk."""
        for file in self.storage_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text())
                vp = VoicePrint(
                    user_id=data["user_id"],
                    name=data["name"],
                    embeddings=data["embeddings"],
                    created_at=data.get("created_at", time.time()),
                    updated_at=data.get("updated_at", time.time()),
                    metadata=data.get("metadata", {})
                )
                self.voiceprints[vp.user_id] = vp
            except Exception as e:
                logger.warning(f"Failed to load voiceprint from {file}: {e}")

    def _save_voiceprint(self, vp: VoicePrint) -> None:
        """Save voiceprint to disk."""
        try:
            file = self.storage_dir / f"{vp.user_id}.json"
            file.write_text(json.dumps({
                "user_id": vp.user_id,
                "name": vp.name,
                "embeddings": vp.embeddings,
                "created_at": vp.created_at,
                "updated_at": vp.updated_at,
                "metadata": vp.metadata
            }, indent=2))
        except Exception as e:
            logger.error(f"Failed to save voiceprint {vp.user_id}: {e}")

    @staticmethod
    def _compute_embedding(audio_data: np.ndarray, sample_rate: int = 16000) -> list[float]:
        """
        Compute speaker embedding from audio.
        This is a placeholder - in production, use a proper speaker embedding model
        like ECAPA-TDNN, x-vector, or a cloud API (Google Speech, Azure Speaker Recognition).
        """
        # Placeholder: compute simple spectral features as "embedding"
        # Real implementation would use a pre-trained speaker embedding model
        if len(audio_data) == 0:
            return [0.0] * 192  # Standard embedding dimension

        # Simple MFCC-like features as placeholder
        from scipy.fft import rfft
        frame_size = 400
        hop_size = 160
        n_fft = 512

        features = []
        for i in range(0, len(audio_data) - frame_size, hop_size):
            frame = audio_data[i:i + frame_size]
            if len(frame) < frame_size:
                break
            # Apply window
            windowed = frame * np.hanning(frame_size)
            # FFT
            spectrum = np.abs(rfft(windowed, n=n_fft))
            # Log mel filterbank (simplified)
            log_spec = np.log(spectrum + 1e-10)
            features.append(log_spec[:40])  # 40 mel bins

        if not features:
            return [0.0] * 192

        # Average over time and pad/truncate to 192
        avg_features = np.mean(features, axis=0)
        embedding = np.pad(avg_features, (0, max(0, 192 - len(avg_features))))[:192]

        # Normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        return embedding.tolist()

    def enroll(self, user_id: str, name: str, audio_samples: list[np.ndarray],
               sample_rate: int = 16000) -> AuthResult:
        """
        Enroll a new user with voice samples.

        Args:
            user_id: Unique identifier for the user
            name: Display name
            audio_samples: List of audio arrays (at least min_enrollment_samples)
            sample_rate: Audio sample rate

        Returns:
            AuthResult with success status
        """
        if len(audio_samples) < self.min_enrollment_samples:
            return AuthResult(
                authenticated=False,
                error=f"Need at least {self.min_enrollment_samples} audio samples for enrollment"
            )

        embeddings = []
        for audio in audio_samples:
            emb = self._compute_embedding(audio, sample_rate)
            embeddings.append(emb)

        # Check consistency of enrollment samples
        if len(embeddings) >= 2:
            similarities = []
            for i in range(len(embeddings)):
                for j in range(i + 1, len(embeddings)):
                    sim = np.dot(embeddings[i], embeddings[j])
                    similarities.append(sim)

            avg_sim = np.mean(similarities)
            if avg_sim < 0.6:
                logger.warning(f"Low consistency in enrollment samples for {user_id}: {avg_sim:.2f}")

        # Create or update voiceprint
        if user_id in self.voiceprints:
            vp = self.voiceprints[user_id]
            vp.embeddings.extend(embeddings)
            vp.updated_at = time.time()
        else:
            vp = VoicePrint(user_id=user_id, name=name, embeddings=embeddings)
            self.voiceprints[user_id] = vp

        self._save_voiceprint(vp)

        logger.info(f"Enrolled voiceprint for {name} ({user_id}) with {len(embeddings)} samples")
        return AuthResult(authenticated=True, user_id=user_id, confidence=1.0, method="enrollment")

    def verify(self, user_id: str, audio: np.ndarray, sample_rate: int = 16000) -> AuthResult:
        """
        Verify a speaker against enrolled voiceprint.

        Args:
            user_id: User to verify against
            audio: Audio array to verify
            sample_rate: Audio sample rate

        Returns:
            AuthResult with authentication status
        """
        if user_id not in self.voiceprints:
            return AuthResult(
                authenticated=False,
                user_id=user_id,
                error=f"No voiceprint enrolled for user {user_id}"
            )

        vp = self.voiceprints[user_id]
        test_emb = self._compute_embedding(audio, sample_rate)

        # Compute similarity against all enrollment embeddings
        similarities = []
        for enrolled_emb in vp.embeddings:
            sim = np.dot(test_emb, enrolled_emb)
            similarities.append(sim)

        max_sim = max(similarities)
        avg_sim = np.mean(similarities)

        # Use max similarity for decision (more robust to single bad enrollment)
        authenticated = max_sim >= self.match_threshold

        return AuthResult(
            authenticated=authenticated,
            user_id=user_id if authenticated else None,
            confidence=float(max_sim),
            method="voiceprint"
        )

    def identify(self, audio: np.ndarray, sample_rate: int = 16000) -> AuthResult:
        """
        Identify speaker from all enrolled voiceprints (1:N matching).

        Args:
            audio: Audio array to identify
            sample_rate: Audio sample rate

        Returns:
            AuthResult with best matching user
        """
        if not self.voiceprints:
            return AuthResult(authenticated=False, error="No voiceprints enrolled")

        test_emb = self._compute_embedding(audio, sample_rate)

        best_user = None
        best_sim = -1.0

        for user_id, vp in self.voiceprints.items():
            for enrolled_emb in vp.embeddings:
                sim = np.dot(test_emb, enrolled_emb)
                if sim > best_sim:
                    best_sim = sim
                    best_user = user_id

        authenticated = best_sim >= self.match_threshold

        return AuthResult(
            authenticated=authenticated,
            user_id=best_user if authenticated else None,
            confidence=float(best_sim) if best_user else 0.0,
            method="voiceprint"
        )

    def delete_voiceprint(self, user_id: str) -> bool:
        """Delete a voiceprint."""
        if user_id in self.voiceprints:
            del self.voiceprints[user_id]
            file = self.storage_dir / f"{user_id}.json"
            if file.exists():
                file.unlink()
            return True
        return False

    def list_users(self) -> list[dict[str, Any]]:
        """List all enrolled users."""
        return [
            {
                "user_id": vp.user_id,
                "name": vp.name,
                "sample_count": len(vp.embeddings),
                "created_at": vp.created_at,
                "updated_at": vp.updated_at
            }
            for vp in self.voiceprints.values()
        ]


# Singleton instance
_authenticator: VoiceAuthenticator | None = None


def get_voice_authenticator() -> VoiceAuthenticator:
    """Get or create the voice authenticator singleton."""
    global _authenticator
    if _authenticator is None:
        _authenticator = VoiceAuthenticator()
    return _authenticator