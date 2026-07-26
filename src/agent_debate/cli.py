"""Typer command-line interface."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, TextIO

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from agent_debate import __version__
from agent_debate.adapters.registry import get_adapter
from agent_debate.config import DebateConfig, load_config
from agent_debate.engine import DebateEngine, EngineResult, resume_debate
from agent_debate.errors import (
    AgentExecutionError,
    ConfigError,
    DebateError,
    JudgeProtocolError,
    PreflightError,
    ResumeError,
    UnsafeConfigurationError,
)
from agent_debate.initializer import initialize_project
from agent_debate.models import AgentRequest
from agent_debate.preflight import diagnose_agents
from agent_debate.stop import StopOutcome

app = typer.Typer(
    name="agent-debate",
    help="Run safe, structured debates through local agent CLIs.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)

_EXIT_USAGE = 2
_EXIT_PREFLIGHT = 3
_EXIT_EXECUTION = 4
_EXIT_RESUME = 5
_EXIT_EXHAUSTED = 10
_EXIT_BLOCKED = 11
_EXIT_TIMED_OUT = 12


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agent-debate-engine {__version__}")
        raise typer.Exit


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the package version and exit.",
        ),
    ] = False,
) -> None:
    """Agent Debate Engine command group."""

    del version


@app.command("init")
def init_command(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory that will receive debate.yaml and prompts/."),
    ] = Path(),
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace existing starter files."),
    ] = False,
) -> None:
    """Create a self-contained starter configuration."""

    try:
        created = initialize_project(directory, force=force)
    except DebateError as exc:
        _abort(exc)
    console.print(f"[green]Created {len(created)} files in {created[0].parent}[/green]")
    for path in created:
        console.print(f"  {path}")


@app.command()
def validate(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="YAML configuration path."),
    ] = Path("debate.yaml"),
) -> None:
    """Validate configuration and adapter command construction without model calls."""

    try:
        config = load_config(config_path)
        planned = _validate_adapter_contracts(config)
    except DebateError as exc:
        _abort(exc)
    console.print(
        f"[green]Valid schema v{config.schema_version} configuration[/green] — "
        f"{len(config.agents)} agents, {len(config.workflow.stages)} stages"
    )
    if config.is_unsafe:
        console.print(
            "[yellow]Unsafe permission opt-in configured for: "
            f"{', '.join(config.unsafe_agents())}[/yellow]"
        )
    for agent_id, command in planned.items():
        console.print(f"  {agent_id}: {command}", markup=False)


@app.command()
def doctor(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="YAML configuration path."),
    ] = Path("debate.yaml"),
) -> None:
    """Check executable discovery and safe built-in versions without model calls."""

    try:
        config = load_config(config_path)
        _validate_adapter_contracts(config)
        diagnostics = asyncio.run(diagnose_agents(config.agents, cwd=config.run.workspace))
    except DebateError as exc:
        _abort(exc)

    table = Table(title="Agent diagnostics")
    table.add_column("Agent")
    table.add_column("Adapter")
    table.add_column("Executable")
    table.add_column("Version / error")
    table.add_column("Warnings")
    for item in diagnostics:
        table.add_row(
            item.agent_id,
            item.adapter,
            str(item.executable or "—"),
            item.version or item.error or "—",
            "\n".join(item.warnings) or "—",
            style=None if item.ok else "red",
        )
    console.print(table)
    failed = [item for item in diagnostics if not item.ok]
    if failed:
        raise typer.Exit(_EXIT_PREFLIGHT)
    console.print(
        "[green]All executables passed preflight.[/green] "
        "Built-in versions were checked; generic commands were not executed. "
        "Authentication and model aliases are checked only by a real invocation."
    )


@app.command("run")
def run_command(
    task: Annotated[
        str | None,
        typer.Argument(help="Debate task. Omit when using --task-file."),
    ] = None,
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="YAML configuration path."),
    ] = Path("debate.yaml"),
    task_file: Annotated[
        str | None,
        typer.Option(
            "--task-file",
            help="Read the task from a UTF-8 file, or '-' for stdin.",
        ),
    ] = None,
    allow_unsafe: Annotated[
        bool,
        typer.Option(
            "--allow-unsafe",
            help="Second acknowledgement for configured non-read-only permissions.",
        ),
    ] = False,
    no_stream: Annotated[
        bool,
        typer.Option("--no-stream", help="Do not mirror provider output to the terminal."),
    ] = False,
) -> None:
    """Start a new debate run."""

    try:
        config = load_config(config_path)
        requirement = _read_task(
            task,
            task_file,
            max_chars=config.context.max_requirement_chars,
        )
        if no_stream:
            config = config.model_copy(
                update={"run": config.run.model_copy(update={"stream": False})}
            )
        engine = DebateEngine(
            config,
            allow_unsafe=allow_unsafe,
            stream_handler=_rich_stream,
        )
        result = asyncio.run(engine.run(requirement))
    except DebateError as exc:
        _abort(exc)
    except KeyboardInterrupt:
        error_console.print("[yellow]Cancelled by user.[/yellow]")
        raise typer.Exit(130) from None
    _render_result(result)


@app.command()
def resume(
    run_directory: Annotated[
        Path,
        typer.Argument(help="Existing run directory containing manifest.json."),
    ],
    retry_failed: Annotated[
        bool,
        typer.Option(
            "--retry-failed",
            help="Permit repeating calls from a run already marked failed.",
        ),
    ] = False,
    allow_unsafe: Annotated[
        bool,
        typer.Option(
            "--allow-unsafe",
            help="Second acknowledgement for configured non-read-only permissions.",
        ),
    ] = False,
    no_stream: Annotated[
        bool,
        typer.Option("--no-stream", help="Do not mirror provider output to the terminal."),
    ] = False,
) -> None:
    """Resume after the last valid Judge barrier."""

    try:
        result = asyncio.run(
            resume_debate(
                run_directory,
                allow_unsafe=allow_unsafe,
                retry_failed=retry_failed,
                stream_handler=None if no_stream else _rich_stream,
            )
        )
    except DebateError as exc:
        _abort(exc)
    except KeyboardInterrupt:
        error_console.print("[yellow]Cancelled by user.[/yellow]")
        raise typer.Exit(130) from None
    _render_result(result)


@app.command()
def schema(
    kind: Annotated[
        str,
        typer.Option("--kind", help="'config' or 'judge'."),
    ] = "config",
) -> None:
    """Print a machine-readable JSON Schema."""

    if kind == "config":
        value = DebateConfig.model_json_schema()
    elif kind == "judge":
        schema_path = Path(__file__).with_name("schemas") / "judge-v1.json"
        try:
            value = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _abort(ConfigError(f"Could not read Judge schema {schema_path}: {exc}"))
    else:
        raise typer.BadParameter("kind must be 'config' or 'judge'", param_hint="--kind")
    console.print_json(data=value)


def _validate_adapter_contracts(config: DebateConfig) -> dict[str, str]:
    # Allow unsafe values only so validation can report the plan; execution still
    # requires --allow-unsafe. The constructor also rejects parallel shared writes.
    DebateEngine(
        config,
        allow_unsafe=config.is_unsafe,
        run_preflight=False,
    )
    commands: dict[str, str] = {}
    for agent_id, agent in config.agents.items():
        request = AgentRequest(
            agent_id=agent_id,
            role_id="validation",
            prompt="Validate command construction without executing a model.",
            cwd=config.run.workspace,
            timeout_seconds=agent.timeout_seconds,
            max_output_chars=agent.max_output_chars,
            model=agent.model,
            permission=agent.permission,
        )
        spec = get_adapter(agent.adapter).build_command(request, agent)
        commands[agent_id] = " ".join(spec.display_argv)
    return commands


def _read_task(
    task: str | None,
    task_file: str | None,
    *,
    max_chars: int,
) -> str:
    if task is not None and task_file is not None:
        raise ConfigError("Pass either a task argument or --task-file, not both.")
    if task_file is not None:
        if task_file == "-":
            value = _read_bounded_stream(
                sys.stdin,
                max_chars=max_chars,
                description="stdin",
            )
        else:
            try:
                path = Path(task_file).expanduser()
                with path.open(encoding="utf-8") as file_handle:
                    value = _read_bounded_stream(
                        file_handle,
                        max_chars=max_chars,
                        description=f"task file {task_file!r}",
                    )
            except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
                raise ConfigError(f"Could not read task file {task_file!r}: {exc}") from exc
    elif task is not None:
        value = task
    else:
        try:
            stdin_is_tty = sys.stdin.isatty()
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Could not inspect stdin: {exc}") from exc
        if stdin_is_tty:
            raise ConfigError("Provide a task argument, --task-file, or piped stdin.")
        value = _read_bounded_stream(
            sys.stdin,
            max_chars=max_chars,
            description="stdin",
        )
    if len(value) > max_chars:
        raise ConfigError(
            f"The debate task exceeds context.max_requirement_chars ({max_chars} characters)."
        )
    if not value.strip():
        raise ConfigError("The debate task must contain non-whitespace text.")
    return value.strip()


def _read_bounded_stream(
    stream: TextIO,
    *,
    max_chars: int,
    description: str,
) -> str:
    try:
        value = stream.read(max_chars + 1)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"Could not read {description}: {exc}") from exc
    if not isinstance(value, str):
        raise ConfigError(f"Could not read {description}: expected text input.")
    return value


def _rich_stream(agent_id: str, stream: str, text: str) -> None:
    prefix = Text(f"[{agent_id}:{stream}] ", style="dim" if stream == "stdout" else "yellow")
    content = Text(text)
    console.print(prefix + content, end="")


def _render_result(result: EngineResult) -> None:
    style = "green" if result.converged else "yellow"
    console.print(f"[{style}]Run status: {result.status}[/{style}]")
    console.print(f"Artifacts: {result.run_dir}")
    console.print(f"Stop reason: {result.stop_reason}")
    exit_codes = {
        StopOutcome.EXHAUSTED.value: _EXIT_EXHAUSTED,
        StopOutcome.BLOCKED.value: _EXIT_BLOCKED,
        StopOutcome.TIMED_OUT.value: _EXIT_TIMED_OUT,
    }
    if result.status in exit_codes:
        raise typer.Exit(exit_codes[result.status])


def _abort(error: DebateError) -> None:
    error_console.print(f"[red]{type(error).__name__}:[/red] {error}")
    if isinstance(error, PreflightError):
        code = _EXIT_PREFLIGHT
    elif isinstance(error, ResumeError):
        code = _EXIT_RESUME
    elif isinstance(error, (AgentExecutionError, JudgeProtocolError)):
        code = _EXIT_EXECUTION
    elif isinstance(error, (ConfigError, UnsafeConfigurationError)):
        code = _EXIT_USAGE
    else:
        code = _EXIT_EXECUTION
    raise typer.Exit(code)


if __name__ == "__main__":  # pragma: no cover
    app()
