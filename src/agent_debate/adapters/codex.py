"""Codex CLI adapter for the supported 0.x command contract."""

from __future__ import annotations

from agent_debate.adapters.base import (
    AgentConfigLike,
    AgentRequestLike,
    BaseAdapter,
    CommandSpec,
    provider_executable,
    redact_display_argv,
    reject_provider_extra_args,
    request_extra_args,
    request_max_final_output,
    request_max_output,
    request_model,
    request_model_reasoning_effort,
    request_optional_path,
    request_permission,
    request_timeout,
    request_workspace,
)

DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_MODEL_REASONING_EFFORT = "medium"

_CODEX_SANDBOX = {
    "read_only": "read-only",
    "workspace_write": "workspace-write",
    "danger_full_access": "danger-full-access",
}


class CodexAdapter(BaseAdapter):
    """Build and run a non-interactive Codex CLI invocation."""

    name = "codex"

    def build_command(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
    ) -> CommandSpec:
        workspace = request_workspace(request)
        permission = request_permission(request, agent_config)
        model = request_model(request, agent_config) or DEFAULT_CODEX_MODEL
        model_reasoning_effort = (
            request_model_reasoning_effort(request, agent_config)
            or DEFAULT_CODEX_MODEL_REASONING_EFFORT
        )
        extra_args = request_extra_args(request, agent_config)
        reject_provider_extra_args(extra_args, adapter_name=self.name)

        argv = [
            provider_executable(
                agent_config,
                default_executable="codex",
                adapter_name=self.name,
            ),
            "--ask-for-approval",
            "never",
            "--sandbox",
            _CODEX_SANDBOX[permission],
            "--cd",
            str(workspace),
        ]
        if model is not None:
            argv.extend(("--model", model))
        if model_reasoning_effort is not None:
            argv.extend(("--config", f"model_reasoning_effort={model_reasoning_effort}"))

        argv.extend(
            (
                "exec",
                "--json",
                "--color",
                "never",
                "--ephemeral",
                "--skip-git-repo-check",
            )
        )
        final_output_path = request_optional_path(
            request,
            "final_output_path",
            relative_to=workspace,
        )
        if final_output_path is not None:
            argv.extend(("-o", str(final_output_path)))

        output_schema_path = request_optional_path(
            request,
            "output_schema_path",
            relative_to=workspace,
        )
        if output_schema_path is not None:
            argv.extend(("--output-schema", str(output_schema_path)))

        argv.append("-")
        immutable_argv = tuple(argv)
        return CommandSpec(
            argv=immutable_argv,
            display_argv=redact_display_argv(immutable_argv),
            cwd=workspace,
            stdin=request.prompt,
            timeout_seconds=request_timeout(request, agent_config),
            max_output_chars=request_max_output(request, agent_config),
            max_final_output_chars=request_max_final_output(request, agent_config),
            truncate_transport_output=final_output_path is not None,
            allow_residual_process_cleanup=final_output_path is not None,
            final_output_path=final_output_path,
            provider_adapter=self.name,
            provider_model=model,
            session_mode="fresh",
            session_enforcement=(
                "codex exec --ephemeral; provider session files are not persisted or resumed"
            ),
        )


def build_codex_command(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None = None,
) -> CommandSpec:
    """Functional convenience wrapper around :class:`CodexAdapter`."""

    return CodexAdapter().build_command(request, agent_config)
