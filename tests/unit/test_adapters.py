from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_debate.adapters import base as adapter_base
from agent_debate.adapters.base import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    REDACTED_PROMPT,
    CommandSpec,
    FinalOutputError,
    command_prefix,
    provider_executable,
    redact_display_argv,
    reject_literal_credentials,
    request_max_output,
    request_model,
    request_optional_path,
    request_permission,
    request_timeout,
    request_workspace,
)
from agent_debate.adapters.codex import CodexAdapter, build_codex_command
from agent_debate.adapters.generic import GenericAdapter, build_generic_command
from agent_debate.adapters.kimi import KimiAdapter, PromptTooLargeError, build_kimi_command
from agent_debate.adapters.process import ProcessOutputLimitError
from agent_debate.adapters.registry import available_adapters, get_adapter
from agent_debate.config import AgentAdapter as AdapterKind
from agent_debate.config import AgentConfig, PromptTransport
from agent_debate.errors import ConfigError
from agent_debate.models import AgentRequest, PermissionMode


def make_request(
    workspace: Path,
    *,
    prompt: str = "Review the proposed design.",
    permission: PermissionMode = PermissionMode.READ_ONLY,
    model: str | None = None,
    extra_args: tuple[str, ...] = (),
    final_output_path: Path | None = None,
    output_schema_path: Path | None = None,
) -> AgentRequest:
    return AgentRequest(
        agent_id="test-agent",
        role_id="reviewer",
        prompt=prompt,
        cwd=workspace,
        timeout_seconds=17.5,
        max_output_chars=12_345,
        model=model,
        permission=permission,
        extra_args=extra_args,
        final_output_path=final_output_path,
        output_schema_path=output_schema_path,
    )


def make_fake_executable(path: Path, source: str) -> Path:
    """Write one directly executable Python provider fixture."""

    path.write_text(f"#!{sys.executable} -S\n{source}", encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.mark.parametrize(
    ("permission", "sandbox"),
    [
        (PermissionMode.READ_ONLY, "read-only"),
        (PermissionMode.WORKSPACE_WRITE, "workspace-write"),
        (PermissionMode.DANGER_FULL_ACCESS, "danger-full-access"),
    ],
    ids=("read-only", "workspace-write", "danger-full-access"),
)
def test_codex_0145_maps_each_permission_to_exact_argv(
    tmp_path: Path,
    permission: PermissionMode,
    sandbox: str,
) -> None:
    prompt = "Return only the final recommendation."
    request = make_request(tmp_path, prompt=prompt, permission=permission)
    config = AgentConfig(adapter=AdapterKind.CODEX, command=("codex",))

    spec = CodexAdapter().build_command(request, config)

    assert spec.argv == (
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        sandbox,
        "--cd",
        str(tmp_path),
        "exec",
        "--json",
        "--color",
        "never",
        "--ephemeral",
        "--skip-git-repo-check",
        "-",
    )
    assert spec.display_argv == spec.argv
    assert spec.stdin == prompt
    assert spec.cwd == tmp_path


def test_codex_0145_orders_model_and_managed_artifact_args(
    tmp_path: Path,
) -> None:
    final_path = tmp_path / "last-message.txt"
    schema_path = tmp_path / "judge.schema.json"
    prompt = "Judge this debate without exposing this prompt."
    request = make_request(
        tmp_path,
        prompt=prompt,
        permission=PermissionMode.WORKSPACE_WRITE,
        model="request-model",
        final_output_path=final_path,
        output_schema_path=schema_path,
    )
    config = AgentConfig(
        adapter=AdapterKind.CODEX,
        command=("codex-wrapper",),
        model="config-model",
    )

    spec = CodexAdapter().build_command(request, config)

    assert spec.argv == (
        "codex-wrapper",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(tmp_path),
        "--model",
        "request-model",
        "exec",
        "--json",
        "--color",
        "never",
        "--ephemeral",
        "--skip-git-repo-check",
        "-o",
        str(final_path),
        "--output-schema",
        str(schema_path),
        "-",
    )
    assert spec.stdin == prompt
    assert spec.final_output_path == final_path
    assert spec.timeout_seconds == 17.5
    assert spec.max_output_chars == 12_345


def test_kimi_0291_builds_exact_redacted_argv_for_full_access(
    tmp_path: Path,
) -> None:
    prompt = "Analyze $(touch /tmp/not-executed); keep this private."
    request = make_request(
        tmp_path,
        prompt=prompt,
        permission=PermissionMode.DANGER_FULL_ACCESS,
        model="kimi-request-model",
    )
    config = AgentConfig(
        adapter=AdapterKind.KIMI,
        command=("kimi-wrapper",),
        model="kimi-config-model",
        permission=PermissionMode.DANGER_FULL_ACCESS,
    )

    spec = KimiAdapter().build_command(request, config)

    assert spec.argv == (
        "kimi-wrapper",
        "--prompt",
        prompt,
        "--output-format",
        "text",
        "--model",
        "kimi-request-model",
    )
    assert spec.display_argv == (
        "kimi-wrapper",
        "--prompt",
        REDACTED_PROMPT,
        "--output-format",
        "text",
        "--model",
        "kimi-request-model",
    )
    assert {"--plan", "--yolo", "--auto"}.isdisjoint(spec.argv)
    assert prompt not in spec.display_argv
    assert spec.stdin is None
    assert spec.cwd == tmp_path


@pytest.mark.parametrize(
    "permission",
    [PermissionMode.READ_ONLY, PermissionMode.WORKSPACE_WRITE],
    ids=("read-only", "workspace-write"),
)
def test_kimi_0291_fails_closed_for_unenforceable_permissions(
    tmp_path: Path,
    permission: PermissionMode,
) -> None:
    config = AgentConfig(
        adapter=AdapterKind.KIMI,
        command=("kimi",),
        permission=PermissionMode.DANGER_FULL_ACCESS,
    )

    with pytest.raises(ConfigError, match="danger_full_access"):
        KimiAdapter().build_command(
            make_request(tmp_path, permission=permission),
            config,
        )


def test_kimi_0291_prompt_ceiling_counts_utf8_bytes(tmp_path: Path) -> None:
    config = AgentConfig(
        adapter=AdapterKind.KIMI,
        command=("kimi",),
        permission=PermissionMode.DANGER_FULL_ACCESS,
    )
    adapter = KimiAdapter(prompt_byte_limit=6)

    exact = adapter.build_command(
        make_request(
            tmp_path,
            prompt="你好",
            permission=PermissionMode.DANGER_FULL_ACCESS,
        ),
        config,
    )
    assert exact.argv[2] == "你好"

    with pytest.raises(PromptTooLargeError) as exc_info:
        adapter.build_command(
            make_request(
                tmp_path,
                prompt="你好a",
                permission=PermissionMode.DANGER_FULL_ACCESS,
            ),
            config,
        )

    assert exc_info.value.observed_bytes == 7
    assert exc_info.value.limit_bytes == 6
    assert "UTF-8 prompt is 7 bytes" in str(exc_info.value)


def test_generic_stdin_transport_keeps_prompt_out_of_argv(tmp_path: Path) -> None:
    prompt = "literal stdin; $(touch /tmp/not-executed)"
    request = make_request(
        tmp_path,
        prompt=prompt,
        extra_args=("--request=value with spaces",),
    )
    config = AgentConfig(
        adapter=AdapterKind.GENERIC,
        command=("custom-agent", "--fixed-prefix"),
        extra_args=("--config=literal;still-one-argument",),
        prompt_transport=PromptTransport.STDIN,
    )

    spec = GenericAdapter().build_command(request, config)

    assert spec.argv == (
        "custom-agent",
        "--fixed-prefix",
        "--config=literal;still-one-argument",
        "--request=value with spaces",
    )
    assert spec.display_argv == spec.argv
    assert spec.stdin == prompt
    assert prompt not in spec.argv


def test_generic_argument_transport_preserves_one_shell_free_argv_item(
    tmp_path: Path,
) -> None:
    prompt = "literal argument; $(touch /tmp/not-executed) && exit 9"
    request = make_request(tmp_path, prompt=prompt)
    config = AgentConfig(
        adapter=AdapterKind.GENERIC,
        command=("custom-agent", "--fixed-prefix"),
        prompt_transport=PromptTransport.ARGUMENT,
    )

    spec = GenericAdapter().build_command(request, config)

    assert spec.argv == ("custom-agent", "--fixed-prefix", prompt)
    assert spec.display_argv == (
        "custom-agent",
        "--fixed-prefix",
        REDACTED_PROMPT,
    )
    assert spec.argv[-1] == prompt
    assert prompt not in spec.display_argv
    assert spec.stdin is None


def test_generic_flag_transport_orders_flag_and_redacts_prompt(
    tmp_path: Path,
) -> None:
    prompt = "literal flag value | never invoke a shell"
    request = make_request(tmp_path, prompt=prompt, extra_args=("--verbose",))
    config = AgentConfig(
        adapter=AdapterKind.GENERIC,
        command=("custom-agent",),
        extra_args=("--config", "value with spaces"),
        prompt_transport=PromptTransport.FLAG,
        prompt_flag="--message",
    )

    spec = GenericAdapter().build_command(request, config)

    assert spec.argv == (
        "custom-agent",
        "--config",
        "value with spaces",
        "--verbose",
        "--message",
        prompt,
    )
    assert spec.display_argv == (
        "custom-agent",
        "--config",
        "value with spaces",
        "--verbose",
        "--message",
        REDACTED_PROMPT,
    )
    assert prompt not in spec.display_argv
    assert spec.stdin is None


@pytest.mark.parametrize(
    "credential_argv",
    [
        ("--api-key", "literal-secret"),
        ("--access-token=literal-secret",),
        ("--github-token=literal-secret",),
        ("OPENAI_API_KEY=literal-secret",),
        ("GITHUB_TOKEN=literal-secret",),
    ],
)
def test_generic_rejects_literal_credentials_in_argv(
    tmp_path: Path,
    credential_argv: tuple[str, ...],
) -> None:
    config = AgentConfig(
        adapter=AdapterKind.GENERIC,
        command=("custom-agent",),
        prompt_transport=PromptTransport.STDIN,
    )
    request = make_request(tmp_path, extra_args=credential_argv)

    with pytest.raises(ConfigError, match="literal credential option"):
        GenericAdapter().build_command(request, config)


def test_generic_rejects_credential_shaped_prompt_flag(tmp_path: Path) -> None:
    config = AgentConfig(
        adapter=AdapterKind.GENERIC,
        command=("custom-agent",),
        prompt_transport=PromptTransport.FLAG,
        prompt_flag="--api-key",
    )

    with pytest.raises(ConfigError, match="literal credential option"):
        GenericAdapter().build_command(make_request(tmp_path), config)


@pytest.mark.parametrize(
    "unmodeled_arg",
    [
        "-a",
        "--ask-for-approval=on-request",
        "-s",
        "--sandbox=danger-full-access",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--add-dir=/tmp/extra-workspace",
        "-C",
        "--cd=/tmp",
        "-m",
        "--model=overridden",
        "--json",
        "--color=always",
        "--ephemeral",
        "-o",
        "--output-last-message=/tmp/leak",
        "--output-schema=/tmp/other-schema.json",
        "-c",
        "--config=sandbox_permissions=['disk-full-read-access']",
        "--profile=unsafe",
        "--image=/tmp/untrusted.png",
        "--enable=web_search",
        "--disable=safety",
        "resume",
    ],
)
def test_codex_rejects_every_request_extra_arg(
    tmp_path: Path,
    unmodeled_arg: str,
) -> None:
    config = AgentConfig(adapter=AdapterKind.CODEX, command=("codex",))
    request = make_request(tmp_path, extra_args=(unmodeled_arg,))

    with pytest.raises(ConfigError, match="extra_args are disabled"):
        CodexAdapter().build_command(request, config)


@pytest.mark.parametrize(
    "unmodeled_arg",
    [
        "-p",
        "--prompt=secret",
        "--output-format=json",
        "-m",
        "--model=overridden",
        "--plan",
        "-y",
        "--yolo",
        "--auto",
        "--add-dir=/tmp/extra-workspace",
        "-cy",
        "--verbose",
        "session-name",
    ],
)
def test_kimi_rejects_every_request_extra_arg(
    tmp_path: Path,
    unmodeled_arg: str,
) -> None:
    config = AgentConfig(
        adapter=AdapterKind.KIMI,
        command=("kimi",),
        permission=PermissionMode.DANGER_FULL_ACCESS,
    )
    request = make_request(
        tmp_path,
        permission=PermissionMode.DANGER_FULL_ACCESS,
        extra_args=(unmodeled_arg,),
    )

    with pytest.raises(ConfigError, match="extra_args are disabled"):
        KimiAdapter().build_command(request, config)


@pytest.mark.parametrize(
    ("adapter", "kind", "reserved_arg"),
    [
        (CodexAdapter(), AdapterKind.CODEX, "--json"),
        (KimiAdapter(), AdapterKind.KIMI, "--auto"),
    ],
)
def test_structural_config_extra_args_are_also_rejected(
    tmp_path: Path,
    adapter: CodexAdapter | KimiAdapter,
    kind: AdapterKind,
    reserved_arg: str,
) -> None:
    config = SimpleNamespace(
        command=(kind.value,),
        permission=(
            PermissionMode.DANGER_FULL_ACCESS
            if kind is AdapterKind.KIMI
            else PermissionMode.READ_ONLY
        ),
        extra_args=(reserved_arg,),
    )

    with pytest.raises(ConfigError, match="extra_args are disabled"):
        adapter.build_command(
            make_request(
                tmp_path,
                permission=(
                    PermissionMode.DANGER_FULL_ACCESS
                    if kind is AdapterKind.KIMI
                    else PermissionMode.READ_ONLY
                ),
            ),
            config,
        )


@pytest.mark.parametrize(
    ("kind", "expected_type"),
    [
        (" CODEX ", CodexAdapter),
        (AdapterKind.KIMI, KimiAdapter),
        ("generic", GenericAdapter),
    ],
)
def test_registry_resolves_strings_and_config_enums(
    kind: str | AdapterKind,
    expected_type: type[CodexAdapter] | type[KimiAdapter] | type[GenericAdapter],
) -> None:
    assert isinstance(get_adapter(kind), expected_type)


def test_registry_lists_stable_keys_and_returns_fresh_adapters() -> None:
    assert available_adapters() == ("codex", "generic", "kimi")
    assert get_adapter("codex") is not get_adapter("codex")


def test_registry_rejects_unknown_adapter_with_available_choices() -> None:
    with pytest.raises(
        ConfigError,
        match=r"Unknown agent adapter 'other'.*codex, generic, kimi",
    ):
        get_adapter("other")


async def test_codex_execute_uses_final_output_file_as_authoritative_text(
    tmp_path: Path,
) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    make_fake_executable(
        fake_codex,
        (
            "import pathlib, sys\n"
            "prompt = sys.stdin.read()\n"
            "output_path = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "output_path.write_text('FINAL:' + prompt, encoding='utf-8')\n"
            'print(\'{"type":"turn.completed"}\')\n'
        ),
    )
    final_path = tmp_path / "final.txt"
    request = make_request(
        tmp_path,
        prompt="private prompt",
        final_output_path=final_path,
    )
    config = AgentConfig(
        adapter=AdapterKind.CODEX,
        command=(str(fake_codex),),
    )

    result = await CodexAdapter().execute(request, config)

    assert result.stdout == '{"type":"turn.completed"}\n'
    assert result.final_text == "FINAL:private prompt"
    assert result.output == "FINAL:private prompt"
    assert result.exit_code == 0


async def test_codex_execute_fails_when_requested_final_output_is_missing(
    tmp_path: Path,
) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    make_fake_executable(fake_codex, "print('success without artifact')\n")
    request = make_request(
        tmp_path,
        final_output_path=tmp_path / "missing.txt",
    )
    config = AgentConfig(
        adapter=AdapterKind.CODEX,
        command=(str(fake_codex),),
    )

    with pytest.raises(FinalOutputError, match="did not produce or refresh"):
        await CodexAdapter().execute(request, config)


async def test_codex_execute_does_not_follow_final_output_symlink(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    unrelated = tmp_path / "unrelated-secret.txt"
    unrelated.write_text("do not read this", encoding="utf-8")
    make_fake_executable(
        fake_codex,
        (
            "import pathlib, sys\n"
            "output_path = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            f"output_path.symlink_to({str(unrelated)!r})\n"
        ),
    )
    final_path = tmp_path / "final.txt"
    request = make_request(tmp_path, final_output_path=final_path)
    config = AgentConfig(
        adapter=AdapterKind.CODEX,
        command=(str(fake_codex),),
    )

    with pytest.raises(FinalOutputError, match="could not be read"):
        await CodexAdapter().execute(request, config)


async def test_codex_execute_rejects_hard_linked_final_output(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    unrelated = tmp_path / "unrelated-secret.txt"
    unrelated.write_text("do not read this", encoding="utf-8")
    make_fake_executable(
        fake_codex,
        (
            "import os, pathlib, sys\n"
            "output_path = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            f"os.link({str(unrelated)!r}, output_path)\n"
        ),
    )
    request = make_request(tmp_path, final_output_path=tmp_path / "final.txt")
    config = AgentConfig(adapter=AdapterKind.CODEX, command=(str(fake_codex),))

    with pytest.raises(FinalOutputError, match="could not be read") as caught:
        await CodexAdapter().execute(request, config)

    assert "exactly one hard link" in str(caught.value.__cause__)


def test_final_output_read_rejects_link_count_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "final.txt"
    output.write_text("stable text", encoding="utf-8")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int) -> os.stat_result:
        nonlocal calls
        metadata = real_fstat(descriptor)
        calls += 1
        if calls != 2:
            return metadata
        values = list(metadata)
        values[3] = 2
        return os.stat_result(values)

    monkeypatch.setattr(adapter_base.os, "fstat", changing_fstat)

    with pytest.raises(OSError, match="exactly one hard link"):
        adapter_base._read_bounded_text(output, 100)


async def test_codex_execute_rejects_unchanged_stale_final_output(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    make_fake_executable(fake_codex, "print('success without artifact')\n")
    final_path = tmp_path / "stale-final.txt"
    final_path.write_text("answer from an earlier attempt", encoding="utf-8")
    request = make_request(tmp_path, final_output_path=final_path)
    config = AgentConfig(
        adapter=AdapterKind.CODEX,
        command=(str(fake_codex),),
    )

    with pytest.raises(FinalOutputError, match="did not produce or refresh"):
        await CodexAdapter().execute(request, config)


def test_codex_resolves_relative_artifact_paths_against_workspace(tmp_path: Path) -> None:
    request = make_request(
        tmp_path,
        final_output_path=Path("artifacts/final.txt"),
        output_schema_path=Path("schemas/judge.json"),
    )
    config = AgentConfig(adapter=AdapterKind.CODEX, command=("codex",))

    spec = CodexAdapter().build_command(request, config)

    assert spec.final_output_path == tmp_path / "artifacts/final.txt"
    assert str(tmp_path / "artifacts/final.txt") in spec.argv
    assert str(tmp_path / "schemas/judge.json") in spec.argv


async def test_codex_final_output_file_obeys_character_limit(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    make_fake_executable(
        fake_codex,
        (
            "import pathlib, sys\n"
            "output_path = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "output_path.write_text('x' * 20_000, encoding='utf-8')\n"
        ),
    )
    request = make_request(
        tmp_path,
        final_output_path=tmp_path / "oversized.txt",
    )
    config = AgentConfig(
        adapter=AdapterKind.CODEX,
        command=(str(fake_codex),),
    )

    with pytest.raises(ProcessOutputLimitError) as caught:
        await CodexAdapter().execute(request, config)

    assert caught.value.stream == "final"
    assert caught.value.limit == request.max_output_chars
    assert caught.value.exit_code == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"argv": (), "display_argv": ()},
        {"argv": ("agent",), "display_argv": ("agent", "--extra")},
        {"argv": ("",), "display_argv": ("agent",)},
        {"argv": ("agent",), "display_argv": ("\x00",)},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"max_output_chars": 0},
        {"max_output_chars": 1.5},
        {"terminate_grace_seconds": -0.1},
        {"terminate_grace_seconds": float("nan")},
        {"terminate_grace_seconds": float("inf")},
    ],
)
def test_command_spec_rejects_invalid_process_contract(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "argv": ("agent",),
        "display_argv": ("agent",),
        "cwd": tmp_path,
    }
    values.update(overrides)

    with pytest.raises(ConfigError):
        CommandSpec(**values)  # type: ignore[arg-type]


def test_command_spec_compatibility_properties(tmp_path: Path) -> None:
    spec = CommandSpec(
        argv=("agent",),
        display_argv=("agent",),
        cwd=tmp_path,
        stdin="prompt",
    )

    assert spec.command == spec.argv
    assert spec.stdin_data == "prompt"


def test_display_argv_redaction_is_shape_preserving_and_centralized() -> None:
    argv = (
        "agent",
        "--api-key",
        "first-secret",
        "--access-token=second-secret",
        "OPENAI_API_KEY=third-secret",
        "--message",
        "private prompt",
    )

    display = redact_display_argv(
        argv,
        redactions={6: REDACTED_PROMPT},
    )

    assert display == (
        "agent",
        "--api-key",
        "<credential:redacted>",
        "--access-token=<credential:redacted>",
        "OPENAI_API_KEY=<credential:redacted>",
        "--message",
        REDACTED_PROMPT,
    )
    assert len(display) == len(argv)
    assert all("secret" not in argument for argument in display)


def test_command_spec_rejects_unredacted_literal_credential(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="display_argv exposes"):
        CommandSpec(
            argv=("agent", "--api-key", "literal-secret"),
            display_argv=("agent", "--api-key", "literal-secret"),
            cwd=tmp_path,
        )


def test_literal_credential_error_never_echoes_secret() -> None:
    with pytest.raises(ConfigError) as caught:
        reject_literal_credentials(
            ("agent", "--api-key=literal-secret"),
            context="test argv",
        )

    assert "literal-secret" not in str(caught.value)
    assert "--api-key" in str(caught.value)


def test_structural_request_helpers_cover_fallbacks(tmp_path: Path) -> None:
    empty_request = SimpleNamespace()
    fallback_config = SimpleNamespace(
        command=(),
        executable="wrapped-agent",
        timeout=12.5,
        max_output=321,
        model="fallback-model",
        permission=PermissionMode.WORKSPACE_WRITE,
    )

    assert command_prefix(None, default_executable="default-agent") == ("default-agent",)
    assert command_prefix(
        SimpleNamespace(command="one literal executable"),
        default_executable="unused",
    ) == ("one literal executable",)
    assert command_prefix(fallback_config, default_executable="unused") == ("wrapped-agent",)
    with pytest.raises(ConfigError, match="must contain an executable"):
        command_prefix(SimpleNamespace(command=()), default_executable="unused")
    assert (
        provider_executable(
            SimpleNamespace(command=("codex",)),
            default_executable="unused",
            adapter_name="codex",
        )
        == "codex"
    )
    with pytest.raises(ConfigError, match="exactly one executable"):
        provider_executable(
            SimpleNamespace(command=("codex", "--profile=unsafe")),
            default_executable="unused",
            adapter_name="codex",
        )

    assert request_workspace(SimpleNamespace(cwd=tmp_path)) == tmp_path
    with pytest.raises(ConfigError, match="missing a workspace"):
        request_workspace(empty_request)
    assert request_timeout(empty_request, None) == DEFAULT_TIMEOUT_SECONDS
    assert request_timeout(empty_request, fallback_config) == 12.5
    assert request_max_output(empty_request, None) == DEFAULT_MAX_OUTPUT_CHARS
    assert request_max_output(empty_request, fallback_config) == 321
    assert request_model(empty_request, fallback_config) == "fallback-model"
    assert request_permission(empty_request, fallback_config) == "workspace_write"
    assert request_permission(SimpleNamespace(permission="full-access"), None) == (
        "danger_full_access"
    )
    with pytest.raises(ConfigError, match="Unsupported permission"):
        request_permission(SimpleNamespace(permission="root"), None)
    assert request_optional_path(SimpleNamespace(result_path=tmp_path), "result_path") == tmp_path


def test_functional_adapter_builders_and_constructor_fallbacks(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    assert build_codex_command(request).argv[0] == "codex"
    assert (
        build_kimi_command(
            make_request(
                tmp_path,
                permission=PermissionMode.DANGER_FULL_ACCESS,
            )
        ).argv[0]
        == "kimi"
    )
    generic = build_generic_command(
        request,
        command=("custom",),
        prompt_transport="stdin",
    )
    assert generic.argv == ("custom",)
    assert generic.stdin == request.prompt


def test_generic_constructor_validation_paths(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    with pytest.raises(ConfigError, match="constructor command"):
        GenericAdapter(prompt_transport="stdin").build_command(request)
    with pytest.raises(ConfigError, match="requires prompt_transport"):
        GenericAdapter(command=("custom",)).build_command(request)
    with pytest.raises(ConfigError, match="Unsupported generic"):
        GenericAdapter(
            command=("custom",),
            prompt_transport="query",  # type: ignore[arg-type]
        ).build_command(request)
    with pytest.raises(ConfigError, match="requires prompt_flag"):
        GenericAdapter(
            command=("custom",),
            prompt_transport="flag",
        ).build_command(request)
    with pytest.raises(ConfigError, match="NUL-free argv flag"):
        GenericAdapter(
            command=("custom",),
            prompt_transport="flag",
            prompt_flag="message",
        ).build_command(request)


def test_argument_transports_reject_nul_prompt(tmp_path: Path) -> None:
    request = make_request(tmp_path).model_copy(update={"prompt": "unsafe\x00prompt"})
    kimi_request = request.model_copy(update={"permission": PermissionMode.DANGER_FULL_ACCESS})

    with pytest.raises(ConfigError, match="NUL character"):
        KimiAdapter().build_command(kimi_request)
    with pytest.raises(ConfigError, match="NUL character"):
        GenericAdapter(
            command=("custom",),
            prompt_transport="argument",
        ).build_command(request)


def test_kimi_constructor_contract_and_alias() -> None:
    with pytest.raises(ConfigError, match="positive integer"):
        KimiAdapter(prompt_byte_limit=0)
    with pytest.raises(ConfigError, match=r"safe .* argv ceiling"):
        KimiAdapter(prompt_byte_limit=65 * 1024)

    assert KimiAdapter(prompt_byte_limit=42).max_prompt_bytes == 42
