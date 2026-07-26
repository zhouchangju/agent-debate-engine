"""Agent Debate Engine public package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-debate-engine")
except PackageNotFoundError:  # pragma: no cover - editable source tree
    __version__ = "0.1.0"

__all__ = ["__version__"]
