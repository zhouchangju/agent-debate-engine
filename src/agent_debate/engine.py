"""State-machine orchestration for structured local-agent debates."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from agent_debate import __version__
from agent_debate.adapters.base import StreamName, redact_display_argv
from agent_debate.adapters.process import (
    ProcessOutputLimitError,
    ProcessResult,
    ProcessTimeoutError,
)
from agent_debate.adapters.registry import get_adapter
from agent_debate.artifacts import ArtifactStore, content_sha256
from agent_debate.config import (
    AgentAdapter,
    AgentConfig,
    AgentErrorPolicy,
    DebateConfig,
    JudgeErrorPolicy,
    ParticipantConfig,
    StageConfig,
    StageMode,
)
from agent_debate.context import ContextEvidence, JudgeContextState, build_context
from agent_debate.errors import (
    AgentExecutionError,
    ConfigError,
    DebateError,
    JudgeProtocolError,
    ResumeError,
    UnsafeConfigurationError,
)
from agent_debate.judge import parse_judge_response
from agent_debate.models import (
    AgentRequest,
    AgentResult,
    InvocationStatus,
    JudgeDecision,
    PermissionMode,
)
from agent_debate.preflight import AgentDiagnostic, diagnose_agents, require_healthy
from agent_debate.reporting import (
    FinalReportData,
    render_evidence_report,
    render_final_report,
)
from agent_debate.result_document import build_result_document
from agent_debate.stop import StopDecision, StopOutcome, evaluate_stop

StreamHandler = Callable[[str, StreamName, str], Awaitable[None] | None]
_PARALLEL_WRITE_CONFLICT_MIN_PARTICIPANTS = 2
_ENGINE_STATE_DIRECTORY = "agent-debate-engine"
_PROVIDER_SCRATCH_DIRECTORY = "provider-scratch"
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Terminal result returned to CLI and embedding callers."""

    run_id: str
    run_dir: Path
    status: str
    stop_reason: str
    rounds_completed: int
    final_report: str

    @property
    def converged(self) -> bool:
        """Whether deterministic convergence criteria were satisfied."""

        return self.status == StopOutcome.FINALIZED.value


@dataclass(frozen=True, slots=True)
class _InvocationOutcome:
    result: AgentResult
    error: DebateError | None


class EmptyAgentOutputError(AgentExecutionError):
    """A process succeeded without producing a usable final response."""


class GlobalTimeLimitError(AgentExecutionError):
    """No time remains in the configured run-level wall-clock budget."""


class DebateEngine:
    """Run a validated configuration as a bounded, auditable state machine."""

    def __init__(
        self,
        config: DebateConfig,
        *,
        allow_unsafe: bool = False,
        stream_handler: StreamHandler | None = None,
        run_preflight: bool = True,
    ) -> None:
        self.config = config
        self.allow_unsafe = allow_unsafe
        self.stream_handler = stream_handler
        self.run_preflight = run_preflight
        self._semaphore = asyncio.Semaphore(config.run.max_parallel)
        self._clock_started = 0.0
        self._base_elapsed_seconds = 0.0
        self._validate_security()

    async def run(self, task: str) -> EngineResult:
        """Start a new run and return its terminal result."""

        requirement = task.strip()
        if not requirement:
            raise ConfigError("The debate task must contain non-whitespace text.")
        if len(requirement) > self.config.context.max_requirement_chars:
            raise ConfigError(
                "The debate task exceeds context.max_requirement_chars "
                f"({self.config.context.max_requirement_chars} characters)."
            )

        config_snapshot = self.config.model_dump(mode="json")
        store = ArtifactStore.create(
            self.config.run.output_dir,
            config=config_snapshot,
            request=requirement,
        )
        self._clock_started = time.monotonic()
        self._base_elapsed_seconds = 0.0
        try:
            diagnostics = await self._preflight()
            self._initialize_manifest(store, requirement, diagnostics)
            return await self._execute(
                store=store,
                task=requirement,
                start_round=1,
                prior_decisions=[],
                prior_evidence=[],
            )
        except asyncio.CancelledError:
            self._record_cancelled(store)
            raise
        except BaseException as exc:
            if store.manifest.get("status") != "failed":
                self._record_failure(store, exc)
            raise
        finally:
            store.close()

    async def resume(
        self,
        run_dir: str | Path,
        *,
        retry_failed: bool = False,
    ) -> EngineResult:
        """Resume after the last valid Judge barrier.

        Completed Judge rounds are reused. An incomplete round is rerun in full,
        which is explicit through ``retry_failed`` for failed terminal runs.
        """

        store = ArtifactStore.load_existing(run_dir)
        return await self._resume_open_store(store, retry_failed=retry_failed)

    async def _resume_open_store(
        self,
        store: ArtifactStore,
        *,
        retry_failed: bool,
    ) -> EngineResult:
        """Resume a store that is already locked, schema-checked, and hash-verified."""

        execution_started = False
        try:
            manifest = store.manifest
            _validate_resume_status(manifest, retry_failed=retry_failed)
            self._verify_resume_inputs(store)
            try:
                task = store.read_artifact_text("request.md").strip()
            except DebateError as exc:
                raise ResumeError(
                    f"Cannot restore debate request from the verified run artifact: {exc}"
                ) from exc
            decisions = self._load_prior_decisions(store)
            evidence = self._load_prior_evidence(
                store,
                completed_rounds=len(decisions),
            )
            start_round = len(decisions) + 1
            self._base_elapsed_seconds = float(manifest.get("elapsed_seconds", 0.0))
            self._clock_started = time.monotonic()
            diagnostics = await self._preflight()
            store.mark_resumed()
            execution_started = True
            store.update_manifest(
                status="running",
                error=None,
                finished_at=None,
                round_count=len(decisions),
                rounds=_restored_round_summaries(manifest, decisions),
                diagnostics=[_diagnostic_dict(item) for item in diagnostics],
                elapsed_seconds=self._elapsed_seconds(),
            )
            store.append_event(
                "resume_execution_started",
                {"start_round": start_round, "retry_failed": retry_failed},
            )
            return await self._execute(
                store=store,
                task=task,
                start_round=start_round,
                prior_decisions=decisions,
                prior_evidence=evidence,
            )
        except asyncio.CancelledError:
            if execution_started:
                self._record_cancelled(store)
            raise
        except BaseException as exc:
            if execution_started and store.manifest.get("status") != "failed":
                self._record_failure(store, exc)
            raise
        finally:
            store.close()

    async def _preflight(self) -> list[AgentDiagnostic]:
        if not self.run_preflight:
            return []
        diagnostics = await diagnose_agents(
            self.config.agents,
            cwd=self.config.run.workspace,
        )
        require_healthy(diagnostics)
        return diagnostics

    def _validate_security(self) -> None:
        unsafe = self.config.unsafe_agents()
        if unsafe and not self.allow_unsafe:
            rendered = ", ".join(unsafe)
            raise UnsafeConfigurationError(
                "Non-read-only agent permissions require a second runtime acknowledgement "
                f"(--allow-unsafe). Unsafe agents: {rendered}"
            )

        for stage in self.config.workflow.stages:
            if (
                stage.mode is not StageMode.PARALLEL
                or len(stage.participants) < _PARALLEL_WRITE_CONFLICT_MIN_PARTICIPANTS
            ):
                continue
            write_capable = [
                participant.id
                for participant in stage.participants
                if (
                    self.config.agents[participant.agent].adapter is AgentAdapter.GENERIC
                    or self.config.agents[participant.agent].permission
                    is not PermissionMode.READ_ONLY
                )
            ]
            if write_capable:
                rendered = ", ".join(write_capable)
                raise UnsafeConfigurationError(
                    f"Parallel stage {stage.id!r} contains agents whose read-only boundary "
                    f"cannot be enforced ({rendered}). Use trusted built-in read-only agents "
                    "or sequential externally contained execution."
                )

    def _initialize_manifest(
        self,
        store: ArtifactStore,
        task: str,
        diagnostics: Sequence[AgentDiagnostic],
    ) -> None:
        serialized = self.config.model_dump(mode="json")
        config_text = json.dumps(serialized, ensure_ascii=False, sort_keys=True)
        prompt_hashes = {
            str(path): content_sha256(path.read_bytes()) for path in self._prompt_paths()
        }
        store.update_manifest(
            status="running",
            engine_version=__version__,
            config_source=(
                str(self.config.source_path) if self.config.source_path is not None else None
            ),
            config_hash=content_sha256(config_text),
            task_hash=content_sha256(task),
            prompt_hashes=prompt_hashes,
            diagnostics=[_diagnostic_dict(item) for item in diagnostics],
            workspace=_workspace_metadata(self.config.run.workspace),
            outcome=None,
            stop_reason=None,
            elapsed_seconds=0.0,
            round_count=0,
        )
        store.append_event(
            "execution_started",
            {
                "engine_version": __version__,
                "configured_agents": sorted(self.config.agents),
            },
        )

    async def _execute(
        self,
        *,
        store: ArtifactStore,
        task: str,
        start_round: int,
        prior_decisions: list[JudgeDecision],
        prior_evidence: list[ContextEvidence],
    ) -> EngineResult:
        last_decision = prior_decisions[-1] if prior_decisions else None
        rounds_completed = len(prior_decisions)

        if last_decision is not None:
            previous_stop = evaluate_stop(
                rounds_completed,
                self._elapsed_seconds(),
                last_decision,
                prior_decisions=prior_decisions[:-1],
                policy=self.config.workflow.stop,
            )
            if previous_stop.should_stop:
                return self._finalize(
                    store,
                    task=task,
                    stop=previous_stop,
                    decision=last_decision,
                    rounds_completed=rounds_completed,
                )

        for round_number in range(start_round, self.config.workflow.stop.max_rounds + 1):
            if self._remaining_seconds() <= 0:
                return self._finalize(
                    store,
                    task=task,
                    stop=StopDecision(
                        True,
                        StopOutcome.TIMED_OUT,
                        "maximum elapsed time reached before the next round",
                    ),
                    decision=last_decision,
                    rounds_completed=rounds_completed,
                )

            store.append_event("round_started", {"round_number": round_number})
            store.update_manifest(
                current_round=round_number,
                elapsed_seconds=self._elapsed_seconds(),
            )
            round_evidence: list[ContextEvidence] = []
            judge_state = _judge_state(last_decision)

            try:
                for stage in self.config.workflow.stages:
                    stage_evidence = await self._run_stage(
                        store=store,
                        task=task,
                        round_number=round_number,
                        stage=stage,
                        judge_state=judge_state,
                        current_round_evidence=round_evidence,
                        prior_evidence=prior_evidence,
                    )
                    round_evidence.extend(stage_evidence)

                decision = await self._run_judge(
                    store=store,
                    task=task,
                    round_number=round_number,
                    judge_state=judge_state,
                    current_round_evidence=round_evidence,
                    prior_evidence=prior_evidence,
                )
            except AgentExecutionError:
                if self._remaining_seconds() <= 0:
                    return self._finalize(
                        store,
                        task=task,
                        stop=StopDecision(
                            True,
                            StopOutcome.TIMED_OUT,
                            "maximum elapsed time reached during an agent invocation",
                        ),
                        decision=last_decision,
                        rounds_completed=rounds_completed,
                    )
                raise

            stop = evaluate_stop(
                round_number,
                self._elapsed_seconds(),
                decision,
                prior_decisions=prior_decisions,
                policy=self.config.workflow.stop,
            )
            rounds_completed = round_number
            last_decision = decision
            store.append_event(
                "round_completed",
                {
                    "round_number": round_number,
                    "judge_verdict": decision.verdict.value,
                    "stop_outcome": stop.outcome.value,
                },
            )
            store.update_manifest(
                round_count=rounds_completed,
                rounds=[
                    *store.manifest.get("rounds", []),
                    {
                        "round_number": round_number,
                        "judge_verdict": decision.verdict.value,
                        "completed_at": _utc_now().isoformat(),
                    },
                ],
                elapsed_seconds=self._elapsed_seconds(),
                latest_decision=decision.model_dump(mode="json"),
                current_round=None,
            )
            if stop.should_stop:
                return self._finalize(
                    store,
                    task=task,
                    stop=stop,
                    decision=decision,
                    rounds_completed=rounds_completed,
                )

            prior_decisions.append(decision)
            prior_evidence.extend(round_evidence)
            prior_evidence[:] = [
                item
                for item in prior_evidence
                if item.round_number >= round_number - self.config.context.keep_recent_rounds + 1
            ]

        raise RuntimeError("Stop evaluator did not terminate at max_rounds")

    async def _run_stage(
        self,
        *,
        store: ArtifactStore,
        task: str,
        round_number: int,
        stage: StageConfig,
        judge_state: JudgeContextState,
        current_round_evidence: Sequence[ContextEvidence],
        prior_evidence: Sequence[ContextEvidence],
    ) -> list[ContextEvidence]:
        store.append_event(
            "stage_started",
            {
                "round_number": round_number,
                "stage": stage.id,
                "mode": stage.mode.value,
            },
        )
        completed: list[tuple[ParticipantConfig, _InvocationOutcome, str]] = []

        if stage.mode is StageMode.PARALLEL:
            prompts = [
                (
                    participant,
                    self._build_participant_prompt(
                        task,
                        participant,
                        judge_state,
                        current_round_evidence,
                        prior_evidence,
                    ),
                )
                for participant in stage.participants
            ]
            tasks: list[asyncio.Task[_InvocationOutcome]] = []
            async with asyncio.TaskGroup() as group:
                for participant, prompt in prompts:
                    tasks.append(
                        group.create_task(
                            self._invoke_agent(
                                store=store,
                                round_number=round_number,
                                stage_id=stage.id,
                                role_id=participant.id,
                                agent_id=participant.agent,
                                prompt=prompt,
                            ),
                            name=f"debate-{stage.id}-{participant.id}",
                        )
                    )
            completed = [
                (participant, task_result.result(), prompt)
                for (participant, prompt), task_result in zip(prompts, tasks, strict=True)
            ]
        elif stage.mode is StageMode.INDEPENDENT_SEQUENTIAL:
            prompts = [
                (
                    participant,
                    self._build_participant_prompt(
                        task,
                        participant,
                        judge_state,
                        current_round_evidence,
                        prior_evidence,
                    ),
                )
                for participant in stage.participants
            ]
            for participant, prompt in prompts:
                outcome = await self._invoke_agent(
                    store=store,
                    round_number=round_number,
                    stage_id=stage.id,
                    role_id=participant.id,
                    agent_id=participant.agent,
                    prompt=prompt,
                )
                completed.append((participant, outcome, prompt))
                if outcome.error is not None and (
                    self.config.failure.on_agent_error is AgentErrorPolicy.ABORT
                    or self.config.failure.require_all_participants
                ):
                    store.append_event(
                        "stage_failed",
                        {
                            "round_number": round_number,
                            "stage": stage.id,
                            "participant": participant.id,
                            "error": str(outcome.error),
                        },
                    )
                    break
        else:
            sequential_evidence = list(current_round_evidence)
            for participant in stage.participants:
                prompt = self._build_participant_prompt(
                    task,
                    participant,
                    judge_state,
                    sequential_evidence,
                    prior_evidence,
                )
                outcome = await self._invoke_agent(
                    store=store,
                    round_number=round_number,
                    stage_id=stage.id,
                    role_id=participant.id,
                    agent_id=participant.agent,
                    prompt=prompt,
                )
                completed.append((participant, outcome, prompt))
                if outcome.error is not None and (
                    self.config.failure.on_agent_error is AgentErrorPolicy.ABORT
                    or self.config.failure.require_all_participants
                ):
                    store.append_event(
                        "stage_failed",
                        {
                            "round_number": round_number,
                            "stage": stage.id,
                            "participant": participant.id,
                            "error": str(outcome.error),
                        },
                    )
                    break
                if outcome.error is None and outcome.result.final_output:
                    sequential_evidence.append(
                        ContextEvidence(
                            round_number,
                            stage.id,
                            participant.id,
                            outcome.result.final_output,
                            sequence=len(sequential_evidence) + 1,
                        )
                    )

        failures = [
            (participant.id, outcome.error)
            for participant, outcome, _prompt in completed
            if outcome.error is not None
        ]
        successes = [
            (participant, outcome)
            for participant, outcome, _prompt in completed
            if outcome.error is None and outcome.result.final_output
        ]
        if failures and (
            self.config.failure.on_agent_error is AgentErrorPolicy.ABORT
            or self.config.failure.require_all_participants
        ):
            detail = "; ".join(f"{role}: {error}" for role, error in failures)
            raise AgentExecutionError(f"Stage {stage.id!r} failed: {detail}")
        if not successes:
            raise AgentExecutionError(f"Stage {stage.id!r} produced no successful evidence.")

        evidence = [
            ContextEvidence(
                round_number,
                stage.id,
                participant.id,
                outcome.result.final_output or "",
                sequence=len(current_round_evidence) + index,
            )
            for index, (participant, outcome) in enumerate(successes, start=1)
        ]
        store.append_event(
            "stage_completed",
            {
                "round_number": round_number,
                "stage": stage.id,
                "successes": [item.agent for item in evidence],
                "failures": [role for role, _error in failures],
            },
        )
        return evidence

    def _build_participant_prompt(
        self,
        task: str,
        participant: ParticipantConfig,
        judge_state: JudgeContextState,
        current_round_evidence: Sequence[ContextEvidence],
        prior_evidence: Sequence[ContextEvidence],
    ) -> str:
        role = _read_prompt(participant.prompt)
        if participant.label:
            role = f"{role}\n\nRole label: {participant.label}"
        return build_context(
            task,
            role,
            budget=self.config.context,
            judge_state=judge_state,
            current_round_outputs=current_round_evidence,
            recent_evidence=prior_evidence,
        )

    async def _run_judge(
        self,
        *,
        store: ArtifactStore,
        task: str,
        round_number: int,
        judge_state: JudgeContextState,
        current_round_evidence: Sequence[ContextEvidence],
        prior_evidence: Sequence[ContextEvidence],
    ) -> JudgeDecision:
        role = _read_prompt(self.config.workflow.judge.prompt)
        last_raw = ""
        last_result: AgentResult | None = None
        last_error: JudgeProtocolError | None = None
        repair_attempts = (
            self.config.failure.schema_repair_attempts
            if self.config.failure.on_judge_error is JudgeErrorPolicy.RETRY
            else 0
        )

        for protocol_attempt in range(repair_attempts + 1):
            evidence = list(current_round_evidence)
            repair_role = role
            if protocol_attempt and last_error is not None:
                evidence.append(
                    ContextEvidence(
                        round_number,
                        "judge-protocol",
                        "invalid-output",
                        last_raw,
                        sequence=len(evidence) + 1,
                    )
                )
                repair_role = (
                    f"{role}\n\nYour prior response failed Judge schema v1 validation: "
                    f"{last_error}. Return one corrected JSON object only."
                )
            prompt = build_context(
                task,
                repair_role,
                budget=self.config.context,
                judge_state=judge_state,
                current_round_outputs=evidence,
                recent_evidence=prior_evidence,
            )
            role_id = f"judge-protocol-{protocol_attempt + 1}"
            outcome = await self._invoke_agent(
                store=store,
                round_number=round_number,
                stage_id="judge-call",
                role_id=role_id,
                agent_id=self.config.workflow.judge.agent,
                prompt=prompt,
                structured_judge=True,
                invocation_kind="judge_attempt",
            )
            if outcome.error is not None or not outcome.result.final_output:
                raise AgentExecutionError(
                    f"Judge invocation failed: {outcome.error or 'empty final output'}"
                )

            last_raw = outcome.result.final_output
            last_result = outcome.result
            try:
                decision = parse_judge_response(last_raw)
            except JudgeProtocolError as exc:
                last_error = exc
                if protocol_attempt < repair_attempts:
                    store.append_event(
                        "judge_repair_requested",
                        {
                            "round_number": round_number,
                            "attempt": protocol_attempt + 1,
                            "error": str(exc),
                        },
                    )
                    continue
                store.append_event(
                    "judge_protocol_failed",
                    {
                        "round_number": round_number,
                        "attempt": protocol_attempt + 1,
                        "error": str(exc),
                    },
                )
                raise

            store.write_judge(
                round_number,
                prompt,
                last_raw,
                decision.model_dump(mode="json"),
                last_result,
            )
            return decision

        raise JudgeProtocolError("Judge schema repair loop ended without a decision")

    async def _invoke_agent(
        self,
        *,
        store: ArtifactStore,
        round_number: int,
        stage_id: str,
        role_id: str,
        agent_id: str,
        prompt: str,
        structured_judge: bool = False,
        invocation_kind: Literal["participant", "judge_attempt"] = "participant",
    ) -> _InvocationOutcome:
        config = self.config.agents[agent_id]
        final_outcome: _InvocationOutcome | None = None
        for attempt in range(1, config.retries + 2):
            async with self._semaphore:
                outcome = await self._invoke_once(
                    store=store,
                    round_number=round_number,
                    stage_id=stage_id,
                    role_id=role_id,
                    agent_id=agent_id,
                    agent_config=config,
                    prompt=prompt,
                    attempt=attempt,
                    structured_judge=structured_judge,
                )
            final_outcome = outcome
            store.write_invocation(
                round_number,
                stage_id,
                role_id,
                prompt,
                outcome.result,
                kind=invocation_kind,
                attempt=attempt,
                elapsed_seconds=self._elapsed_seconds(),
            )
            if outcome.error is None:
                return outcome
            if attempt <= config.retries:
                store.append_event(
                    "invocation_retry",
                    {
                        "round_number": round_number,
                        "stage": stage_id,
                        "participant": role_id,
                        "agent": agent_id,
                        "attempt": attempt,
                        "error": str(outcome.error),
                    },
                )
        if final_outcome is None:  # pragma: no cover - retries are validated non-negative
            raise RuntimeError("Invocation loop did not execute")
        return final_outcome

    async def _invoke_once(
        self,
        *,
        store: ArtifactStore,
        round_number: int,
        stage_id: str,
        role_id: str,
        agent_id: str,
        agent_config: AgentConfig,
        prompt: str,
        attempt: int,
        structured_judge: bool,
    ) -> _InvocationOutcome:
        remaining = self._remaining_seconds()
        adapter = get_adapter(agent_config.adapter)
        scratch: Path | None = None
        final_output_path: Path | None = None
        output_schema_path = (
            Path(__file__).with_name("schemas") / "judge-v1.json"
            if structured_judge and agent_config.adapter.value == "codex"
            else None
        )
        started_at = _utc_now()
        started_clock = time.monotonic()
        spec = None
        process_result: ProcessResult | None = None
        error: DebateError | None = None
        try:
            if remaining <= 0:
                raise GlobalTimeLimitError("No run-level wall-clock budget remains.")
            if agent_config.adapter is AgentAdapter.CODEX:
                scratch, final_output_path = _create_provider_scratch(
                    store.run_id,
                    workspace=self.config.run.workspace,
                )
            request = AgentRequest(
                agent_id=agent_id,
                role_id=role_id,
                prompt=prompt,
                cwd=self.config.run.workspace,
                final_output_path=final_output_path,
                output_schema_path=output_schema_path,
                run_id=store.run_id,
                round_number=round_number,
                stage_id=stage_id,
                timeout_seconds=max(
                    0.001,
                    min(agent_config.timeout_seconds, remaining),
                ),
                max_output_chars=agent_config.max_output_chars,
                model=agent_config.model,
                permission=agent_config.permission,
                extra_args=(),
            )
            spec = adapter.build_command(request, agent_config)
            process_result = await adapter.execute(
                request,
                agent_config,
                on_stream=self._stream_callback(agent_id),
            )
            if not process_result.output.strip():
                raise EmptyAgentOutputError(
                    f"Agent {agent_id!r} exited successfully but produced no final response."
                )
        except asyncio.CancelledError:
            raise
        except (DebateError, OSError) as exc:
            error = exc if isinstance(exc, DebateError) else AgentExecutionError(str(exc))
        finally:
            if scratch is not None:
                try:
                    _remove_provider_scratch(scratch)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    if error is None:
                        error = AgentExecutionError(
                            f"Could not remove private provider scratch directory: {exc}"
                        )

        finished_at = _utc_now()
        stdout = (
            process_result.stdout if process_result is not None else _error_text(error, "stdout")
        )
        stderr = (
            process_result.stderr if process_result is not None else _error_text(error, "stderr")
        )
        final_output = (
            process_result.output if process_result is not None and error is None else None
        )
        exit_code = (
            process_result.exit_code
            if process_result is not None
            else _error_int(error, "exit_code")
        )
        status = _invocation_status(error)
        display_command = (
            spec.display_argv if spec is not None else redact_display_argv(agent_config.command)
        )
        output_hash_source = "\0".join((stdout, stderr, final_output or ""))
        result = AgentResult(
            agent_id=agent_id,
            role_id=role_id,
            status=status,
            stdout=stdout,
            stderr=stderr,
            final_output=final_output,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, time.monotonic() - started_clock),
            timed_out=status is InvocationStatus.TIMED_OUT,
            truncated=status is InvocationStatus.OUTPUT_LIMIT,
            display_command=display_command,
            input_hash=content_sha256(prompt),
            output_hash=content_sha256(output_hash_source),
            provider_adapter=(spec.provider_adapter if spec is not None else adapter.name),
            provider_model=(
                spec.provider_model if spec is not None else agent_config.model
            ),
            session_mode=(spec.session_mode if spec is not None else "unverified"),
            session_enforcement=(
                spec.session_enforcement
                if spec is not None
                else "command construction failed before session isolation was declared"
            ),
        )
        return _InvocationOutcome(result=result, error=error)

    def _stream_callback(
        self,
        agent_id: str,
    ) -> Callable[[StreamName, str], Awaitable[None] | None] | None:
        if self.stream_handler is None or not self.config.run.stream:
            return None
        handler = self.stream_handler

        def callback(stream: StreamName, text: str) -> Awaitable[None] | None:
            value = handler(agent_id, stream, text)
            return value if inspect.isawaitable(value) else None

        return callback

    def _finalize(
        self,
        store: ArtifactStore,
        *,
        task: str,
        stop: StopDecision,
        decision: JudgeDecision | None,
        rounds_completed: int,
    ) -> EngineResult:
        report = render_final_report(
            FinalReportData(
                run_id=store.run_id,
                status=stop.outcome.value,
                stop_reason=stop.reason,
                round_count=rounds_completed,
                request=task,
                decision=decision,
            )
        )
        store.write_final(report)
        elapsed = self._elapsed_seconds()
        store.append_event(
            "execution_finished",
            {
                "outcome": stop.outcome.value,
                "stop_reason": stop.reason,
                "round_count": rounds_completed,
            },
        )
        store.update_manifest(
            status=stop.outcome.value,
            outcome=stop.outcome.value,
            stop_reason=stop.reason,
            round_count=rounds_completed,
            elapsed_seconds=elapsed,
            finished_at=_utc_now().isoformat(),
            current_round=None,
            final_decision=(decision.model_dump(mode="json") if decision else None),
        )
        self._write_evidence_report(store)
        return EngineResult(
            run_id=store.run_id,
            run_dir=store.run_dir,
            status=stop.outcome.value,
            stop_reason=stop.reason,
            rounds_completed=rounds_completed,
            final_report=report,
        )

    def _elapsed_seconds(self) -> float:
        return self._base_elapsed_seconds + max(0.0, time.monotonic() - self._clock_started)

    def _remaining_seconds(self) -> float:
        return self.config.workflow.stop.max_elapsed_seconds - self._elapsed_seconds()

    def _prompt_paths(self) -> tuple[Path, ...]:
        values = [
            participant.prompt
            for stage in self.config.workflow.stages
            for participant in stage.participants
        ]
        values.append(self.config.workflow.judge.prompt)
        return tuple(dict.fromkeys(values))

    def _verify_resume_inputs(self, store: ArtifactStore) -> None:
        manifest = store.manifest
        expected = manifest.get("prompt_hashes", {})
        if not isinstance(expected, dict):
            raise ResumeError("Run manifest does not contain verifiable prompt hashes.")
        try:
            actual = {str(path): content_sha256(path.read_bytes()) for path in self._prompt_paths()}
        except OSError as exc:
            raise ResumeError(f"Cannot verify configured role prompts: {exc}") from exc
        if expected != actual:
            raise ResumeError(
                "Configured role prompts changed after the run began; start a new run "
                "instead of mixing incompatible evidence."
            )
        config_text = json.dumps(
            self.config.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        if manifest.get("config_hash") != content_sha256(config_text):
            raise ResumeError("Resolved configuration no longer matches the run snapshot.")

    def _load_prior_decisions(self, store: ArtifactStore) -> list[JudgeDecision]:
        return _load_verified_judge_decisions(store)

    def _load_prior_evidence(
        self,
        store: ArtifactStore,
        *,
        completed_rounds: int,
    ) -> list[ContextEvidence]:
        evidence: list[ContextEvidence] = []
        invocations = store.manifest.get("invocations", [])
        if not isinstance(invocations, list):
            raise ResumeError("Run manifest invocations field is invalid.")

        workflow_order = {
            (stage.id, participant.id): ordinal
            for ordinal, (stage, participant) in enumerate(
                (
                    (stage, participant)
                    for stage in self.config.workflow.stages
                    for participant in stage.participants
                ),
                start=1,
            )
        }
        latest: dict[tuple[int, str, str], dict[str, Any]] = {}
        for invocation in invocations:
            number = int(invocation.get("round_number", 0))
            if number > completed_rounds:
                continue
            if invocation.get("kind") != "participant":
                continue
            stage = str(invocation.get("stage", ""))
            participant = str(invocation.get("participant", ""))
            if (stage, participant) not in workflow_order:
                continue
            latest[(number, stage, participant)] = invocation

        ordered = sorted(
            latest.items(),
            key=lambda item: (
                item[0][0],
                workflow_order[(item[0][1], item[0][2])],
            ),
        )
        for (number, stage, participant), invocation in ordered:
            if invocation.get("status") != InvocationStatus.SUCCESS.value:
                continue
            relative = f"{invocation['path']}/final.md"
            try:
                content = store.read_artifact_text(relative)
            except DebateError as exc:
                raise ResumeError(f"Cannot restore evidence from {relative}: {exc}") from exc
            if not content.strip():
                continue
            evidence.append(
                ContextEvidence(
                    number,
                    stage,
                    participant,
                    content,
                    sequence=workflow_order[(stage, participant)],
                )
            )
        return evidence

    def _record_cancelled(self, store: ArtifactStore) -> None:
        try:
            store.append_event("execution_cancelled", {})
            store.update_manifest(
                status="cancelled",
                outcome="cancelled",
                stop_reason="caller cancelled the run",
                elapsed_seconds=self._elapsed_seconds(),
                finished_at=_utc_now().isoformat(),
            )
            self._write_evidence_report(store)
        except DebateError:
            pass

    def _record_failure(self, store: ArtifactStore, error: BaseException) -> None:
        """Persist the consumed wall-clock budget before the terminal failure."""

        store.update_manifest(elapsed_seconds=self._elapsed_seconds())
        store.record_failure(error)
        self._write_evidence_report(store)

    @staticmethod
    def _write_evidence_report(store: ArtifactStore) -> None:
        manifest = store.manifest
        document = build_result_document(manifest, store.read_artifact_text)
        store.write_result(document)
        manifest = store.manifest
        report = render_evidence_report(manifest, store.read_artifact_text)
        store.write_evidence(report)


async def run_debate(
    config: DebateConfig,
    task: str,
    *,
    allow_unsafe: bool = False,
    stream_handler: StreamHandler | None = None,
) -> EngineResult:
    """Functional convenience wrapper for a new debate run."""

    return await DebateEngine(
        config,
        allow_unsafe=allow_unsafe,
        stream_handler=stream_handler,
    ).run(task)


async def resume_debate(
    run_dir: str | Path,
    *,
    allow_unsafe: bool = False,
    retry_failed: bool = False,
    stream_handler: StreamHandler | None = None,
) -> EngineResult:
    """Load the snapshotted configuration and resume a saved run."""

    run_path = await asyncio.to_thread(_absolute_run_path, run_dir)
    store = await asyncio.to_thread(ArtifactStore.load_existing, run_path)
    try:
        _validate_resume_status(store.manifest, retry_failed=retry_failed)
        _load_verified_judge_decisions(store)
        config_text = store.read_artifact_text("config.resolved.yaml")
        config = _load_snapshot_config(config_text, store.run_dir)
        engine = DebateEngine(
            config,
            allow_unsafe=allow_unsafe,
            stream_handler=stream_handler,
        )
    except BaseException:
        store.close()
        raise
    return await engine._resume_open_store(store, retry_failed=retry_failed)


def _load_verified_judge_decisions(store: ArtifactStore) -> list[JudgeDecision]:
    """Validate contained Judge barriers before trusting configuration paths."""

    decisions: list[JudgeDecision] = []
    judges = store.manifest.get("judges", [])
    if not isinstance(judges, list):
        raise ResumeError("Run manifest judges field is invalid.")
    for judge in judges:
        relative = f"{judge['path']}/decision.json"
        try:
            value = json.loads(store.read_artifact_text(relative))
            decisions.append(JudgeDecision.model_validate(value))
        except (DebateError, ValueError, TypeError) as exc:
            raise ResumeError(f"Cannot restore Judge decision from {relative}: {exc}") from exc
    return decisions


def _load_snapshot_config(text: str, run_dir: Path) -> DebateConfig:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid verified run configuration snapshot: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Invalid verified run configuration snapshot: root is not a mapping")
    try:
        return DebateConfig.model_validate(raw).resolved(
            relative_to=run_dir,
            source_path=run_dir / "config.resolved.yaml",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Invalid verified run configuration snapshot: {exc}") from exc


def _absolute_run_path(run_dir: str | Path) -> Path:
    path = Path(run_dir).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _create_provider_scratch(run_id: str, *, workspace: Path) -> tuple[Path, Path]:
    """Create one Codex output slot outside its model-writable filesystem roots."""

    root = _provider_scratch_root(workspace)
    scratch = Path(tempfile.mkdtemp(prefix=f"{run_id}-", dir=root))
    try:
        scratch = _harden_private_directory(scratch)
        final_output = scratch / "final.txt"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(final_output, flags, _PRIVATE_FILE_MODE)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError(f"Provider output slot is not a private regular file: {final_output}")
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
    except BaseException:
        with suppress(OSError):
            _remove_provider_scratch(scratch)
        raise
    return scratch, final_output


def _provider_scratch_root(workspace: Path) -> Path:
    """Return a private state root disjoint from the workspace and system temp."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        state_home = Path(configured).expanduser()
        if not state_home.is_absolute():
            raise ConfigError("XDG_STATE_HOME must be an absolute path.")
    else:
        try:
            state_home = Path.home() / ".local" / "state"
        except RuntimeError as exc:
            raise ConfigError("Cannot determine a private engine state directory.") from exc

    engine_root = state_home / _ENGINE_STATE_DIRECTORY
    _reject_symlink_components(engine_root)
    _validate_scratch_boundaries(
        engine_root / _PROVIDER_SCRATCH_DIRECTORY,
        workspace=workspace,
    )
    engine_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    _reject_symlink_components(engine_root)
    engine_root = _harden_private_directory(engine_root)
    scratch_root = engine_root / _PROVIDER_SCRATCH_DIRECTORY
    scratch_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
    _reject_symlink_components(scratch_root)
    scratch_root = _harden_private_directory(scratch_root)
    _validate_scratch_boundaries(scratch_root, workspace=workspace)
    return scratch_root


def _validate_scratch_boundaries(scratch_root: Path, *, workspace: Path) -> None:
    scratch_root = scratch_root.resolve(strict=False)
    workspace_root = workspace.resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    for label, boundary in (("workspace", workspace_root), ("system temporary", temp_root)):
        if _paths_overlap(scratch_root, boundary):
            raise ConfigError(
                f"Engine provider scratch root {scratch_root} overlaps the {label} "
                f"directory {boundary}; choose a disjoint absolute XDG_STATE_HOME."
            )


def _reject_symlink_components(path: Path) -> None:
    """Reject every existing symlink from the filesystem root to ``path``."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"Private state path contains a symbolic link: {current}")


def _harden_private_directory(path: Path) -> Path:
    """Open one directory without following its leaf and enforce owner-only access."""

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        visible = path.lstat()
        if not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(visible.st_mode):
            raise OSError(f"Private state path is not a directory: {path}")
        if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
            raise OSError(f"Private state directory changed while it was opened: {path}")
        if opened.st_uid != os.getuid():
            raise OSError(f"Private state directory is not owned by this user: {path}")
        os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
        hardened = os.fstat(descriptor)
        if stat.S_IMODE(hardened.st_mode) != _PRIVATE_DIRECTORY_MODE:
            raise OSError(f"Could not enforce private permissions on state directory: {path}")
    finally:
        os.close(descriptor)
    return path.resolve(strict=True)


def _remove_provider_scratch(path: Path) -> None:
    """Remove a scratch tree only through Python's fd-safe POSIX implementation."""

    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError("Secure provider scratch cleanup is unavailable on this platform.")
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise OSError(f"Refusing to remove an unsafe provider scratch path: {path}")
    shutil.rmtree(path)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _restored_round_summaries(
    manifest: dict[str, Any],
    decisions: Sequence[JudgeDecision],
) -> list[dict[str, Any]]:
    existing = manifest.get("rounds", [])
    judges = manifest.get("judges", [])
    summaries: list[dict[str, Any]] = []
    for index, decision in enumerate(decisions):
        if isinstance(existing, list) and index < len(existing):
            item = existing[index]
            if isinstance(item, dict):
                summaries.append(dict(item))
                continue
        recorded_at = (
            judges[index].get("recorded_at")
            if (
                isinstance(judges, list) and index < len(judges) and isinstance(judges[index], dict)
            )
            else None
        )
        summaries.append(
            {
                "round_number": index + 1,
                "judge_verdict": decision.verdict.value,
                "completed_at": recorded_at,
            }
        )
    return summaries


def _read_prompt(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Could not read role prompt {path}: {exc}") from exc
    if not value:
        raise ConfigError(f"Role prompt is empty: {path}")
    return value


def _judge_state(decision: JudgeDecision | None) -> JudgeContextState:
    if decision is None:
        return JudgeContextState()
    return JudgeContextState(
        ledger=decision.accepted_decisions,
        open_issues=decision.unresolved_issues,
        next_round_focus=decision.next_round_focus,
    )


def _diagnostic_dict(value: AgentDiagnostic) -> dict[str, Any]:
    result = asdict(value)
    if result["executable"] is not None:
        result["executable"] = str(result["executable"])
    return result


def _workspace_metadata(workspace: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(workspace),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": None,
        "git_dirty": None,
    }
    try:
        commit = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        dirty = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return result
    result["git_commit"] = commit.stdout.strip()
    result["git_dirty"] = bool(dirty.stdout.strip())
    return result


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_resume_status(
    manifest: dict[str, Any],
    *,
    retry_failed: bool,
) -> None:
    status = str(manifest.get("status", ""))
    if status in {
        StopOutcome.FINALIZED.value,
        StopOutcome.EXHAUSTED.value,
        StopOutcome.BLOCKED.value,
        StopOutcome.TIMED_OUT.value,
    }:
        raise ResumeError(f"Run is already terminal with status {status!r}.")
    if status == "failed" and not retry_failed:
        raise ResumeError("Failed runs require --retry-failed before model calls are repeated.")


def _error_text(error: DebateError | None, name: str) -> str:
    value = getattr(error, name, "") if error is not None else ""
    return str(value) if value is not None else ""


def _error_int(error: DebateError | None, name: str) -> int | None:
    value = getattr(error, name, None) if error is not None else None
    return value if isinstance(value, int) else None


def _invocation_status(error: DebateError | None) -> InvocationStatus:
    if error is None:
        return InvocationStatus.SUCCESS
    if isinstance(error, ProcessTimeoutError):
        return InvocationStatus.TIMED_OUT
    if isinstance(error, ProcessOutputLimitError):
        return InvocationStatus.OUTPUT_LIMIT
    return InvocationStatus.FAILED


__all__ = [
    "DebateEngine",
    "EmptyAgentOutputError",
    "EngineResult",
    "GlobalTimeLimitError",
    "StreamHandler",
    "resume_debate",
    "run_debate",
]
