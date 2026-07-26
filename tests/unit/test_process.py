from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent_debate.adapters.base import CommandSpec
from agent_debate.adapters.process import (
    ProcessExitError,
    ProcessOutputLimitError,
    ProcessResidualGroupError,
    ProcessResult,
    ProcessSpawnError,
    ProcessTimeoutError,
    run_process,
)


def fake_cli(
    tmp_path: Path,
    source: str,
    *,
    stdin: str | bytes | None = None,
    timeout_seconds: float = 2.0,
    max_output_chars: int = 1_000_000,
    terminate_grace_seconds: float = 0.05,
) -> CommandSpec:
    script = tmp_path / "fake_cli.py"
    script.write_text(source, encoding="utf-8")
    # ``-S`` prevents pytest-cov's subprocess site hook from writing coverage
    # shards with a different branch setting from this repository.
    argv = (sys.executable, "-S", str(script))
    return CommandSpec(
        argv=argv,
        display_argv=argv,
        cwd=tmp_path,
        stdin=stdin,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        terminate_grace_seconds=terminate_grace_seconds,
    )


async def test_concurrently_drains_stdout_and_stderr_flood(tmp_path: Path) -> None:
    size = 300_000
    spec = fake_cli(
        tmp_path,
        (f"import os\nos.write(1, b'o' * {size})\nos.write(2, b'e' * {size})\n"),
        max_output_chars=(size * 2) + 1,
    )
    streamed: dict[str, list[str]] = {"stdout": [], "stderr": []}

    async def on_stream(stream: str, text: str) -> None:
        await asyncio.sleep(0)
        streamed[stream].append(text)

    result = await run_process(spec, on_stream=on_stream)

    assert result.exit_code == 0
    assert result.returncode == 0
    assert result.stdout == "o" * size
    assert result.stderr == "e" * size
    assert "".join(streamed["stdout"]) == result.stdout
    assert "".join(streamed["stderr"]) == result.stderr


async def test_decodes_utf8_split_across_pipe_reads(tmp_path: Path) -> None:
    spec = fake_cli(
        tmp_path,
        (
            "import os, time\n"
            "for byte in '你好'.encode('utf-8'):\n"
            "    os.write(1, bytes([byte]))\n"
            "    time.sleep(0.01)\n"
        ),
    )

    result = await run_process(spec)

    assert result.stdout == "你好"


async def test_writes_prompt_to_stdin_without_shell_interpretation(tmp_path: Path) -> None:
    prompt = "你好 $(touch should-not-exist) `whoami`\n"
    spec = fake_cli(
        tmp_path,
        "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        stdin=prompt,
    )

    result = await run_process(spec)

    assert result.output == prompt
    assert not (tmp_path / "should-not-exist").exists()


async def test_nonzero_exit_is_strict_failure_with_captured_output(tmp_path: Path) -> None:
    spec = fake_cli(
        tmp_path,
        (
            "import sys\n"
            "print('partial answer')\n"
            "print('provider failed', file=sys.stderr)\n"
            "raise SystemExit(7)\n"
        ),
    )

    with pytest.raises(ProcessExitError) as caught:
        await run_process(spec)

    assert caught.value.exit_code == 7
    assert caught.value.returncode == 7
    assert caught.value.stdout == "partial answer\n"
    assert caught.value.stderr == "provider failed\n"


async def test_timeout_terminates_term_ignoring_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-alive"
    spec = fake_cli(
        tmp_path,
        (
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "subprocess.Popen([\n"
            "    sys.executable,\n"
            "    '-S',\n"
            "    '-c',\n"
            f'    "import time; from pathlib import Path; time.sleep(1); '
            f"Path({str(marker)!r}).write_text('alive')\",\n"
            "])\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n"
        ),
        timeout_seconds=0.3,
        terminate_grace_seconds=0.05,
    )

    with pytest.raises(ProcessTimeoutError) as caught:
        await run_process(spec)

    assert caught.value.timeout_seconds == 0.3
    await asyncio.sleep(1.1)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
async def test_successful_parent_with_background_child_is_failure_and_cleaned(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "background-child-ready"
    marker = tmp_path / "background-child-survived"
    child_source = (
        "import signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(0.7)\n"
        f"Path({str(marker)!r}).write_text('survived')\n"
        "time.sleep(60)\n"
    )
    spec = fake_cli(
        tmp_path,
        (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            "subprocess.Popen(\n"
            f"    [sys.executable, '-S', '-c', {child_source!r}],\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            "    close_fds=True,\n"
            ")\n"
            "deadline = time.monotonic() + 2\n"
            f"while not Path({str(ready)!r}).exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise RuntimeError('background child did not become ready')\n"
            "    time.sleep(0.01)\n"
            "print('parent finished')\n"
        ),
        terminate_grace_seconds=0.05,
    )

    with pytest.raises(ProcessResidualGroupError) as caught:
        await run_process(spec)

    assert caught.value.exit_code == 0
    assert caught.value.stdout == "parent finished\n"
    assert caught.value.process_group > 0
    assert caught.value.cleanup_succeeded is True
    await asyncio.sleep(0.8)
    assert not marker.exists()


async def test_output_limit_is_failure_not_successful_truncation(tmp_path: Path) -> None:
    spec = fake_cli(
        tmp_path,
        (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('x' * 10000)\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        ),
        max_output_chars=100,
    )

    with pytest.raises(ProcessOutputLimitError) as caught:
        await run_process(spec)

    assert caught.value.limit == 100
    assert caught.value.observed > 100
    assert len(caught.value.stdout) <= 100


async def test_cancellation_terminates_process_group_and_propagates(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cancelled-child-alive"
    spec = fake_cli(
        tmp_path,
        (
            "import subprocess, sys, time\n"
            "subprocess.Popen([\n"
            "    sys.executable,\n"
            "    '-S',\n"
            "    '-c',\n"
            f'    "import time; from pathlib import Path; time.sleep(1); '
            f"Path({str(marker)!r}).write_text('alive')\",\n"
            "])\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        ),
        terminate_grace_seconds=0.05,
    )
    invocation = asyncio.create_task(run_process(spec))
    await asyncio.sleep(0.1)

    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    await asyncio.sleep(1.1)
    assert not marker.exists()


async def test_missing_executable_is_preflight_failure(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    spec = CommandSpec(
        argv=(str(missing),),
        display_argv=(str(missing),),
        cwd=tmp_path,
    )

    with pytest.raises(ProcessSpawnError) as caught:
        await run_process(spec)

    assert caught.value.display_argv == (str(missing),)


def test_process_result_authoritative_output_aliases() -> None:
    result = ProcessResult(
        stdout="events",
        stderr="diagnostics",
        exit_code=0,
        duration_seconds=0.1,
        final_text="answer",
    )

    assert result.output == "answer"
    assert result.final_output == "answer"
    assert result.returncode == 0


async def test_sync_stream_callback_and_environment_override(tmp_path: Path) -> None:
    variable_name = "AGENT_DEBATE_FAKE_ENV"
    spec = fake_cli(
        tmp_path,
        (
            "import os\n"
            f"print(os.environ[{variable_name!r}])\n"
            "print('warning', file=__import__('sys').stderr)\n"
        ),
    )
    spec = CommandSpec(
        argv=spec.argv,
        display_argv=spec.display_argv,
        cwd=spec.cwd,
        timeout_seconds=spec.timeout_seconds,
        max_output_chars=spec.max_output_chars,
        env={variable_name: "literal-value"},
    )
    chunks: list[tuple[str, str]] = []

    def on_stream(stream: str, text: str) -> None:
        chunks.append((stream, text))

    result = await run_process(spec, on_stream=on_stream)

    assert result.stdout == "literal-value\n"
    assert result.stderr == "warning\n"
    assert {stream for stream, _ in chunks} == {"stdout", "stderr"}
    assert os.environ.get(variable_name) is None


async def test_stream_callback_error_aborts_process_and_propagates(tmp_path: Path) -> None:
    spec = fake_cli(
        tmp_path,
        "import time\nprint('ready', flush=True)\ntime.sleep(60)\n",
    )

    def on_stream(_stream: str, _text: str) -> None:
        raise RuntimeError("consumer failed")

    with pytest.raises(RuntimeError, match="consumer failed"):
        await run_process(spec, on_stream=on_stream)


async def test_stream_callback_timeout_is_not_misreported_as_process_timeout(
    tmp_path: Path,
) -> None:
    spec = fake_cli(
        tmp_path,
        "import time\nprint('ready', flush=True)\ntime.sleep(60)\n",
    )

    def on_stream(_stream: str, _text: str) -> None:
        raise TimeoutError("consumer timeout")

    with pytest.raises(TimeoutError, match="consumer timeout") as caught:
        await run_process(spec, on_stream=on_stream)

    assert not isinstance(caught.value, ProcessTimeoutError)


async def test_incomplete_utf8_is_replaced_when_stream_closes(tmp_path: Path) -> None:
    spec = fake_cli(
        tmp_path,
        "import os\nos.write(1, b'valid\\xe4')\n",
    )

    result = await run_process(spec)

    assert result.stdout == "valid\ufffd"


async def test_combined_stdout_stderr_character_limit(tmp_path: Path) -> None:
    spec = fake_cli(
        tmp_path,
        ("import os, time\nos.write(1, b'o' * 60)\nos.write(2, b'e' * 60)\ntime.sleep(60)\n"),
        max_output_chars=100,
    )

    with pytest.raises(ProcessOutputLimitError) as caught:
        await run_process(spec)

    assert caught.value.observed == 120


async def test_child_exiting_before_large_stdin_is_not_a_writer_failure(
    tmp_path: Path,
) -> None:
    spec = fake_cli(
        tmp_path,
        "raise SystemExit(0)\n",
        stdin=b"x" * 1_000_000,
    )

    result = await run_process(spec)

    assert result.exit_code == 0
