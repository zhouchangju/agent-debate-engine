"""Kimi Code CLI 0.29.1 adapter."""

from __future__ import annotations

from agent_debate.adapters.base import (
    REDACTED_PROMPT,
    AgentConfigLike,
    AgentRequestLike,
    BaseAdapter,
    CommandSpec,
    provider_executable,
    redact_display_argv,
    reject_provider_extra_args,
    request_extra_args,
    request_max_output,
    request_model,
    request_permission,
    request_reasoning_effort,
    request_timeout,
    request_workspace,
)
from agent_debate.errors import AgentExecutionError, ConfigError

DEFAULT_KIMI_PROMPT_MAX_BYTES = 64 * 1024
KIMI_PROMPT_BYTE_LIMIT = DEFAULT_KIMI_PROMPT_MAX_BYTES
DEFAULT_KIMI_MODEL = "k3"
DEFAULT_KIMI_REASONING_EFFORT = "high"


def _normalize_kimi_reasoning_effort(value: str) -> str:
    """Normalize user-facing aliases to a CLI-accepted effort value."""

    normalized = value.strip().lower()
    alias = {
        "standard": "high",
        "advanced": "max",
        "extreme": "max",
        "medium": "high",
        "minimum": "low",
        "light": "low",
    }
    return alias.get(normalized, normalized)


class PromptTooLargeError(AgentExecutionError):
    """A Kimi prompt is too large for its documented argv transport."""

    def __init__(self, *, observed_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            "Kimi Code CLI 0.29.1 has no documented stdin prompt transport; "
            f"the UTF-8 prompt is {observed_bytes} bytes, exceeding the enforced "
            f"{limit_bytes}-byte argv ceiling"
        )
        self.observed_bytes = observed_bytes
        self.limit_bytes = limit_bytes


class KimiAdapter(BaseAdapter):
    """Build and run a non-interactive Kimi Code CLI 0.29.1 invocation."""

    name = "kimi"

    def __init__(self, prompt_byte_limit: int = DEFAULT_KIMI_PROMPT_MAX_BYTES) -> None:
        if (
            isinstance(prompt_byte_limit, bool)
            or not isinstance(prompt_byte_limit, int)
            or prompt_byte_limit <= 0
        ):
            raise ConfigError("Kimi prompt_byte_limit must be a positive integer")
        if prompt_byte_limit > KIMI_PROMPT_BYTE_LIMIT:
            raise ConfigError(
                "Kimi prompt_byte_limit must not exceed the safe "
                f"{KIMI_PROMPT_BYTE_LIMIT}-byte argv ceiling"
            )
        self.prompt_byte_limit = prompt_byte_limit

    @property
    def max_prompt_bytes(self) -> int:
        """Descriptive alias for the enforced argv payload ceiling."""

        return self.prompt_byte_limit

    def build_command(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
    ) -> CommandSpec:
        prompt = request.prompt
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > self.prompt_byte_limit:
            raise PromptTooLargeError(
                observed_bytes=prompt_bytes,
                limit_bytes=self.prompt_byte_limit,
            )
        if "\x00" in prompt:
            raise ConfigError("Kimi prompt cannot contain a NUL character in argv transport")

        workspace = request_workspace(request)
        permission = request_permission(request, agent_config)
        if permission != "danger_full_access":
            raise ConfigError(
                "Kimi Code CLI 0.29.1 prompt mode always runs with auto permission and "
                "auto-approves tool calls; it cannot honor read_only or workspace_write. "
                "Use danger_full_access with the engine's explicit --allow-unsafe "
                "acknowledgement, or choose Codex/generic with an external sandbox."
            )
        model = request_model(request, agent_config) or DEFAULT_KIMI_MODEL
        extra_args = request_extra_args(request, agent_config)
        reject_provider_extra_args(extra_args, adapter_name=self.name)

        argv = [
            provider_executable(
                agent_config,
                default_executable="kimi",
                adapter_name=self.name,
            ),
            "--prompt",
            prompt,
            "--output-format",
            "text",
        ]
        if model is not None:
            argv.extend(("--model", model))
        reasoning_effort = request_reasoning_effort(request, agent_config)
        if reasoning_effort is None:
            reasoning_effort = DEFAULT_KIMI_REASONING_EFFORT
        else:
            reasoning_effort = _normalize_kimi_reasoning_effort(reasoning_effort)
        env = {"KIMI_MODEL_THINKING_EFFORT": reasoning_effort} if reasoning_effort else None

        return CommandSpec(
            argv=tuple(argv),
            display_argv=redact_display_argv(
                argv,
                redactions={2: REDACTED_PROMPT},
            ),
            cwd=workspace,
            env=env,
            timeout_seconds=request_timeout(request, agent_config),
            max_output_chars=request_max_output(request, agent_config),
            provider_adapter=self.name,
            provider_model=model,
            session_mode="fresh",
            session_enforcement=(
                "new Kimi prompt session; adapter forbids --session/-S and --continue/-c"
            ),
        )


def build_kimi_command(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None = None,
    *,
    prompt_byte_limit: int = DEFAULT_KIMI_PROMPT_MAX_BYTES,
) -> CommandSpec:
    """Functional convenience wrapper around :class:`KimiAdapter`."""

    return KimiAdapter(prompt_byte_limit=prompt_byte_limit).build_command(request, agent_config)
