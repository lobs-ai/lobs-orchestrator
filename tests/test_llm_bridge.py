"""Tests for the llm-bridge host-side script."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the bridge module (no .py extension, so use spec_from_loader)
import importlib.util
import importlib.machinery
_bridge_path = str(Path(__file__).parent.parent / "bin" / "llm-bridge")
_loader = importlib.machinery.SourceFileLoader("llm_bridge", _bridge_path)
_spec = importlib.util.spec_from_loader("llm_bridge", _loader, origin=_bridge_path)
llm_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llm_bridge)


class TestProcessRequest:
    """Test process_request handling of various Ollama responses."""

    def _write_request(self, requests_dir: Path, req_id: str = "test-123", **overrides) -> Path:
        payload = {
            "id": req_id,
            "prompt": "Hello",
            "temperature": 0.5,
            "max_tokens": 256,
            "model": "llama3.2:1b",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        payload.update(overrides)
        path = requests_dir / f"{req_id}.json"
        path.write_text(json.dumps(payload))
        return path

    @patch("requests.post")
    def test_successful_request(self, mock_post, tmp_path):
        requests_dir = tmp_path / "requests"
        responses_dir = tmp_path / "responses"
        requests_dir.mkdir()
        responses_dir.mkdir()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "Hello world"}
        mock_post.return_value = mock_resp

        req_path = self._write_request(requests_dir)
        llm_bridge.process_request(req_path, responses_dir, "http://localhost:11434")

        # Response file should exist
        resp_files = list(responses_dir.glob("*.json"))
        assert len(resp_files) == 1

        data = json.loads(resp_files[0].read_text())
        assert data["id"] == "test-123"
        assert data["response"] == "Hello world"
        assert data["error"] is None
        assert "timestamp" in data

        # Request file should be cleaned up
        assert not req_path.exists()

    @patch("requests.post")
    def test_ollama_parameters_forwarded(self, mock_post, tmp_path):
        requests_dir = tmp_path / "requests"
        responses_dir = tmp_path / "responses"
        requests_dir.mkdir()
        responses_dir.mkdir()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_post.return_value = mock_resp

        req_path = self._write_request(
            requests_dir,
            model="codellama:7b",
            temperature=0.9,
        )
        llm_bridge.process_request(req_path, responses_dir, "http://myhost:11434")

        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["model"] == "codellama:7b"
        assert payload["options"]["temperature"] == 0.9
        assert call_kwargs.args[0] == "http://myhost:11434/api/generate"

    @patch("requests.post")
    def test_connection_error_writes_error_response(self, mock_post, tmp_path):
        import requests as real_requests
        requests_dir = tmp_path / "requests"
        responses_dir = tmp_path / "responses"
        requests_dir.mkdir()
        responses_dir.mkdir()

        mock_post.side_effect = real_requests.exceptions.ConnectionError("refused")

        req_path = self._write_request(requests_dir)
        llm_bridge.process_request(req_path, responses_dir, "http://localhost:11434")

        resp_files = list(responses_dir.glob("*.json"))
        assert len(resp_files) == 1
        data = json.loads(resp_files[0].read_text())
        assert data["response"] is None
        assert "Cannot connect" in data["error"]

        # Request should still be cleaned up
        assert not req_path.exists()

    @patch("requests.post")
    def test_timeout_writes_error_response(self, mock_post, tmp_path):
        import requests as real_requests
        requests_dir = tmp_path / "requests"
        responses_dir = tmp_path / "responses"
        requests_dir.mkdir()
        responses_dir.mkdir()

        mock_post.side_effect = real_requests.exceptions.Timeout("timed out")

        req_path = self._write_request(requests_dir)
        llm_bridge.process_request(req_path, responses_dir, "http://localhost:11434")

        resp_files = list(responses_dir.glob("*.json"))
        assert len(resp_files) == 1
        data = json.loads(resp_files[0].read_text())
        assert data["response"] is None
        assert "timed out" in data["error"]

    @patch("requests.post")
    def test_http_error_writes_error_response(self, mock_post, tmp_path):
        import requests as real_requests
        requests_dir = tmp_path / "requests"
        responses_dir = tmp_path / "responses"
        requests_dir.mkdir()
        responses_dir.mkdir()

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "model not found"
        http_err = real_requests.exceptions.HTTPError(response=mock_resp)
        mock_post.side_effect = http_err

        req_path = self._write_request(requests_dir)
        llm_bridge.process_request(req_path, responses_dir, "http://localhost:11434")

        resp_files = list(responses_dir.glob("*.json"))
        assert len(resp_files) == 1
        data = json.loads(resp_files[0].read_text())
        assert data["response"] is None
        assert "404" in data["error"]

    def test_malformed_request_writes_error_and_cleans_up(self, tmp_path):
        requests_dir = tmp_path / "requests"
        responses_dir = tmp_path / "responses"
        requests_dir.mkdir()
        responses_dir.mkdir()

        # Write invalid JSON
        req_path = requests_dir / "bad-req.json"
        req_path.write_text("not json at all{{{")

        llm_bridge.process_request(req_path, responses_dir, "http://localhost:11434")

        # Error response should be written
        resp_files = list(responses_dir.glob("*.json"))
        assert len(resp_files) == 1
        data = json.loads(resp_files[0].read_text())
        assert data["error"] is not None
        assert data["response"] is None

        # Malformed request cleaned up
        assert not req_path.exists()


class TestCheckOllama:
    @patch("requests.get")
    def test_returns_true_when_reachable(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        assert llm_bridge.check_ollama("http://localhost:11434") is True

    @patch("requests.get")
    def test_returns_false_on_connection_error(self, mock_get):
        import requests as real_requests
        mock_get.side_effect = real_requests.exceptions.ConnectionError()
        assert llm_bridge.check_ollama("http://localhost:11434") is False

    @patch("requests.get")
    def test_returns_false_on_non_200(self, mock_get):
        mock_get.return_value = MagicMock(status_code=500)
        assert llm_bridge.check_ollama("http://localhost:11434") is False


class TestAtomicResponseWrite:
    """Verify response files are written atomically (via tmp + rename)."""

    @patch("requests.post")
    def test_no_partial_response_visible(self, mock_post, tmp_path):
        """The response should appear atomically — never a .tmp file left behind."""
        requests_dir = tmp_path / "requests"
        responses_dir = tmp_path / "responses"
        requests_dir.mkdir()
        responses_dir.mkdir()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "done"}
        mock_post.return_value = mock_resp

        req_path = requests_dir / "atomic-test.json"
        req_path.write_text(json.dumps({
            "id": "atomic-test",
            "prompt": "test",
            "temperature": 0.5,
            "max_tokens": 64,
            "model": "test",
            "timestamp": "2026-01-01T00:00:00Z",
        }))

        llm_bridge.process_request(req_path, responses_dir, "http://localhost:11434")

        # No .tmp files should remain
        assert list(responses_dir.glob("*.tmp")) == []
        # Response should exist
        assert (responses_dir / "atomic-test.json").exists()
