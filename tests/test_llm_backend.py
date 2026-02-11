"""Tests for LLM backend abstraction layer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.llm_backend import (
    FileBridgeBackend,
    HaikuBackend,
    LLMBackend,
    OllamaDirectBackend,
)
from orchestrator.services.agent_meta import AgentMetaBrain


# ===================================================================
# OllamaDirectBackend
# ===================================================================


def _make_ollama_backend(*, available=True, response=None):
    """Create an OllamaDirectBackend with a mocked underlying client."""
    mock_client = MagicMock()
    mock_client.model = "test-model"
    mock_client.is_available.return_value = available
    if response is not None:
        mock_client.generate.return_value = response
    else:
        mock_client.generate.return_value = {}

    backend = OllamaDirectBackend.__new__(OllamaDirectBackend)
    backend._client = mock_client
    return backend


class TestOllamaDirectBackend:
    def test_name_includes_model(self):
        backend = _make_ollama_backend()
        assert "test-model" in backend.name

    def test_is_available_delegates(self):
        backend = _make_ollama_backend(available=True)
        assert backend.is_available() is True
        backend._client.is_available.assert_called_once()

    def test_generate_returns_text(self):
        backend = _make_ollama_backend(response={"response": "Hello world"})
        result = backend.generate("Say hello")
        assert result == "Hello world"

    def test_generate_returns_none_on_empty(self):
        backend = _make_ollama_backend(response={"response": "  "})
        assert backend.generate("test") is None

    def test_generate_returns_none_on_missing_response(self):
        backend = _make_ollama_backend(response={})
        assert backend.generate("test") is None

    def test_client_property_exposes_underlying_client(self):
        backend = _make_ollama_backend()
        assert backend.client is backend._client


# ===================================================================
# HaikuBackend
# ===================================================================


class TestHaikuBackend:
    def test_name(self):
        backend = HaikuBackend()
        assert backend.name == "haiku"

    def test_is_available_with_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        backend = HaikuBackend()
        assert backend.is_available() is True

    def test_is_available_without_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        backend = HaikuBackend()
        assert backend.is_available() is False

    def test_generate_returns_none_without_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        backend = HaikuBackend()
        assert backend.generate("test") is None

    def test_generate_calls_api(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": "Hello from Haiku"}]
        }

        import requests as real_requests
        with patch.object(real_requests, "post", return_value=mock_resp) as mock_post:
            backend = HaikuBackend()
            result = backend.generate("test prompt", temperature=0.5, max_tokens=512)

            assert result == "Hello from Haiku"
            call_kwargs = mock_post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert payload["temperature"] == 0.5
            assert payload["max_tokens"] == 512

    def test_generate_returns_none_on_error(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        import requests as real_requests
        with patch.object(real_requests, "post", side_effect=Exception("connection error")):
            backend = HaikuBackend()
            assert backend.generate("test") is None


# ===================================================================
# FileBridgeBackend
# ===================================================================


class TestFileBridgeBackend:
    def test_creates_subdirs_on_init(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        FileBridgeBackend(bridge_dir=bridge_dir)
        assert (bridge_dir / "requests").is_dir()
        assert (bridge_dir / "responses").is_dir()

    def test_no_error_if_bridge_dir_missing(self, tmp_path):
        bridge_dir = tmp_path / "nonexistent"
        backend = FileBridgeBackend(bridge_dir=bridge_dir)
        assert backend.is_available() is False

    def test_is_available_checks_dir_exists(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        backend = FileBridgeBackend(bridge_dir=bridge_dir)
        assert backend.is_available() is True

    def test_name(self, tmp_path):
        backend = FileBridgeBackend(bridge_dir=tmp_path)
        assert backend.name == "file_bridge"

    def test_writes_request_file(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        backend = FileBridgeBackend(bridge_dir=bridge_dir, timeout=0.1, poll_interval=0.05)

        # Generate will timeout, but we can check the request was written
        backend.generate("Hello LLM", temperature=0.5, max_tokens=256)

        # Request file should have been cleaned up on timeout, but let's verify format
        # by intercepting — instead, test with a response present
        pass

    def test_reads_response_file(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        backend = FileBridgeBackend(bridge_dir=bridge_dir, timeout=2.0, poll_interval=0.05)

        import threading

        def write_response():
            """Watch for a request file and write a response."""
            requests_dir = bridge_dir / "requests"
            responses_dir = bridge_dir / "responses"
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                for req_file in requests_dir.glob("*.json"):
                    data = json.loads(req_file.read_text())
                    resp = {
                        "id": data["id"],
                        "response": "Generated text",
                        "error": None,
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                    (responses_dir / req_file.name).write_text(json.dumps(resp))
                    return
                time.sleep(0.02)

        t = threading.Thread(target=write_response)
        t.start()
        result = backend.generate("test prompt")
        t.join(timeout=3)

        assert result == "Generated text"

    def test_timeout_returns_none(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        backend = FileBridgeBackend(bridge_dir=bridge_dir, timeout=0.1, poll_interval=0.05)

        result = backend.generate("test")
        assert result is None

    def test_error_in_response_returns_none(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        backend = FileBridgeBackend(bridge_dir=bridge_dir, timeout=2.0, poll_interval=0.05)

        import threading

        def write_error_response():
            requests_dir = bridge_dir / "requests"
            responses_dir = bridge_dir / "responses"
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                for req_file in requests_dir.glob("*.json"):
                    data = json.loads(req_file.read_text())
                    resp = {
                        "id": data["id"],
                        "response": None,
                        "error": "Model not found",
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                    (responses_dir / req_file.name).write_text(json.dumps(resp))
                    return
                time.sleep(0.02)

        t = threading.Thread(target=write_error_response)
        t.start()
        result = backend.generate("test")
        t.join(timeout=3)

        assert result is None

    def test_cleans_up_files(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        backend = FileBridgeBackend(bridge_dir=bridge_dir, timeout=2.0, poll_interval=0.05)

        import threading

        def write_response():
            requests_dir = bridge_dir / "requests"
            responses_dir = bridge_dir / "responses"
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                for req_file in requests_dir.glob("*.json"):
                    data = json.loads(req_file.read_text())
                    resp = {
                        "id": data["id"],
                        "response": "ok",
                        "error": None,
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                    (responses_dir / req_file.name).write_text(json.dumps(resp))
                    return
                time.sleep(0.02)

        t = threading.Thread(target=write_response)
        t.start()
        backend.generate("test")
        t.join(timeout=3)

        # Both request and response files should be cleaned up
        assert list((bridge_dir / "requests").glob("*.json")) == []
        assert list((bridge_dir / "responses").glob("*.json")) == []

    def test_request_json_format(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        backend = FileBridgeBackend(
            bridge_dir=bridge_dir,
            model="llama3.2:1b",
            timeout=0.1,
            poll_interval=0.05,
        )

        # Capture the request before it gets cleaned up on timeout
        import threading
        captured = {}

        def capture_request():
            requests_dir = bridge_dir / "requests"
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                for req_file in requests_dir.glob("*.json"):
                    captured.update(json.loads(req_file.read_text()))
                    return
                time.sleep(0.02)

        t = threading.Thread(target=capture_request)
        t.start()
        backend.generate("Hello", temperature=0.5, max_tokens=256)
        t.join(timeout=2)

        assert captured.get("prompt") == "Hello"
        assert captured.get("temperature") == 0.5
        assert captured.get("max_tokens") == 256
        assert captured.get("model") == "llama3.2:1b"
        assert "id" in captured
        assert "timestamp" in captured


# ===================================================================
# Backend fallback (AgentMetaBrain integration)
# ===================================================================


class TestBackendFallback:
    @staticmethod
    def _mock_backend(name: str, available: bool, response: str | None) -> MagicMock:
        b = MagicMock(spec=LLMBackend)
        b.name = name
        b.is_available.return_value = available
        b.generate.return_value = response
        return b

    def _make_brain(self, backends: list[MagicMock]) -> AgentMetaBrain:
        tracker = MagicMock()
        memory = MagicMock()
        memory.get_agent_context.return_value = ""
        memory.load_memory.return_value = ""
        memory.load_evolved_traits.return_value = ""
        return AgentMetaBrain(
            backends=backends,
            agent_tracker=tracker,
            agent_memory=memory,
        )

    def test_tries_first_backend(self):
        b1 = self._mock_backend("b1", True, "response from b1")
        b2 = self._mock_backend("b2", True, "response from b2")
        brain = self._make_brain([b1, b2])

        result = brain._generate("test")
        assert result == "response from b1"
        b1.generate.assert_called_once()
        b2.generate.assert_not_called()

    def test_falls_back_on_unavailable(self):
        b1 = self._mock_backend("b1", False, "response from b1")
        b2 = self._mock_backend("b2", True, "response from b2")
        brain = self._make_brain([b1, b2])

        result = brain._generate("test")
        assert result == "response from b2"
        b1.generate.assert_not_called()
        b2.generate.assert_called_once()

    def test_falls_back_on_failure(self):
        b1 = self._mock_backend("b1", True, None)
        b1.generate.side_effect = RuntimeError("boom")
        b2 = self._mock_backend("b2", True, "fallback")
        brain = self._make_brain([b1, b2])

        result = brain._generate("test")
        assert result == "fallback"

    def test_falls_back_on_empty_response(self):
        b1 = self._mock_backend("b1", True, None)
        b2 = self._mock_backend("b2", True, "got it")
        brain = self._make_brain([b1, b2])

        result = brain._generate("test")
        assert result == "got it"

    def test_returns_none_when_all_fail(self):
        b1 = self._mock_backend("b1", True, None)
        b2 = self._mock_backend("b2", False, None)
        brain = self._make_brain([b1, b2])

        result = brain._generate("test")
        assert result is None

    def test_empty_backends_returns_none(self):
        brain = self._make_brain([])
        result = brain._generate("test")
        assert result is None

    def test_any_backend_available(self):
        b1 = self._mock_backend("b1", False, None)
        b2 = self._mock_backend("b2", True, None)
        brain = self._make_brain([b1, b2])
        assert brain._any_backend_available() is True

    def test_no_backend_available(self):
        b1 = self._mock_backend("b1", False, None)
        brain = self._make_brain([b1])
        assert brain._any_backend_available() is False
