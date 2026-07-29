"""Async, shell-free subprocess supervision for CLI agents."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import inspect
import os
import shlex
import signal
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Literal, cast

from agent_debate.adapters.base import CommandSpec, StreamCallback, StreamName
from agent_debate.errors import AgentExecutionError, PreflightError

_READ_CHUNK_BYTES = 64 * 1024
_GROUP_POLL_SECONDS = 0.025
_KILL_CONFIRM_SECONDS = 0.25
OutputSource = Literal["stdout", "stderr", "final"]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured output from a process that exited with status zero."""

    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    final_text: str | None = None
    transport_truncated: bool = False
    transport_observed_chars: int = 0

    @property
    def returncode(self) -> int:
        """Compatibility alias matching ``asyncio.subprocess.Process``."""

        return self.exit_code

    @property
    def output(self) -> str:
        """Return the provider's authoritative output."""

        return self.final_text if self.final_text is not None else self.stdout

    @property
    def final_output(self) -> str | None:
        """Compatibility alias for callers using artifact terminology."""

        return self.final_text


class ProcessExecutionError(AgentExecutionError):
    """Base class for a process that did not produce a valid success."""

    def __init__(
        self,
        message: str,
        *,
        display_argv: tuple[str, ...],
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        transport_truncated: bool = False,
        transport_observed_chars: int = 0,
    ) -> None:
        super().__init__(message)
        self.display_argv = display_argv
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.transport_truncated = transport_truncated
        self.transport_observed_chars = transport_observed_chars

    @property
    def returncode(self) -> int | None:
        """Compatibility alias for the failed process return code."""

        return self.exit_code


class ProcessSpawnError(PreflightError):
    """The configured executable could not be started."""

    def __init__(
        self,
        message: str,
        *,
        display_argv: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.display_argv = display_argv


class ProcessExitError(ProcessExecutionError):
    """The process exited with a non-zero status."""


class ProcessResidualGroupError(ProcessExecutionError):
    """The process leader exited while members of its process group remained."""

    def __init__(
        self,
        *,
        process_group: int,
        cleanup_succeeded: bool,
        display_argv: tuple[str, ...],
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        transport_truncated: bool = False,
        transport_observed_chars: int = 0,
    ) -> None:
        cleanup_status = (
            "the residual process group was terminated"
            if cleanup_succeeded
            else "the residual process group could not be fully terminated"
        )
        super().__init__(
            "Agent process leader exited but left background processes running; "
            f"{cleanup_status}: {_display_command(display_argv)}",
            display_argv=display_argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            transport_truncated=transport_truncated,
            transport_observed_chars=transport_observed_chars,
        )
        self.process_group = process_group
        self.cleanup_succeeded = cleanup_succeeded


class ProcessTimeoutError(ProcessExecutionError):
    """The process exceeded its configured wall-clock timeout."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        display_argv: tuple[str, ...],
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        transport_truncated: bool = False,
        transport_observed_chars: int = 0,
    ) -> None:
        super().__init__(
            f"Agent process timed out after {timeout_seconds:g} seconds: "
            f"{_display_command(display_argv)}",
            display_argv=display_argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            transport_truncated=transport_truncated,
            transport_observed_chars=transport_observed_chars,
        )
        self.timeout_seconds = timeout_seconds


class ProcessOutputLimitError(ProcessExecutionError):
    """Combined captured output crossed the configured character ceiling."""

    def __init__(
        self,
        *,
        limit: int,
        observed: int,
        stream: OutputSource,
        display_argv: tuple[str, ...],
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        transport_truncated: bool = False,
        transport_observed_chars: int = 0,
    ) -> None:
        super().__init__(
            f"Agent {stream} output exceeded the {limit} character limit "
            f"(observed at least {observed} characters): {_display_command(display_argv)}",
            display_argv=display_argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            transport_truncated=transport_truncated,
            transport_observed_chars=transport_observed_chars,
        )
        self.limit = limit
        self.observed = observed
        self.stream = stream


@dataclass(slots=True)
class _OutputBudget:
    limit: int
    display_argv: tuple[str, ...]
    truncate: bool = False
    captured: int = 0
    observed: int = 0
    truncated: bool = False

    def consume(self, text: str, *, stream: StreamName) -> str:
        self.observed += len(text)
        remaining = max(0, self.limit - self.captured)
        if len(text) > remaining and not self.truncate:
            raise ProcessOutputLimitError(
                limit=self.limit,
                observed=self.observed,
                stream=stream,
                display_argv=self.display_argv,
                transport_truncated=True,
                transport_observed_chars=self.observed,
            )
        captured = text[:remaining]
        self.captured += len(captured)
        self.truncated = self.truncated or len(captured) < len(text)
        return captured


async def run_process(
    spec: CommandSpec,
    *,
    on_stream: StreamCallback | None = None,
) -> ProcessResult:
    """Run an argv directly, draining both output pipes without deadlock.

    Timeout, caller cancellation, and strict output-limit failure terminate the
    dedicated process group with TERM, wait for the configured grace period,
    then use KILL if any member remains. Adapters with an authoritative final
    artifact may instead bound captured transport output while continuing to
    drain the process pipes.
    """

    started = asyncio.get_running_loop().time()
    stdin_pipe = asyncio.subprocess.PIPE if spec.stdin is not None else asyncio.subprocess.DEVNULL
    environment = _process_environment(spec.env)

    try:
        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            stdin=stdin_pipe,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=environment,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise ProcessSpawnError(
            f"Unable to start agent process {_display_command(spec.display_argv)}: {exc}",
            display_argv=spec.display_argv,
        ) from exc

    if process.stdout is None or process.stderr is None:  # pragma: no cover - asyncio contract
        await _terminate_process_group(process, grace_seconds=spec.terminate_grace_seconds)
        raise RuntimeError("asyncio did not create the requested output pipes")

    budget = _OutputBudget(
        limit=spec.max_output_chars,
        display_argv=spec.display_argv,
        truncate=spec.truncate_transport_output,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_task = asyncio.create_task(
        _drain_stream(
            process.stdout,
            stream="stdout",
            chunks=stdout_chunks,
            budget=budget,
            on_stream=on_stream,
        ),
        name="agent-stdout-drain",
    )
    stderr_task = asyncio.create_task(
        _drain_stream(
            process.stderr,
            stream="stderr",
            chunks=stderr_chunks,
            budget=budget,
            on_stream=on_stream,
        ),
        name="agent-stderr-drain",
    )
    wait_task = asyncio.create_task(process.wait(), name="agent-process-wait")
    input_task = asyncio.create_task(
        _write_stdin(process, spec.stdin),
        name="agent-stdin-writer",
    )
    tasks: tuple[asyncio.Task[object], ...] = cast(
        tuple[asyncio.Task[object], ...],
        (stdout_task, stderr_task, wait_task, input_task),
    )

    timeout_context = asyncio.timeout(spec.timeout_seconds)
    try:
        async with timeout_context:
            await asyncio.gather(*tasks)
    except TimeoutError as exc:
        await _abort_process(
            process,
            tasks=tasks,
            grace_seconds=spec.terminate_grace_seconds,
        )
        if not timeout_context.expired():
            raise
        raise ProcessTimeoutError(
            timeout_seconds=spec.timeout_seconds,
            display_argv=spec.display_argv,
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
            exit_code=process.returncode,
            transport_truncated=budget.truncated,
            transport_observed_chars=budget.observed,
        ) from exc
    except ProcessOutputLimitError as exc:
        await _abort_process(
            process,
            tasks=tasks,
            grace_seconds=spec.terminate_grace_seconds,
        )
        raise ProcessOutputLimitError(
            limit=exc.limit,
            observed=exc.observed,
            stream=exc.stream,
            display_argv=spec.display_argv,
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
            exit_code=process.returncode,
            transport_truncated=True,
            transport_observed_chars=budget.observed,
        ) from exc
    except asyncio.CancelledError:
        await asyncio.shield(
            _abort_process(
                process,
                tasks=tasks,
                grace_seconds=spec.terminate_grace_seconds,
            )
        )
        raise
    except BaseException:
        await _abort_process(
            process,
            tasks=tasks,
            grace_seconds=spec.terminate_grace_seconds,
        )
        raise

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    exit_code = wait_task.result()
    duration = asyncio.get_running_loop().time() - started
    if os.name == "posix" and _process_group_exists(process.pid):
        cleanup_succeeded = await _terminate_process_group(
            process,
            grace_seconds=spec.terminate_grace_seconds,
        )
        if not (exit_code == 0 and cleanup_succeeded and spec.allow_residual_process_cleanup):
            raise ProcessResidualGroupError(
                process_group=process.pid,
                cleanup_succeeded=cleanup_succeeded,
                display_argv=spec.display_argv,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                transport_truncated=budget.truncated,
                transport_observed_chars=budget.observed,
            )
    if exit_code != 0:
        raise ProcessExitError(
            f"Agent process exited with status {exit_code}: {_display_command(spec.display_argv)}",
            display_argv=spec.display_argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            transport_truncated=budget.truncated,
            transport_observed_chars=budget.observed,
        )
    return ProcessResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_seconds=duration,
        transport_truncated=budget.truncated,
        transport_observed_chars=budget.observed,
    )


async def _drain_stream(
    reader: asyncio.StreamReader,
    *,
    stream: StreamName,
    chunks: list[str],
    budget: _OutputBudget,
    on_stream: StreamCallback | None,
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while raw_chunk := await reader.read(_READ_CHUNK_BYTES):
        text = decoder.decode(raw_chunk, final=False)
        if text:
            captured = budget.consume(text, stream=stream)
            if captured:
                chunks.append(captured)
                await _notify_stream(on_stream, stream=stream, text=captured)

    final_text = decoder.decode(b"", final=True)
    if final_text:
        captured = budget.consume(final_text, stream=stream)
        if captured:
            chunks.append(captured)
            await _notify_stream(on_stream, stream=stream, text=captured)


async def _notify_stream(
    callback: StreamCallback | None,
    *,
    stream: StreamName,
    text: str,
) -> None:
    if callback is None:
        return
    callback_result = callback(stream, text)
    if inspect.isawaitable(callback_result):
        await cast(Awaitable[object], callback_result)


async def _write_stdin(
    process: asyncio.subprocess.Process,
    payload: str | bytes | None,
) -> None:
    if payload is None or process.stdin is None:
        return
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # A process may exit before consuming all input. Its return code remains
        # the authoritative success/failure signal.
        pass
    finally:
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()


async def _abort_process(
    process: asyncio.subprocess.Process,
    *,
    tasks: tuple[asyncio.Task[object], ...],
    grace_seconds: float,
) -> None:
    await _terminate_process_group(process, grace_seconds=grace_seconds)
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> bool:
    if os.name != "posix":  # pragma: no cover - CI and supported local CLIs are POSIX
        await _terminate_single_process(process, grace_seconds=grace_seconds)
        return process.returncode is not None

    process_group = process.pid
    _signal_process_group(process_group, signal.SIGTERM, process)
    group_exited = await _wait_for_process_group(process_group, grace_seconds)
    if not group_exited:
        _signal_process_group(process_group, signal.SIGKILL, process)
        kill_confirm_seconds = max(grace_seconds, _KILL_CONFIRM_SECONDS)
        if process.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(process.wait()),
                    timeout=kill_confirm_seconds,
                )
        group_exited = await _wait_for_process_group(
            process_group,
            kill_confirm_seconds,
        )

    if process.returncode is None:
        await process.wait()
    return group_exited and not _process_group_exists(process_group)


async def _terminate_single_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=grace_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()


def _signal_process_group(
    process_group: int,
    requested_signal: signal.Signals,
    process: asyncio.subprocess.Process,
) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        return
    except PermissionError:
        if process.returncode is not None:
            return
        if requested_signal is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


async def _wait_for_process_group(process_group: int, grace_seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_seconds
    while _process_group_exists(process_group):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_GROUP_POLL_SECONDS, remaining))
    return True


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_environment(overrides: Mapping[str, str] | None) -> Mapping[str, str] | None:
    if overrides is None:
        return None
    return {**os.environ, **overrides}


def _display_command(argv: tuple[str, ...]) -> str:
    return shlex.join(argv)


# Concise aliases for callers that do not care about the implementation layer.
OutputLimitExceeded = ProcessOutputLimitError
ProcessTimedOut = ProcessTimeoutError

__all__ = [
    "OutputLimitExceeded",
    "ProcessExecutionError",
    "ProcessExitError",
    "ProcessOutputLimitError",
    "ProcessResidualGroupError",
    "ProcessResult",
    "ProcessSpawnError",
    "ProcessTimedOut",
    "ProcessTimeoutError",
    "run_process",
]
