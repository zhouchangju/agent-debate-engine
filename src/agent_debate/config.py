"""Strict YAML configuration schema and config-relative path loading."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    ConfigDict,
    Field,
    FiniteFloat,
    PrivateAttr,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from agent_debate.errors import ConfigError
from agent_debate.models import PermissionMode, SafeId, StrictModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MAX_AGENTS = 64
MAX_STAGES = 32
MAX_PARTICIPANTS_PER_STAGE = 32
MAX_ROUNDS = 100
MAX_PARALLEL = 32
MAX_ELAPSED_SECONDS = 86_400.0
MAX_PROMPT_CHARS = 1_000_000
MAX_OUTPUT_CHARS = 10_000_000


def _require_ordered(value: object, field_name: str) -> object:
    if not isinstance(value, (list, tuple)):
        raise PydanticCustomError(
            "ordered_sequence_type",
            f"{field_name} must be an ordered list or tuple",
        )
    return value


class _ConfigModel(StrictModel):
    """Base model shared by every closed configuration section."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class AgentAdapter(StrEnum):
    """Supported process adapter."""

    CODEX = "codex"
    KIMI = "kimi"
    GENERIC = "generic"


AdapterKind = AgentAdapter
AdapterType = AgentAdapter


class PromptTransport(StrEnum):
    """How the generic adapter supplies the prompt to an executable."""

    STDIN = "stdin"
    ARGUMENT = "argument"
    FLAG = "flag"


class StageMode(StrEnum):
    """Execution order for participants inside a workflow stage."""

    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class AgentErrorPolicy(StrEnum):
    """Action after an agent invocation fails."""

    ABORT = "abort"
    CONTINUE = "continue"


class JudgeErrorPolicy(StrEnum):
    """Action after the Judge response fails protocol validation."""

    ABORT = "abort"
    RETRY = "retry"


class AgentConfig(_ConfigModel):
    """Executable and resource limits for one named agent."""

    adapter: AgentAdapter
    command: tuple[str, ...] = Field(min_length=1)
    model: NonEmptyStr | None = None
    permission: PermissionMode = PermissionMode.READ_ONLY
    extra_args: tuple[str, ...] = ()
    timeout: FiniteFloat = Field(default=300.0, gt=0, strict=True)
    max_output: int = Field(
        default=100_000,
        gt=0,
        le=MAX_OUTPUT_CHARS,
        strict=True,
    )
    retries: int = Field(default=0, ge=0, le=5, strict=True)
    prompt_transport: PromptTransport | None = None
    prompt_flag: NonEmptyStr | None = None

    @field_validator("command", "extra_args", mode="before")
    @classmethod
    def require_ordered_argv(cls, value: object) -> object:
        return _require_ordered(value, "argv")

    @field_validator("command", "extra_args")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for argument in value:
            if not argument or "\x00" in argument:
                raise ValueError("argv entries must be non-empty and NUL-free")
        return value

    @field_validator("model", "prompt_flag")
    @classmethod
    def validate_nul_free_metadata(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("model and prompt_flag must not contain NUL")
        return value

    @model_validator(mode="after")
    def validate_transport(self) -> AgentConfig:
        if self.adapter in {AgentAdapter.CODEX, AgentAdapter.KIMI}:
            if len(self.command) != 1:
                raise ValueError(
                    f"{self.adapter.value} command must contain exactly one executable; "
                    "use the generic adapter for an externally sandboxed wrapper"
                )
            if self.extra_args:
                raise ValueError(
                    f"{self.adapter.value} extra_args are disabled because built-in "
                    "adapters own their complete provider argv"
                )
        if self.adapter is AgentAdapter.GENERIC:
            if self.prompt_transport is None:
                raise ValueError("generic agents require prompt_transport")
            if self.prompt_transport is PromptTransport.FLAG:
                if self.prompt_flag is None:
                    raise ValueError("flag prompt transport requires prompt_flag")
                if not self.prompt_flag.startswith("-"):
                    raise ValueError("prompt_flag must be an argv flag beginning with '-'")
            elif self.prompt_flag is not None:
                raise ValueError("prompt_flag is only valid with flag prompt transport")
        elif self.prompt_transport is not None or self.prompt_flag is not None:
            raise ValueError("prompt_transport and prompt_flag are only valid for generic agents")
        if (
            self.adapter is AgentAdapter.KIMI
            and self.permission is not PermissionMode.DANGER_FULL_ACCESS
        ):
            raise ValueError(
                "Kimi Code CLI 0.29.1 prompt mode forces auto permission; "
                "Kimi agents must declare permission: danger_full_access"
            )
        return self

    @property
    def executable(self) -> str:
        """Executable argv element used by preflight and adapters."""

        return self.command[0]

    @property
    def timeout_seconds(self) -> float:
        """Explicit unit-bearing alias for adapter protocols."""

        return float(self.timeout)

    @property
    def max_output_chars(self) -> int:
        """Explicit unit-bearing alias for adapter protocols."""

        return self.max_output


class ParticipantConfig(_ConfigModel):
    """One role invocation within a workflow stage."""

    id: SafeId
    agent: SafeId
    prompt: Path
    label: NonEmptyStr | None = None


class StageConfig(_ConfigModel):
    """A workflow barrier containing parallel or sequential participants."""

    id: SafeId
    mode: StageMode = StageMode.PARALLEL
    participants: tuple[ParticipantConfig, ...] = Field(
        min_length=1,
        max_length=MAX_PARTICIPANTS_PER_STAGE,
    )

    @field_validator("participants", mode="before")
    @classmethod
    def require_ordered_participants(cls, value: object) -> object:
        return _require_ordered(value, "participants")

    @model_validator(mode="after")
    def validate_participant_ids(self) -> StageConfig:
        participant_ids = [participant.id for participant in self.participants]
        if len(participant_ids) != len(set(participant_ids)):
            raise ValueError(f"stage {self.id!r} has duplicate participant ids")
        return self


class JudgeConfig(_ConfigModel):
    """Agent and role prompt used for the structured Judge invocation."""

    agent: SafeId
    prompt: Path


class StopConfig(_ConfigModel):
    """Deterministic debate stopping bounds."""

    min_rounds: int = Field(default=2, ge=1, strict=True)
    max_rounds: int = Field(default=6, ge=1, le=MAX_ROUNDS, strict=True)
    confidence_threshold: FiniteFloat = Field(default=0.8, ge=0, le=1, strict=True)
    stable_rounds: int = Field(default=2, ge=1, strict=True)
    max_elapsed_seconds: FiniteFloat = Field(
        default=1_800.0,
        gt=0,
        le=MAX_ELAPSED_SECONDS,
        strict=True,
    )

    @model_validator(mode="after")
    def validate_round_bounds(self) -> StopConfig:
        if self.min_rounds > self.max_rounds:
            raise ValueError("min_rounds must not exceed max_rounds")
        if self.stable_rounds > self.max_rounds:
            raise ValueError("stable_rounds must not exceed max_rounds")
        return self


class WorkflowConfig(_ConfigModel):
    """Ordered stages followed by one structured Judge."""

    stages: tuple[StageConfig, ...] = Field(min_length=1, max_length=MAX_STAGES)
    judge: JudgeConfig
    stop: StopConfig = Field(default_factory=StopConfig)

    @field_validator("stages", mode="before")
    @classmethod
    def require_ordered_stages(cls, value: object) -> object:
        return _require_ordered(value, "stages")

    @model_validator(mode="after")
    def validate_stage_ids(self) -> WorkflowConfig:
        stage_ids = [stage.id for stage in self.stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("workflow stage ids must be unique")
        return self


class ContextConfig(_ConfigModel):
    """Character budgets for immutable requirements and debate evidence."""

    max_prompt_chars: int = Field(
        default=24_000,
        gt=0,
        le=MAX_PROMPT_CHARS,
        strict=True,
    )
    max_requirement_chars: int = Field(
        default=8_000,
        gt=0,
        le=MAX_PROMPT_CHARS,
        strict=True,
    )
    max_response_chars: int = Field(
        default=8_000,
        ge=11,
        le=MAX_PROMPT_CHARS,
        strict=True,
    )
    keep_recent_rounds: int = Field(default=2, ge=0, le=MAX_ROUNDS, strict=True)

    @model_validator(mode="after")
    def validate_budget_relationships(self) -> ContextConfig:
        if self.max_requirement_chars > self.max_prompt_chars:
            raise ValueError("max_requirement_chars must not exceed max_prompt_chars")
        if self.max_response_chars > self.max_prompt_chars:
            raise ValueError("max_response_chars must not exceed max_prompt_chars")
        return self


class FailurePolicyConfig(_ConfigModel):
    """Explicit failure and Judge schema-repair policy."""

    on_agent_error: AgentErrorPolicy = AgentErrorPolicy.ABORT
    on_judge_error: JudgeErrorPolicy = JudgeErrorPolicy.RETRY
    require_all_participants: bool = Field(default=True, strict=True)
    schema_repair_attempts: Literal[0, 1] = 1

    @field_validator("schema_repair_attempts", mode="before")
    @classmethod
    def reject_coerced_repair_attempts(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_repair_attempts must be the integer 0 or 1")
        return value


FailureConfig = FailurePolicyConfig


class RunConfig(_ConfigModel):
    """Filesystem and concurrency settings for one run."""

    output_dir: Path = Path(".agent-debate/runs")
    workspace: Path = Path()
    max_parallel: int = Field(default=4, ge=1, le=MAX_PARALLEL, strict=True)
    stream: bool = Field(default=True, strict=True)


class DebateConfig(_ConfigModel):
    """Complete schema-v1 debate configuration."""

    schema_version: Literal[1]
    run: RunConfig = Field(default_factory=RunConfig)
    agents: Mapping[SafeId, AgentConfig] = Field(min_length=1, max_length=MAX_AGENTS)
    workflow: WorkflowConfig
    context: ContextConfig = Field(default_factory=ContextConfig)
    failure: FailurePolicyConfig = Field(default_factory=FailurePolicyConfig)

    _source_path: Path | None = PrivateAttr(default=None)

    @field_validator("agents", mode="before")
    @classmethod
    def reject_normalized_agent_id_collisions(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        normalized: dict[str, object] = {}
        for key in value:
            if not isinstance(key, str):
                continue
            canonical = key.strip()
            previous = normalized.get(canonical)
            if previous is not None and previous != key:
                raise ValueError(
                    f"agent ids {previous!r} and {key!r} normalize to the same safe id"
                )
            normalized[canonical] = key
        return value

    @field_validator("agents")
    @classmethod
    def freeze_agents(
        cls,
        value: Mapping[str, AgentConfig],
    ) -> Mapping[str, AgentConfig]:
        return MappingProxyType(dict(value))

    @field_serializer("agents")
    def serialize_agents(
        self,
        value: Mapping[str, AgentConfig],
    ) -> dict[str, AgentConfig]:
        return dict(value)

    @field_validator("schema_version", mode="before")
    @classmethod
    def reject_coerced_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_agent_references(self) -> DebateConfig:
        known_agents = set(self.agents)
        unknown = [
            (f"stage {stage.id!r} participant {participant.id!r}: {participant.agent!r}")
            for stage in self.workflow.stages
            for participant in stage.participants
            if participant.agent not in known_agents
        ]
        if self.workflow.judge.agent not in known_agents:
            unknown.append(f"judge: {self.workflow.judge.agent!r}")
        if unknown:
            raise ValueError("unknown agent reference(s): " + "; ".join(unknown))
        return self

    @property
    def source_path(self) -> Path | None:
        """Absolute YAML path used to load this instance, if any."""

        return self._source_path

    def unsafe_agents(self) -> tuple[str, ...]:
        """Return every agent needing a separate ``--allow-unsafe`` acknowledgement."""

        return tuple(
            sorted(
                agent_id
                for agent_id, agent in self.agents.items()
                if agent.adapter is AgentAdapter.GENERIC or agent.permission.is_unsafe
            )
        )

    @property
    def is_unsafe(self) -> bool:
        """Whether any provider needs an explicit unsafe acknowledgement."""

        return bool(self.unsafe_agents())

    def resolved(self, *, relative_to: Path, source_path: Path | None = None) -> DebateConfig:
        """Return a copy with all configured paths made absolute."""

        base = relative_to.expanduser().resolve()

        def resolve(path: Path) -> Path:
            expanded = path.expanduser()
            return (expanded if expanded.is_absolute() else base / expanded).resolve()

        data = self.model_dump(mode="python")
        data["run"]["output_dir"] = resolve(self.run.output_dir)
        data["run"]["workspace"] = resolve(self.run.workspace)
        for stage_data, stage in zip(
            data["workflow"]["stages"],
            self.workflow.stages,
            strict=True,
        ):
            for participant_data, participant in zip(
                stage_data["participants"],
                stage.participants,
                strict=True,
            ):
                participant_data["prompt"] = resolve(participant.prompt)
        data["workflow"]["judge"]["prompt"] = resolve(self.workflow.judge.prompt)

        result = DebateConfig.model_validate(data)
        result._source_path = source_path.resolve() if source_path is not None else None
        result._validate_resolved_paths()
        return result

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> DebateConfig:
        """Preserve source provenance while atomically validating updates."""

        result = super().model_copy(update=update, deep=deep)
        result._source_path = self._source_path
        return result

    def _validate_resolved_paths(self) -> None:
        if not self.run.workspace.is_dir():
            raise ValueError(f"workspace directory does not exist: {self.run.workspace}")
        if self.run.output_dir.exists() and not self.run.output_dir.is_dir():
            raise ValueError(f"output_dir exists but is not a directory: {self.run.output_dir}")

        missing_prompts = [
            participant.prompt
            for stage in self.workflow.stages
            for participant in stage.participants
            if not participant.prompt.is_file()
        ]
        if not self.workflow.judge.prompt.is_file():
            missing_prompts.append(self.workflow.judge.prompt)
        if missing_prompts:
            rendered = ", ".join(str(path) for path in dict.fromkeys(missing_prompts))
            raise ValueError(f"prompt file(s) do not exist: {rendered}")

    @classmethod
    def from_file(cls, path: str | Path) -> DebateConfig:
        """Load a YAML configuration through the public error boundary."""

        return load_config(path)


_UniqueKeyLoader: Any = type("_UniqueKeyLoader", (yaml.SafeLoader,), {})


def _construct_unique_mapping(
    loader: Any,
    node: Any,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_config(path: str | Path) -> DebateConfig:
    """Read, validate, and resolve a schema-v1 YAML configuration."""

    try:
        source = Path(path).expanduser().resolve()
        text = source.read_text(encoding="utf-8")
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"Could not read configuration {path}: {exc}") from exc

    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse configuration {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration {source} must contain a YAML mapping")

    try:
        config = DebateConfig.model_validate(raw)
        return config.resolved(relative_to=source.parent, source_path=source)
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        raise ConfigError(f"Invalid configuration {source}: {exc}") from exc


__all__ = [
    "AdapterKind",
    "AdapterType",
    "AgentAdapter",
    "AgentConfig",
    "AgentErrorPolicy",
    "ContextConfig",
    "DebateConfig",
    "FailureConfig",
    "FailurePolicyConfig",
    "JudgeConfig",
    "JudgeErrorPolicy",
    "ParticipantConfig",
    "PermissionMode",
    "PromptTransport",
    "RunConfig",
    "StageConfig",
    "StageMode",
    "StopConfig",
    "WorkflowConfig",
    "load_config",
]
