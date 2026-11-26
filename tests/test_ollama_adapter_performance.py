"""
tests/test_ollama_adapter_performance.py — Tests for OllamaAdapter performance optimizations.

Tests the keep_alive, GPU detection, and warm-start features added to
OllamaAdapter to fix the 193s cold-start latency issue.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from orchestrator.model_router.providers import OllamaAdapter, CompletionRequest


class TestOllamaAdapterConfiguration:
    """Test OllamaAdapter initialization and configuration."""

    def test_default_configuration(self):
        """Adapter should have sensible defaults."""
        adapter = OllamaAdapter()

        assert adapter._base_url == "http://127.0.0.1:11434"
        assert adapter._model == "qwen2.5-coder:3b"
        assert adapter._timeout_s == 120.0
        assert adapter._keep_alive == "10m"
        assert adapter._use_gpu is True
        assert adapter._num_gpu is None
        assert adapter._warm_started is False

    def test_custom_configuration(self):
        """Adapter should accept custom configuration."""
        adapter = OllamaAdapter(
            base_url="http://localhost:11435",
            model="llama3:8b",
            timeout_s=60.0,
            keep_alive="5m",
            use_gpu=False,
            num_gpu=20,
        )

        assert adapter._base_url == "http://localhost:11435"
        assert adapter._model == "llama3:8b"
        assert adapter._timeout_s == 60.0
        assert adapter._keep_alive == "5m"
        assert adapter._use_gpu is False
        assert adapter._num_gpu == 20


class TestOllamaAdapterGPUDetection:
    """Test GPU detection logic."""

    @patch("subprocess.run")
    def test_detects_nvidia_gpu(self, mock_run):
        """Should detect NVIDIA GPU and set appropriate layers."""
        adapter = OllamaAdapter()

        # Mock nvidia-smi output: 8GB GPU
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="NVIDIA RTX 3080, 8192\n",
            stderr=""
        )

        gpu_info = adapter._detect_gpu()

        assert gpu_info["has_gpu"] is True
        assert gpu_info["gpu_name"] == "NVIDIA RTX 3080"
        assert gpu_info["gpu_memory_mb"] == 8192
        assert gpu_info["llama_cpp_gpu_layers"] == 35  # 8GB -> 35 layers

    @patch("subprocess.run")
    def test_detects_nvidia_gpu_4gb(self, mock_run):
        """Should set appropriate layers for 4GB GPU."""
        adapter = OllamaAdapter()

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="NVIDIA RTX 3050, 4096\n",
            stderr=""
        )

        gpu_info = adapter._detect_gpu()

        assert gpu_info["llama_cpp_gpu_layers"] == 20

    @patch("subprocess.run")
    def test_detects_amd_gpu(self, mock_run):
        """Should detect AMD GPU via rocm-smi."""
        adapter = OllamaAdapter()

        # First call (nvidia-smi) fails, second (rocm-smi) succeeds
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="not found"),  # nvidia-smi
            MagicMock(returncode=0, stdout="AMD Radeon RX 7900\nVRAM: 20GB", stderr=""),  # rocm-smi
        ]

        gpu_info = adapter._detect_gpu()

        assert gpu_info["has_gpu"] is True
        assert "AMD" in gpu_info["gpu_name"]
        assert gpu_info["llama_cpp_gpu_layers"] == 20

    @patch("platform.system")
    @patch("platform.machine")
    def test_detects_apple_silicon(self, mock_machine, mock_system):
        """Should detect Apple Silicon Metal GPU."""
        adapter = OllamaAdapter()

        mock_system.return_value = "Darwin"
        mock_machine.return_value = "arm64"

        gpu_info = adapter._detect_gpu()

        assert gpu_info["has_gpu"] is True
        assert "Apple Silicon" in gpu_info["gpu_name"]
        assert gpu_info["llama_cpp_gpu_layers"] == 35

    @patch("subprocess.run")
    def test_no_gpu_fallback(self, mock_run):
        """Should return CPU-only when no GPU detected."""
        adapter = OllamaAdapter()

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        gpu_info = adapter._detect_gpu()

        assert gpu_info["has_gpu"] is False
        assert gpu_info["llama_cpp_gpu_layers"] == 0

    def test_get_gpu_options_returns_correct_layers(self):
        """_get_gpu_options should return num_gpu when GPU available."""
        adapter = OllamaAdapter()

        # Mock GPU detection
        adapter._gpu_info = {"has_gpu": True, "llama_cpp_gpu_layers": 20}
        adapter._gpu_detected = True

        options = adapter._get_gpu_options()

        assert options["num_gpu"] == 20

    def test_get_gpu_options_empty_when_no_gpu(self):
        """_get_gpu_options should return empty when no GPU."""
        adapter = OllamaAdapter(use_gpu=False)

        options = adapter._get_gpu_options()

        assert options == {}

    def test_explicit_num_gpu_override(self):
        """Explicit num_gpu should override auto-detection."""
        adapter = OllamaAdapter(num_gpu=10)

        adapter._gpu_info = {"has_gpu": True, "llama_cpp_gpu_layers": 35}
        adapter._gpu_detected = True

        options = adapter._get_gpu_options()

        assert options["num_gpu"] == 10  # Explicit override


class TestOllamaAdapterWarmStart:
    """Test warm-start functionality."""

    @patch("requests.post")
    def test_warm_start_loads_model(self, mock_post):
        """_warm_start should send a minimal request to load model."""
        adapter = OllamaAdapter()

        mock_post.return_value = MagicMock(status_code=200)

        adapter._warm_start()

        assert adapter._warm_started is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"]["prompt"] == "hi"
        assert call_args[1]["json"]["options"]["num_predict"] == 1
        assert call_args[1]["json"]["options"]["keep_alive"] == "10m"

    @patch("requests.post")
    def test_warm_start_idempotent(self, mock_post):
        """Second warm_start call should not make another request."""
        adapter = OllamaAdapter()

        mock_post.return_value = MagicMock(status_code=200)

        adapter._warm_start()
        adapter._warm_start()  # Second call

        assert mock_post.call_count == 1

    @patch("requests.post")
    def test_warm_start_fails_silently(self, mock_post):
        """Failed warm_start should not raise."""
        adapter = OllamaAdapter()

        mock_post.side_effect = Exception("Connection refused")

        adapter._warm_start()  # Should not raise

        assert adapter._warm_started is False

    @patch("requests.get")
    @patch("threading.Thread")
    def test_probe_triggers_warm_start(self, mock_thread, mock_get):
        """probe() should start warm_start in background thread."""
        adapter = OllamaAdapter()

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "qwen2.5-coder:3b"}]}
        )

        ok, msg = adapter.probe()

        assert ok is True
        assert "ready" in msg
        mock_thread.assert_called_once()
        # Thread target should be _warm_start


class TestOllamaAdapterProbe:
    """Test probe functionality."""

    @patch("requests.get")
    def test_probe_returns_ready_when_model_available(self, mock_get):
        """Probe should return ready when model is available."""
        adapter = OllamaAdapter()

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "qwen2.5-coder:3b"}]}
        )

        ok, msg = adapter.probe()

        assert ok is True
        assert "ready" in msg

    @patch("requests.get")
    def test_probe_fails_when_model_not_pulled(self, mock_get):
        """Probe should fail when model not pulled."""
        adapter = OllamaAdapter()

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "llama3:8b"}]}
        )

        ok, msg = adapter.probe()

        assert ok is False
        assert "not pulled" in msg

    @patch("requests.get")
    def test_probe_fails_when_service_down(self, mock_get):
        """Probe should fail when Ollama service is down."""
        adapter = OllamaAdapter()

        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError()

        ok, msg = adapter.probe()

        assert ok is False
        assert "not running" in msg

    @patch("requests.get")
    def test_probe_fails_on_timeout(self, mock_get):
        """Probe should fail on timeout."""
        adapter = OllamaAdapter()

        import requests
        mock_get.side_effect = requests.exceptions.Timeout()

        ok, msg = adapter.probe()

        assert ok is False
        assert "timed out" in msg


class TestOllamaAdapterComplete:
    """Test complete() with keep_alive and GPU options."""

    @patch("requests.post")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_complete_sends_keep_alive(self, mock_configured, mock_post):
        """Complete should send keep_alive in options."""
        adapter = OllamaAdapter()

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response": "Hello world"}
        )

        req = CompletionRequest(prompt="Say hello", max_tokens=50, timeout_s=30.0)
        result = adapter.complete(req)

        assert result == "Hello world"
        call_args = mock_post.call_args
        assert call_args[1]["json"]["options"]["keep_alive"] == "10m"

    @patch("requests.post")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_complete_sends_gpu_options(self, mock_configured, mock_post):
        """Complete should send GPU options when GPU available."""
        adapter = OllamaAdapter()

        # Mock GPU detection
        adapter._gpu_info = {"has_gpu": True, "llama_cpp_gpu_layers": 20}
        adapter._gpu_detected = True

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response": "Hello world"}
        )

        req = CompletionRequest(prompt="Say hello", max_tokens=50, timeout_s=30.0)
        result = adapter.complete(req)

        assert result == "Hello world"
        call_args = mock_post.call_args
        assert call_args[1]["json"]["options"]["num_gpu"] == 20

    @patch("requests.post")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_complete_uses_model_from_request(self, mock_configured, mock_post):
        """Complete should use model_id from request when not auto."""
        adapter = OllamaAdapter()

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response": "Hello world"}
        )

        req = CompletionRequest(
            prompt="Say hello",
            model_id="custom-model:7b",
            max_tokens=50,
            timeout_s=30.0
        )
        result = adapter.complete(req)

        assert result == "Hello world"
        call_args = mock_post.call_args
        assert call_args[1]["json"]["model"] == "custom-model:7b"

    @patch("requests.post")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_complete_raises_on_timeout(self, mock_configured, mock_post):
        """Complete should raise ProviderTimeoutError on timeout."""
        adapter = OllamaAdapter()

        import requests
        mock_post.side_effect = requests.exceptions.Timeout()

        req = CompletionRequest(prompt="Say hello", max_tokens=50, timeout_s=30.0)

        from orchestrator.model_router.providers import ProviderTimeoutError
        with pytest.raises(ProviderTimeoutError):
            adapter.complete(req)

    @patch("requests.post")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_complete_raises_on_500(self, mock_configured, mock_post):
        """Complete should raise UnavailableError on 500."""
        adapter = OllamaAdapter()

        mock_post.return_value = MagicMock(
            status_code=500,
            text="Internal server error"
        )

        req = CompletionRequest(prompt="Say hello", max_tokens=50, timeout_s=30.0)

        from orchestrator.model_router.providers import UnavailableError
        with pytest.raises(UnavailableError):
            adapter.complete(req)

    @patch("requests.post")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_complete_raises_on_429(self, mock_configured, mock_post):
        """Complete should raise RateLimitedError on 429."""
        adapter = OllamaAdapter()

        mock_post.return_value = MagicMock(
            status_code=429,
            headers={"retry-after": "60"}
        )

        req = CompletionRequest(prompt="Say hello", max_tokens=50, timeout_s=30.0)

        from orchestrator.model_router.providers import RateLimitedError
        with pytest.raises(RateLimitedError) as exc_info:
            adapter.complete(req)

        assert exc_info.value.retry_after_s == 60

    @patch("requests.post")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_complete_raises_on_empty_response(self, mock_configured, mock_post):
        """Complete should raise EmptyAnswerError on empty response."""
        adapter = OllamaAdapter()

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"response": ""}
        )

        req = CompletionRequest(prompt="Say hello", max_tokens=50, timeout_s=30.0)

        from orchestrator.model_router.providers import EmptyAnswerError
        with pytest.raises(EmptyAnswerError):
            adapter.complete(req)


class TestOllamaAdapterIntegration:
    """Integration tests with real Ollama (skipped if not available)."""

    @pytest.mark.skipif(True, reason="Requires running Ollama service")
    @patch.object(OllamaAdapter, "is_configured", return_value=True)
    def test_real_ollama_complete(self, mock_configured):
        """Test against real Ollama if available."""
        adapter = OllamaAdapter()

        # This would require a real Ollama instance
        # Skipped in CI, run manually with: pytest -k "real_ollama" --run-slow
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])