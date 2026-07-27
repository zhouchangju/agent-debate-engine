"""Shell-free adapter for arbitrary argv-based CLI agents."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from agent_debate.adapters.base import (
    REDACTED_PROMPT,
    AgentConfigLike,
    AgentRequestLike,
    BaseAdapter,
    CommandSpec,
    PromptTransport,
    command_prefix,
    redact_display_argv,
    reject_literal_credentials,
    request_extra_args,
    request_max_output,
    request_model,
    request_permission,
    request_timeout,
    request_workspace,
)
from agent_debate.errors import ConfigError


class GenericAdapter(BaseAdapter):
    """Transport a prompt to an externally trusted and sandboxed wrapper.

    Generic permission values are audit labels only. The engine therefore
    classifies every generic agent as unsafe regardless of that label.
    """

    name = "generic"

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        prompt_transport: PromptTransport | None = None,
        prompt_flag: str | None = None,
    ) -> None:
        self._command = tuple(command) if command is not None else None
        self._prompt_transport = prompt_transport
        self._prompt_flag = prompt_flag

    def build_command(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
    ) -> CommandSpec:
        workspace = request_workspace(request)
        # Validate the audit permission even though a generic provider has no
        # portable permission flag to synthesize.
        request_permission(request, agent_config)
        command = self._resolve_command(agent_config)
        extra_args = request_extra_args(request, agent_config)
        reject_literal_credentials(
            (*command, *extra_args),
            context="generic adapter argv",
        )
        transport = self._resolve_transport(agent_config)
        prompt_flag = self._resolve_prompt_flag(agent_config)
        if prompt_flag is not None:
            reject_literal_credentials(
                (prompt_flag,),
                context="generic prompt_flag",
            )
        prompt = request.prompt

        argv = [*command, *extra_args]
        stdin: str | None = None
        prompt_index: int | None = None
        if transport == "stdin":
            stdin = prompt
        else:
            if "\x00" in prompt:
                raise ConfigError(
                    f"Generic {transport} prompt transport cannot contain a NUL character"
                )
            if transport == "flag":
                if prompt_flag is None:
                    raise ConfigError("Generic flag prompt transport requires prompt_flag")
                argv.append(prompt_flag)
            argv.append(prompt)
            prompt_index = len(argv) - 1

        return CommandSpec(
            argv=tuple(argv),
            display_argv=redact_display_argv(
                argv,
                redactions=({prompt_index: REDACTED_PROMPT} if prompt_index is not None else None),
            ),
            cwd=workspace,
            stdin=stdin,
            timeout_seconds=request_timeout(request, agent_config),
            max_output_chars=request_max_output(request, agent_config),
            provider_adapter=self.name,
            provider_model=request_model(request, agent_config),
            session_mode="unverified",
            session_enforcement=(
                "generic adapter cannot prove provider-level session isolation"
            ),
        )

    def _resolve_command(
        self,
        agent_config: AgentConfigLike | None,
    ) -> tuple[str, ...]:
        if agent_config is not None:
            return command_prefix(agent_config, default_executable="agent")
        if self._command:
            return self._command
        raise ConfigError("GenericAdapter requires an agent_config or constructor command")

    def _resolve_transport(
        self,
        agent_config: AgentConfigLike | None,
    ) -> PromptTransport:
        configured = (
            getattr(agent_config, "prompt_transport", None)
            if agent_config is not None
            else self._prompt_transport
        )
        raw = getattr(configured, "value", configured)
        if raw is None:
            raise ConfigError("GenericAdapter requires prompt_transport")
        normalized = str(raw).strip().lower()
        if normalized not in {"stdin", "argument", "flag"}:
            raise ConfigError(f"Unsupported generic prompt_transport: {raw!r}")
        return cast(PromptTransport, normalized)

    def _resolve_prompt_flag(
        self,
        agent_config: AgentConfigLike | None,
    ) -> str | None:
        configured = (
            getattr(agent_config, "prompt_flag", None)
            if agent_config is not None
            else self._prompt_flag
        )
        if configured is None:
            return None
        flag = str(configured)
        if not flag.startswith("-") or "\x00" in flag:
            raise ConfigError("Generic prompt_flag must be a NUL-free argv flag")
        return flag


def build_generic_command(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None = None,
    *,
    command: Sequence[str] | None = None,
    prompt_transport: PromptTransport | None = None,
    prompt_flag: str | None = None,
) -> CommandSpec:
    """Functional convenience wrapper around :class:`GenericAdapter`."""

    return GenericAdapter(
        command=command,
        prompt_transport=prompt_transport,
        prompt_flag=prompt_flag,
    ).build_command(request, agent_config)
