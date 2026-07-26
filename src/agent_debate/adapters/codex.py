"""Codex CLI 0.145 adapter."""

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
    request_max_output,
    request_model,
    request_optional_path,
    request_permission,
    request_timeout,
    request_workspace,
)

_CODEX_SANDBOX = {
    "read_only": "read-only",
    "workspace_write": "workspace-write",
    "danger_full_access": "danger-full-access",
}


class CodexAdapter(BaseAdapter):
    """Build and run a non-interactive Codex CLI 0.145 invocation."""

    name = "codex"

    def build_command(
        self,
        request: AgentRequestLike,
        agent_config: AgentConfigLike | None = None,
    ) -> CommandSpec:
        workspace = request_workspace(request)
        permission = request_permission(request, agent_config)
        model = request_model(request, agent_config)
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
            final_output_path=final_output_path,
        )


def build_codex_command(
    request: AgentRequestLike,
    agent_config: AgentConfigLike | None = None,
) -> CommandSpec:
    """Functional convenience wrapper around :class:`CodexAdapter`."""

    return CodexAdapter().build_command(request, agent_config)
