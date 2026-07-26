"""User-facing exception hierarchy."""


class DebateError(Exception):
    """Base class for expected engine failures."""


class ConfigError(DebateError):
    """Configuration is invalid or incomplete."""


class PreflightError(DebateError):
    """An agent executable or runtime prerequisite is unavailable."""


class UnsafeConfigurationError(DebateError):
    """A potentially destructive configuration was not explicitly authorized."""


class AgentExecutionError(DebateError):
    """An agent invocation failed."""


class ContextBudgetError(DebateError):
    """Required prompt content does not fit the configured budget."""


class JudgeProtocolError(DebateError):
    """The judge did not produce a valid decision."""


class ResumeError(DebateError):
    """A saved run cannot be resumed safely."""
