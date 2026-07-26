"""Built-in CLI adapter registry."""

from __future__ import annotations

from collections.abc import Callable

from agent_debate.adapters.base import AgentAdapter
from agent_debate.adapters.codex import CodexAdapter
from agent_debate.adapters.generic import GenericAdapter
from agent_debate.adapters.kimi import KimiAdapter
from agent_debate.errors import ConfigError

_BUILTIN_ADAPTERS: dict[str, Callable[[], AgentAdapter]] = {
    "codex": CodexAdapter,
    "kimi": KimiAdapter,
    "generic": GenericAdapter,
}


def get_adapter(kind: object) -> AgentAdapter:
    """Return a fresh adapter for a string or serialized Enum kind."""

    raw = getattr(kind, "value", kind)
    normalized = str(raw).strip().lower().replace("-", "_")
    try:
        factory = _BUILTIN_ADAPTERS[normalized]
    except KeyError as exc:
        available = ", ".join(sorted(_BUILTIN_ADAPTERS))
        raise ConfigError(f"Unknown agent adapter {raw!r}; choose one of: {available}") from exc
    return factory()


def available_adapters() -> tuple[str, ...]:
    """Return stable built-in registry keys."""

    return tuple(sorted(_BUILTIN_ADAPTERS))
