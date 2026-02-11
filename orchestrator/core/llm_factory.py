"""Factory for building the ordered list of LLM backends from settings."""

from __future__ import annotations

import logging
from typing import Callable, Any

from orchestrator.core.llm_backend import (
    LLMBackend,
    OllamaDirectBackend,
    HaikuBackend,
    FileBridgeBackend,
)

logger = logging.getLogger(__name__)


def create_backends(settings_fn: Callable[[str, Any], Any] | None = None) -> list[LLMBackend]:
    """Build the ordered backend list from settings.

    Args:
        settings_fn: Optional override for ``get_setting(key, default)``.
                     Defaults to :func:`orchestrator.utils.settings.get_setting`.

    Returns:
        Ordered list of :class:`LLMBackend` instances.
    """
    if settings_fn is None:
        from orchestrator.utils.settings import get_setting
        settings_fn = get_setting

    backend_names: list[str] = settings_fn("llm_backends", ["ollama", "haiku"])
    backends: list[LLMBackend] = []

    for name in backend_names:
        try:
            backend = _build_one(name, settings_fn)
            if backend is not None:
                backends.append(backend)
        except Exception as e:
            logger.warning("[LLM_FACTORY] Failed to create backend '%s': %s", name, e)

    logger.info("[LLM_FACTORY] Backends: %s", [b.name for b in backends])
    return backends


def _build_one(name: str, gs: Callable[[str, Any], Any]) -> LLMBackend | None:
    """Instantiate a single backend by name."""
    name = name.strip().lower()

    if name == "ollama":
        return OllamaDirectBackend(
            base_url=gs("ollama_url", "http://localhost:11434"),
            model=gs("ollama_meta_model", "llama3.2:1b"),
            keep_alive=gs("ollama_keep_alive", "5m"),
        )

    if name == "haiku":
        return HaikuBackend()

    if name == "file_bridge":
        bridge_dir = gs("file_bridge_dir", None)
        if not bridge_dir:
            logger.warning("[LLM_FACTORY] file_bridge requested but file_bridge_dir not set; skipping")
            return None
        from pathlib import Path
        return FileBridgeBackend(
            bridge_dir=Path(bridge_dir),
            model=gs("ollama_meta_model", "llama3.2:1b"),
            poll_interval=float(gs("file_bridge_poll_interval", 0.5)),
            timeout=float(gs("file_bridge_timeout", 120)),
        )

    logger.warning("[LLM_FACTORY] Unknown backend name '%s'; skipping", name)
    return None
