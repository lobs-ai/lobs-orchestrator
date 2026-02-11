"""Abstract LLM backend interface and concrete implementations.

Provides a common interface for text generation across different backends:
- OllamaDirectBackend: HTTP calls to a local Ollama instance
- HaikuBackend: HTTP calls to the Anthropic Messages API
- FileBridgeBackend: File-based communication via a shared directory
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class LLMBackend(ABC):
    """Abstract base class for LLM text-generation backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether this backend can currently serve requests."""
        ...

    @abstractmethod
    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 1024) -> str | None:
        """Generate text from *prompt*.

        Returns the response string, or ``None`` on failure / empty output.
        """
        ...


# ---------------------------------------------------------------------------
# Ollama (local HTTP)
# ---------------------------------------------------------------------------


class OllamaDirectBackend(LLMBackend):
    """Thin wrapper around the existing :class:`OllamaClient`."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1",
        keep_alive: str = "5m",
    ) -> None:
        from orchestrator.core.ollama_client import OllamaClient

        self._client = OllamaClient(base_url=base_url, model=model, keep_alive=keep_alive)

    @property
    def name(self) -> str:
        return f"ollama({self._client.model})"

    @property
    def client(self) -> "OllamaClient":
        """Expose the underlying client for Ollama-specific operations."""
        return self._client

    def is_available(self) -> bool:
        return self._client.is_available()

    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 1024) -> str | None:
        result = self._client.generate(
            prompt=prompt,
            stream=False,
            temperature=temperature,
            context_window=4096,
        )
        text = result.get("response", "").strip() if isinstance(result, dict) else ""
        return text if text else None


# ---------------------------------------------------------------------------
# Anthropic Haiku (cloud HTTP)
# ---------------------------------------------------------------------------

HAIKU_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


class HaikuBackend(LLMBackend):
    """Calls the Anthropic Messages API (Claude Haiku)."""

    @property
    def name(self) -> str:
        return "haiku"

    def is_available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 1024) -> str | None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None

        import requests

        try:
            resp = requests.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": HAIKU_MODEL,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            blocks = data.get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return text.strip() if text.strip() else None
        except Exception as e:
            logger.debug("[LLM] Haiku call failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# File Bridge (shared-directory IPC)
# ---------------------------------------------------------------------------


class FileBridgeBackend(LLMBackend):
    """Communicate with an external LLM runner via JSON files in a shared directory.

    The orchestrator writes request files; an external script on the host picks
    them up, calls Ollama (or another model), and writes response files back.
    """

    def __init__(
        self,
        bridge_dir: Path,
        model: str = "llama3.2:1b",
        poll_interval: float = 0.5,
        timeout: float = 120.0,
    ) -> None:
        self._bridge_dir = Path(bridge_dir)
        self._model = model
        self._poll_interval = poll_interval
        self._timeout = timeout

        # Ensure subdirectories exist if the bridge dir itself exists.
        if self._bridge_dir.exists():
            (self._bridge_dir / "requests").mkdir(exist_ok=True)
            (self._bridge_dir / "responses").mkdir(exist_ok=True)

    @property
    def name(self) -> str:
        return "file_bridge"

    def is_available(self) -> bool:
        requests_dir = self._bridge_dir / "requests"
        return requests_dir.is_dir() and os.access(requests_dir, os.W_OK)

    def generate(self, prompt: str, temperature: float = 0.7, max_tokens: int = 1024) -> str | None:
        req_id = str(uuid.uuid4())
        requests_dir = self._bridge_dir / "requests"
        responses_dir = self._bridge_dir / "responses"
        request_path = requests_dir / f"{req_id}.json"
        response_path = responses_dir / f"{req_id}.json"

        # Write request
        request_payload = {
            "id": req_id,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "model": self._model,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            request_path.write_text(json.dumps(request_payload), encoding="utf-8")
        except Exception as e:
            logger.warning("[LLM] FileBridge: failed to write request: %s", e)
            return None

        # Poll for response
        deadline = time.monotonic() + self._timeout
        try:
            while time.monotonic() < deadline:
                if response_path.exists():
                    try:
                        data = json.loads(response_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError) as e:
                        logger.warning("[LLM] FileBridge: bad response file: %s", e)
                        self._cleanup(request_path, response_path)
                        return None

                    # Cleanup files
                    self._cleanup(request_path, response_path)

                    if data.get("error"):
                        logger.warning("[LLM] FileBridge: response error: %s", data["error"])
                        return None

                    text = (data.get("response") or "").strip()
                    return text if text else None

                time.sleep(self._poll_interval)

            # Timeout
            logger.warning("[LLM] FileBridge: timed out after %.0fs waiting for %s", self._timeout, req_id)
            self._cleanup(request_path, None)
            return None

        except Exception as e:
            logger.warning("[LLM] FileBridge: unexpected error: %s", e)
            self._cleanup(request_path, response_path)
            return None

    @staticmethod
    def _cleanup(request_path: Path, response_path: Path | None) -> None:
        """Best-effort removal of request/response files."""
        for p in (request_path, response_path):
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
