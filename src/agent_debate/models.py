"""Strict domain and wire-protocol models for debate execution."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

_WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CLOCK$",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def ensure_portable_path_component(value: str) -> str:
    """Reject names that become device paths on Windows filesystems."""

    windows_stem = value.rstrip(". ").split(".", maxsplit=1)[0].upper()
    if windows_stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"reserved path component: {value!r}")
    return value


SafeId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    ),
    AfterValidator(ensure_portable_path_component),
]
RunId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Hex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]


def _reject_numeric_datetime(value: object) -> object:
    if isinstance(value, (int, float)):
        raise PydanticCustomError(
            "aware_datetime_type",
            "timestamp must be an ISO 8601 string or an aware datetime",
        )
    return value


def _normalize_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _require_ordered(value: object, field_name: str) -> object:
    if not isinstance(value, (list, tuple)):
        raise PydanticCustomError(
            "ordered_sequence_type",
            f"{field_name} must be an ordered list or tuple",
        )
    return value


UtcTimestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_reject_numeric_datetime),
    AfterValidator(_normalize_utc),
]


class StrictModel(BaseModel):
    """Base class for persisted models with closed schemas."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Copy atomically, revalidating every requested field update."""

        if not update and not deep:
            return super().model_copy(deep=deep)
        data = self.model_dump(mode="python", round_trip=True)
        if update:
            data.update(update)
        return type(self).model_validate(data)


class PermissionMode(StrEnum):
    """Provider permission level requested for one invocation."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER_FULL_ACCESS = "danger_full_access"

    @property
    def is_unsafe(self) -> bool:
        """Whether the mode requires a separate runtime acknowledgement."""

        return self is not PermissionMode.READ_ONLY


class InvocationStatus(StrEnum):
    """Terminal outcome of one agent process invocation."""

    SUCCESS = "success"
    SUCCEEDED = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"
    CANCELLED = "cancelled"


class AgentRequest(StrictModel):
    """Complete, serializable input contract for an agent adapter."""

    schema_version: Literal[1] = 1
    agent_id: SafeId
    role_id: SafeId
    prompt: str
    cwd: Path
    final_output_path: Path | None = None
    output_schema_path: Path | None = None
    run_id: RunId | None = None
    round_number: int | None = Field(default=None, ge=1, strict=True)
    stage_id: SafeId | None = None
    timeout_seconds: FiniteFloat = Field(default=300.0, gt=0, strict=True)
    max_output_chars: int = Field(default=100_000, gt=0, strict=True)
    max_final_output_chars: int = Field(default=20_000, gt=0, strict=True)
    model: NonEmptyText | None = None
    model_reasoning_effort: NonEmptyText | None = None
    reasoning_effort: NonEmptyText | None = None
    permission: PermissionMode = PermissionMode.READ_ONLY
    extra_args: tuple[str, ...] = ()

    @field_validator("extra_args", mode="before")
    @classmethod
    def require_ordered_extra_args(cls, value: object) -> object:
        return _require_ordered(value, "extra_args")

    @field_validator("schema_version", mode="before")
    @classmethod
    def reject_coerced_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """Reject empty prompts without altering their significant whitespace."""

        if not value.strip():
            raise ValueError("prompt must contain non-whitespace text")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("model must not contain NUL")
        return value

    @field_validator("model_reasoning_effort", "reasoning_effort")
    @classmethod
    def validate_reasoning_effort_fields(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("reasoning effort must not contain NUL")
        return value

    @field_validator("extra_args")
    @classmethod
    def validate_extra_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for argument in value:
            if not argument or "\x00" in argument:
                raise ValueError("extra_args must contain non-empty, NUL-free argv entries")
        return value

    @property
    def workspace(self) -> Path:
        """Compatibility name used by adapter protocols."""

        return self.cwd


class AgentResult(StrictModel):
    """Auditable result of one agent process invocation."""

    schema_version: Literal[1] = 1
    agent_id: SafeId
    role_id: SafeId
    status: InvocationStatus
    stdout: str
    stderr: str
    final_output: str | None
    exit_code: int | None = Field(strict=True)
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    duration_seconds: FiniteFloat = Field(ge=0, strict=True)
    timed_out: bool = Field(strict=True)
    truncated: bool = Field(strict=True)
    transport_truncated: bool = Field(default=False, strict=True)
    transport_observed_chars: int = Field(default=0, ge=0, strict=True)
    display_command: tuple[str, ...] = Field(min_length=1)
    input_hash: Sha256Hex
    output_hash: Sha256Hex
    provider_adapter: NonEmptyText = "unknown"
    provider_model: NonEmptyText | None = None
    session_mode: Literal["fresh", "unverified"] = "unverified"
    session_enforcement: NonEmptyText = "adapter did not declare session isolation"

    @field_validator("display_command", mode="before")
    @classmethod
    def require_ordered_display_command(cls, value: object) -> object:
        return _require_ordered(value, "display_command")

    @field_validator("schema_version", mode="before")
    @classmethod
    def reject_coerced_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("display_command")
    @classmethod
    def validate_display_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for argument in value:
            if not argument or "\x00" in argument:
                raise ValueError("display_command must contain non-empty, NUL-free argv entries")
        return value

    @model_validator(mode="after")
    def validate_result_semantics(self) -> AgentResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.timed_out != (self.status is InvocationStatus.TIMED_OUT):
            raise ValueError("timed_out must be true exactly when status is timed_out")
        if self.status is InvocationStatus.SUCCESS and self.exit_code != 0:
            raise ValueError("a successful invocation must have exit_code 0")
        if self.status is InvocationStatus.SUCCESS and (
            self.final_output is None or not self.final_output.strip()
        ):
            raise ValueError("a successful invocation must have non-empty final_output")
        if self.truncated != (self.status is InvocationStatus.OUTPUT_LIMIT):
            raise ValueError("truncated must be true exactly when status is output_limit")
        captured_transport_chars = len(self.stdout) + len(self.stderr)
        if self.transport_truncated and self.transport_observed_chars <= captured_transport_chars:
            raise ValueError(
                "transport_observed_chars must exceed captured transport output "
                "when transport_truncated is true"
            )
        return self


class JudgeVerdict(StrEnum):
    """Structured Judge recommendation."""

    CONTINUE = "continue"
    FINALIZE = "finalize"
    BLOCKED = "blocked"


class IssueSeverity(StrEnum):
    """Materiality of an unresolved Judge issue."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class UnresolvedIssue(StrictModel):
    """One stable issue carried in the Judge ledger."""

    id: NonEmptyText
    severity: IssueSeverity
    summary: NonEmptyText


class JudgeDecision(StrictModel):
    """Judge schema v1 plus cross-field semantic guarantees."""

    schema_version: Literal[1]
    verdict: JudgeVerdict
    confidence: FiniteFloat = Field(ge=0, le=1, strict=True)
    rationale: NonEmptyText
    synthesis: NonEmptyText
    accepted_decisions: tuple[str, ...]
    rejected_options: tuple[str, ...]
    unresolved_issues: tuple[UnresolvedIssue, ...]
    next_round_focus: tuple[str, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def reject_coerced_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator(
        "accepted_decisions",
        "rejected_options",
        "unresolved_issues",
        "next_round_focus",
        mode="before",
    )
    @classmethod
    def require_ordered_arrays(cls, value: object) -> object:
        return _require_ordered(value, "Judge arrays")

    @field_validator("accepted_decisions", "rejected_options", "next_round_focus")
    @classmethod
    def validate_text_arrays(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("decision arrays may not contain blank strings")
        return value

    @model_validator(mode="after")
    def validate_decision_semantics(self) -> JudgeDecision:
        issue_ids = [issue.id for issue in self.unresolved_issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("unresolved issue ids must be unique")

        critical = any(issue.severity is IssueSeverity.CRITICAL for issue in self.unresolved_issues)
        if self.verdict is JudgeVerdict.FINALIZE and critical:
            raise ValueError("finalize cannot contain a critical unresolved issue")
        if self.verdict is JudgeVerdict.CONTINUE and not self.next_round_focus:
            raise ValueError("continue requires at least one next_round_focus item")
        if self.verdict is JudgeVerdict.BLOCKED and not critical:
            raise ValueError("blocked requires at least one critical unresolved issue")
        return self


class RunStatus(StrEnum):
    """Lifecycle status persisted in a run manifest."""

    PENDING = "pending"
    RUNNING = "running"
    FINALIZED = "finalized"
    EXHAUSTED = "exhausted"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RoundStatus(StrEnum):
    """Lifecycle status persisted for one debate round."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RoundSummary(StrictModel):
    """JSON-safe persisted summary for one debate round."""

    round_number: int = Field(ge=1, strict=True)
    status: RoundStatus
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    agent_results: tuple[AgentResult, ...] = ()
    judge_decision: JudgeDecision | None = None
    error: NonEmptyText | None = None

    @field_validator("agent_results", mode="before")
    @classmethod
    def require_ordered_agent_results(cls, value: object) -> object:
        return _require_ordered(value, "agent_results")

    @model_validator(mode="after")
    def validate_timestamps(self) -> RoundSummary:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return a representation containing JSON-native values only."""

        return self.model_dump(mode="json")


class RunSummary(StrictModel):
    """JSON-safe persisted summary for an entire debate run."""

    schema_version: Literal[1] = 1
    run_id: RunId
    status: RunStatus
    started_at: UtcTimestamp
    updated_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    rounds: tuple[RoundSummary, ...] = ()
    final_synthesis: str | None = None
    error: NonEmptyText | None = None

    @field_validator("rounds", mode="before")
    @classmethod
    def require_ordered_rounds(cls, value: object) -> object:
        return _require_ordered(value, "rounds")

    @field_validator("schema_version", mode="before")
    @classmethod
    def reject_coerced_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_summary_semantics(self) -> RunSummary:
        if self.updated_at < self.started_at:
            raise ValueError("updated_at must not precede started_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        round_numbers = [round_.round_number for round_ in self.rounds]
        if round_numbers != sorted(round_numbers) or len(round_numbers) != len(set(round_numbers)):
            raise ValueError("rounds must have unique, increasing round_number values")
        if self.final_synthesis is not None and not self.final_synthesis.strip():
            raise ValueError("final_synthesis may not be blank")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return a representation containing JSON-native values only."""

        return self.model_dump(mode="json")


__all__ = [
    "AgentRequest",
    "AgentResult",
    "InvocationStatus",
    "IssueSeverity",
    "JudgeDecision",
    "JudgeVerdict",
    "PermissionMode",
    "RoundStatus",
    "RoundSummary",
    "RunId",
    "RunStatus",
    "RunSummary",
    "SafeId",
    "StrictModel",
    "UnresolvedIssue",
    "UtcTimestamp",
]
