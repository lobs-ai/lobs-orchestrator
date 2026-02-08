"""
Ollama client for running diagnostic and analysis tasks.

Provides a lightweight alternative to OpenClaw for simpler tasks
that don't need full agent workspace/session management.
"""

import json
import logging
import requests
from typing import Any, Iterator

logger = logging.getLogger(__name__)


class OllamaClient:
    """
    Client for interacting with Ollama API.

    Used for lower-level diagnostic and analysis tasks that don't
    require the full OpenClaw agent infrastructure.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1",
        keep_alive: str = "5m"
    ):
        """
        Initialize Ollama client.

        Args:
            base_url: Ollama API base URL
            model: Model to use (e.g., "llama3.1", "mistral", "codellama")
            keep_alive: How long to keep model loaded in memory
                       Examples: "5m" (5 minutes), "1h" (1 hour), "-1" (forever)
                       After this time, Ollama unloads the model to free memory
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive

    def generate(
        self,
        prompt: str,
        system: str = None,
        stream: bool = False,
        temperature: float = 0.7,
        context_window: int = 8192,
    ) -> dict[str, Any] | Iterator[dict[str, Any]]:
        """
        Generate a response from Ollama.

        Args:
            prompt: The prompt to send
            system: Optional system prompt
            stream: Whether to stream the response
            temperature: Sampling temperature (0.0 to 1.0)
            context_window: Context window size

        Returns:
            Response dict (or iterator of dicts if streaming)
        """
        url = f"{self.base_url}/api/generate"

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": context_window,
            }
        }

        if system:
            payload["system"] = system

        try:
            response = requests.post(url, json=payload, stream=stream, timeout=300)
            response.raise_for_status()

            if stream:
                return self._stream_response(response)
            else:
                return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Ollama API request failed: {e}")
            raise

    def _stream_response(self, response: requests.Response) -> Iterator[dict[str, Any]]:
        """Stream and parse response chunks."""
        for line in response.iter_lines():
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse streaming response: {e}")
                    continue

    def chat(
        self,
        messages: list[dict[str, str]],
        stream: bool = False,
        temperature: float = 0.7,
    ) -> dict[str, Any] | Iterator[dict[str, Any]]:
        """
        Chat completion endpoint.

        Args:
            messages: List of message dicts with 'role' and 'content'
            stream: Whether to stream the response
            temperature: Sampling temperature

        Returns:
            Response dict (or iterator if streaming)
        """
        url = f"{self.base_url}/api/chat"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
            }
        }

        try:
            response = requests.post(url, json=payload, stream=stream, timeout=300)
            response.raise_for_status()

            if stream:
                return self._stream_response(response)
            else:
                return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Ollama chat request failed: {e}")
            raise

    def is_available(self) -> bool:
        """Check if Ollama is running and accessible."""
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def list_models(self) -> list[str]:
        """List available models."""
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            response.raise_for_status()
            data = response.json()
            return [model["name"] for model in data.get("models", [])]
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to list Ollama models: {e}")
            return []
