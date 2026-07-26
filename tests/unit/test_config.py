from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

from agent_debate.config import (
    AgentConfig,
    ContextConfig,
    DebateConfig,
    FailurePolicyConfig,
    PromptTransport,
    StopConfig,
    load_config,
)
from agent_debate.errors import ConfigError
from agent_debate.models import PermissionMode


def _valid_data() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run": {
            "output_dir": "runs",
            "workspace": ".",
            "max_parallel": 2,
            "stream": True,
        },
        "agents": {
            "primary": {
                "adapter": "codex",
                "command": ["codex"],
                "permission": "read_only",
                "timeout": 30,
                "max_output": 20_000,
                "retries": 0,
            },
            "judge": {
                "adapter": "generic",
                "command": ["judge-cli"],
                "permission": "workspace_write",
                "prompt_transport": "flag",
                "prompt_flag": "--prompt",
            },
        },
        "workflow": {
            "stages": [
                {
                    "id": "proposal",
                    "mode": "parallel",
                    "participants": [
                        {
                            "id": "architect",
                            "agent": "primary",
                            "prompt": "prompts/architect.md",
                        }
                    ],
                }
            ],
            "judge": {
                "agent": "judge",
                "prompt": "prompts/judge.md",
            },
            "stop": {
                "min_rounds": 1,
                "max_rounds": 3,
                "confidence_threshold": 0.8,
                "stable_rounds": 2,
                "max_elapsed_seconds": 300,
            },
        },
        "context": {
            "max_prompt_chars": 12_000,
            "max_requirement_chars": 4_000,
            "max_response_chars": 4_000,
            "keep_recent_rounds": 2,
        },
        "failure": {
            "on_agent_error": "abort",
            "on_judge_error": "retry",
            "require_all_participants": True,
            "schema_repair_attempts": 1,
        },
    }


def _write_config(directory: Path, data: dict[str, Any] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    prompts = directory / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "architect.md").write_text("Act as the architect.", encoding="utf-8")
    (prompts / "judge.md").write_text("Return Judge JSON.", encoding="utf-8")
    config_path = directory / "debate.yaml"
    config_path.write_text(yaml.safe_dump(data or _valid_data(), sort_keys=False), encoding="utf-8")
    return config_path


def test_load_config_resolves_every_path_relative_to_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "nested" / "config"
    config_path = _write_config(config_dir)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(config_path)

    assert config.source_path == config_path.resolve()
    assert config.run.workspace == config_dir.resolve()
    assert config.run.output_dir == (config_dir / "runs").resolve()
    participant = config.workflow.stages[0].participants[0]
    assert participant.prompt == (config_dir / "prompts/architect.md").resolve()
    assert config.workflow.judge.prompt == (config_dir / "prompts/judge.md").resolve()
    assert config.agents["primary"].model is None


def test_resolved_config_has_canonical_json_serialization(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    dumped = config.model_dump(mode="json")

    assert dumped["schema_version"] == 1
    assert dumped["run"]["workspace"] == str(tmp_path.resolve())
    assert dumped["agents"]["judge"]["permission"] == "workspace_write"
    assert dumped["agents"]["judge"]["command"] == ["judge-cli"]
    assert "timeout_seconds" not in dumped["agents"]["primary"]


def test_unknown_nested_keys_are_rejected(tmp_path: Path) -> None:
    data = _valid_data()
    data["agents"]["primary"]["mystery"] = True

    with pytest.raises(ConfigError, match="mystery"):
        load_config(_write_config(tmp_path, data))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]\n",
        "schema_version: [\n",
    ],
)
def test_empty_non_mapping_and_malformed_yaml_are_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "debate.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_yaml_mapping_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "debate.yaml"
    path.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="duplicate key"):
        load_config(path)


def test_utf8_decode_errors_are_wrapped_as_config_errors(tmp_path: Path) -> None:
    path = tmp_path / "debate.yaml"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigError, match="Could not read configuration"):
        load_config(path)


def test_schema_version_is_literal_integer_one() -> None:
    data = _valid_data()
    data["schema_version"] = "1"

    with pytest.raises(ValidationError, match="integer 1"):
        DebateConfig.model_validate(data)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1, 2])
def test_schema_repair_attempts_is_strict_zero_or_one(value: object) -> None:
    with pytest.raises(ValidationError):
        FailurePolicyConfig.model_validate({"schema_repair_attempts": value})


@pytest.mark.parametrize("command", [[], "codex", [""], {"codex", "unsafe"}])
def test_command_must_be_nonempty_argv(command: object) -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({"adapter": "codex", "command": command})


@pytest.mark.parametrize("adapter", ["codex", "kimi"])
def test_builtin_provider_command_is_exactly_one_executable(adapter: str) -> None:
    values: dict[str, object] = {
        "adapter": adapter,
        "command": [adapter, "--unsafe-prefix"],
    }
    if adapter == "kimi":
        values["permission"] = "danger_full_access"

    with pytest.raises(ValidationError, match="exactly one executable"):
        AgentConfig.model_validate(values)


@pytest.mark.parametrize("adapter", ["codex", "kimi"])
def test_builtin_provider_extra_args_are_disabled(adapter: str) -> None:
    values: dict[str, object] = {
        "adapter": adapter,
        "command": [adapter],
        "extra_args": ["--profile=unsafe"],
    }
    if adapter == "kimi":
        values["permission"] = "danger_full_access"

    with pytest.raises(ValidationError, match="extra_args are disabled"):
        AgentConfig.model_validate(values)


def test_generic_prompt_transport_contract() -> None:
    with pytest.raises(ValidationError, match="require prompt_transport"):
        AgentConfig.model_validate({"adapter": "generic", "command": ["agent"]})
    with pytest.raises(ValidationError, match="requires prompt_flag"):
        AgentConfig.model_validate(
            {
                "adapter": "generic",
                "command": ["agent"],
                "prompt_transport": "flag",
            }
        )
    with pytest.raises(ValidationError, match="only valid for generic"):
        AgentConfig.model_validate(
            {
                "adapter": "codex",
                "command": ["codex"],
                "prompt_transport": "stdin",
            }
        )

    config = AgentConfig.model_validate(
        {
            "adapter": "generic",
            "command": ["agent"],
            "prompt_transport": "argument",
        }
    )
    assert config.prompt_transport is PromptTransport.ARGUMENT


@pytest.mark.parametrize("permission", [None, "read_only", "workspace_write"])
def test_kimi_requires_explicit_full_access_permission(permission: str | None) -> None:
    values: dict[str, object] = {
        "adapter": "kimi",
        "command": ["kimi"],
    }
    if permission is not None:
        values["permission"] = permission

    with pytest.raises(ValidationError, match="danger_full_access"):
        AgentConfig.model_validate(values)


def test_kimi_accepts_explicit_full_access_permission() -> None:
    config = AgentConfig.model_validate(
        {
            "adapter": "kimi",
            "command": ["kimi"],
            "permission": "danger_full_access",
        }
    )
    assert config.permission is PermissionMode.DANGER_FULL_ACCESS


@pytest.mark.parametrize(
    "values",
    [
        {"adapter": "codex", "command": ["codex"], "model": "bad\x00model"},
        {
            "adapter": "generic",
            "command": ["agent"],
            "prompt_transport": "flag",
            "prompt_flag": "--prompt\x00value",
        },
    ],
)
def test_agent_command_metadata_must_be_nul_free(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="NUL"):
        AgentConfig.model_validate(values)


@pytest.mark.parametrize("reference", ["participant", "judge"])
def test_unknown_agent_references_are_rejected(reference: str) -> None:
    data = _valid_data()
    if reference == "participant":
        data["workflow"]["stages"][0]["participants"][0]["agent"] = "missing"
    else:
        data["workflow"]["judge"]["agent"] = "missing"

    with pytest.raises(ValidationError, match="unknown agent reference"):
        DebateConfig.model_validate(data)


def test_stage_and_participant_ids_must_be_safe_and_unique() -> None:
    duplicate_stage = _valid_data()
    duplicate_stage["workflow"]["stages"].append(deepcopy(duplicate_stage["workflow"]["stages"][0]))
    with pytest.raises(ValidationError, match="stage ids must be unique"):
        DebateConfig.model_validate(duplicate_stage)

    duplicate_participant = _valid_data()
    participant = duplicate_participant["workflow"]["stages"][0]["participants"][0]
    duplicate_participant["workflow"]["stages"][0]["participants"].append(deepcopy(participant))
    with pytest.raises(ValidationError, match="duplicate participant ids"):
        DebateConfig.model_validate(duplicate_participant)

    unsafe_id = _valid_data()
    unsafe_id["workflow"]["stages"][0]["id"] = "../escape"
    with pytest.raises(ValidationError):
        DebateConfig.model_validate(unsafe_id)


@pytest.mark.parametrize("reserved", ["CON", "nul", "LPT1"])
def test_portable_ids_reject_reserved_device_names(reserved: str) -> None:
    data = _valid_data()
    data["workflow"]["stages"][0]["id"] = reserved

    with pytest.raises(ValidationError, match="reserved path component"):
        DebateConfig.model_validate(data)


def test_agent_ids_cannot_shadow_each_other_after_normalization() -> None:
    data = _valid_data()
    data["agents"][" primary "] = {
        "adapter": "codex",
        "command": ["unsafe-shadow"],
        "permission": "danger_full_access",
    }

    with pytest.raises(ValidationError, match="normalize to the same safe id"):
        DebateConfig.model_validate(data)


def test_missing_prompts_and_workspace_fail_during_file_load(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    (tmp_path / "prompts/architect.md").unlink()

    with pytest.raises(ConfigError, match="prompt file"):
        load_config(config_path)

    data = _valid_data()
    data["run"]["workspace"] = "does-not-exist"
    with pytest.raises(ConfigError, match="workspace directory"):
        load_config(_write_config(tmp_path / "other", data))


def test_output_dir_cannot_be_an_existing_file(tmp_path: Path) -> None:
    data = _valid_data()
    data["run"]["output_dir"] = "occupied"
    config_path = _write_config(tmp_path, data)
    (tmp_path / "occupied").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match="output_dir exists but is not a directory"):
        load_config(config_path)


def test_configured_path_expansion_errors_are_wrapped(tmp_path: Path) -> None:
    data = _valid_data()
    data["workflow"]["judge"]["prompt"] = (
        "~__agent_debate_engine_user_that_does_not_exist__/judge.md"
    )

    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(_write_config(tmp_path, data))


def test_unsafe_agents_reports_every_non_read_only_agent() -> None:
    config = DebateConfig.model_validate(_valid_data())
    assert config.unsafe_agents() == ("judge",)
    assert config.is_unsafe

    agents = dict(config.agents)
    agents["primary"] = agents["primary"].model_copy(
        update={"permission": PermissionMode.DANGER_FULL_ACCESS}
    )
    config = config.model_copy(update={"agents": agents})
    assert config.unsafe_agents() == ("judge", "primary")


def test_generic_agent_is_unsafe_even_when_labeled_read_only() -> None:
    data = _valid_data()
    data["agents"]["judge"]["permission"] = "read_only"

    config = DebateConfig.model_validate(data)

    assert config.unsafe_agents() == ("judge",)
    assert config.is_unsafe


def test_config_models_are_immutable_and_resolved_copies_do_not_alias(tmp_path: Path) -> None:
    _write_config(tmp_path)
    original = DebateConfig.model_validate(_valid_data())
    resolved = original.resolved(relative_to=tmp_path)

    assert resolved.agents is not original.agents
    assert resolved.agents["primary"] is not original.agents["primary"]
    assert resolved.context is not original.context
    assert resolved.failure is not original.failure
    assert resolved.workflow.stop is not original.workflow.stop

    mutable_agents = cast(dict[str, AgentConfig], resolved.agents)
    with pytest.raises(TypeError):
        mutable_agents["new"] = resolved.agents["primary"]
    with pytest.raises(ValidationError, match="frozen"):
        resolved.run.max_parallel = 8
    with pytest.raises(ValidationError, match="min_rounds"):
        resolved.workflow.stop.model_copy(
            update={
                "min_rounds": 10,
                "max_rounds": 2,
            }
        )
    assert resolved.workflow.stop.min_rounds == 1


def test_loaded_config_supports_validated_deep_copy(tmp_path: Path) -> None:
    loaded = load_config(_write_config(tmp_path))

    copied = loaded.model_copy(deep=True)

    assert copied == loaded
    assert copied is not loaded
    assert copied.agents is not loaded.agents
    assert copied.source_path == loaded.source_path


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"min_rounds": 4, "max_rounds": 3}, "min_rounds"),
        ({"max_rounds": 3, "stable_rounds": 4}, "stable_rounds"),
        ({"confidence_threshold": 1.1}, "less than or equal to 1"),
        ({"max_elapsed_seconds": 0}, "greater than 0"),
    ],
)
def test_stop_ranges_are_validated(values: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        StopConfig.model_validate(values)


def test_context_budget_relationships_and_marker_floor_are_validated() -> None:
    with pytest.raises(ValidationError, match="max_requirement_chars"):
        ContextConfig(max_prompt_chars=100, max_requirement_chars=101)
    with pytest.raises(ValidationError, match="greater than or equal to 11"):
        ContextConfig(max_response_chars=10)
