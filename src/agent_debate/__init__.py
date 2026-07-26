"""Agent Debate Engine public package."""

from importlib.metadata import PackageNotFoundError, version

from agent_debate.presets import DebateDepth, build_technical_review_config

try:
    __version__ = version("agent-debate-engine")
except PackageNotFoundError:  # pragma: no cover - editable source tree
    __version__ = "0.1.0"

__all__ = [
    "DebateDepth",
    "__version__",
    "build_technical_review_config",
]
