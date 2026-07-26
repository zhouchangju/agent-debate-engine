"""Create an editable debate configuration from bundled resources."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final, NoReturn

from agent_debate.errors import ConfigError

_TEMPLATE_FILES: Final[tuple[str, ...]] = (
    "debate.yaml",
    "prompts/architect.md",
    "prompts/alternative.md",
    "prompts/critic.md",
    "prompts/reviewer.md",
    "prompts/judge.md",
)
_SOURCE_FILE_MODE: Final = 0o644
_SOURCE_DIRECTORY_MODE: Final = 0o755


@dataclass
class _CommitState:
    target: Path
    source: Path
    backup: Path
    had_existing: bool


@dataclass
class _CommitJournal:
    """Mutable hand-off between commit code and the outer cleanup guard."""

    cleanup_safe: bool = True


def initialize_project(directory: str | Path, *, force: bool = False) -> tuple[Path, ...]:
    """Write the bundled starter project and return the created paths.

    Templates are materialized in a sibling staging directory first. A new
    project is installed with one atomic rename; updates to an existing
    directory keep rollback copies until every target has been installed.
    Existing directory modes are never changed.
    """

    staging_root: Path | None = None
    journal: _CommitJournal | None = None
    try:
        destination = _lexical_absolute_path(directory)
        destination_existed = _preflight_destination(destination, force=force)
        contents = _read_templates()

        destination.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(destination.parent)
        _require_directory(destination.parent, kind="Project parent")

        staging_root = Path(
            tempfile.mkdtemp(
                prefix=".agent-debate-init-",
                dir=destination.parent,
            )
        )
        payload_root = staging_root / "payload"
        _stage_templates(payload_root, contents)

        journal = _CommitJournal()
        if destination_existed:
            _install_into_existing(
                payload_root,
                staging_root / "backup",
                destination,
                journal,
            )
        else:
            _install_new(payload_root, destination, journal)

        return tuple(destination / relative for relative in _TEMPLATE_FILES)
    except ConfigError:
        raise
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise ConfigError(f"Could not initialize project at {directory!s}: {exc}") from exc
    finally:
        if staging_root is not None and (journal is None or journal.cleanup_safe):
            shutil.rmtree(staging_root, ignore_errors=True)


def _lexical_absolute_path(directory: str | Path) -> Path:
    """Return an absolute path without dereferencing any filesystem component."""

    expanded = Path(directory).expanduser()
    # Path.resolve() is intentionally unsafe here because it dereferences the
    # exact symlinks this initializer promises to reject.
    return Path(os.path.abspath(os.fspath(expanded)))  # noqa: PTH100


def _preflight_destination(destination: Path, *, force: bool) -> bool:
    """Validate all destination types before any project path is created."""

    _reject_symlink_components(destination)
    destination_status = _lstat(destination)
    destination_existed = destination_status is not None
    if destination_status is not None and not stat.S_ISDIR(destination_status.st_mode):
        raise ConfigError(f"Project destination is not a directory: {destination}")

    if not destination_existed:
        ancestor = destination.parent
        ancestor_status = _lstat(ancestor)
        while ancestor_status is None:
            if ancestor == ancestor.parent:  # pragma: no cover - filesystem root guard
                break
            ancestor = ancestor.parent
            ancestor_status = _lstat(ancestor)
        if ancestor_status is None or not stat.S_ISDIR(ancestor_status.st_mode):
            raise ConfigError(f"Project parent is not a directory: {ancestor}")
        return False

    existing: list[Path] = []
    checked_parents: set[Path] = set()
    for relative in _TEMPLATE_FILES:
        target = destination / relative
        parent = target.parent
        while parent != destination:
            if parent not in checked_parents:
                parent_status = _lstat(parent)
                if parent_status is not None and not stat.S_ISDIR(parent_status.st_mode):
                    raise ConfigError(f"Starter file parent is not a directory: {parent}")
                checked_parents.add(parent)
            parent = parent.parent

        target_status = _lstat(target)
        if target_status is not None:
            if not stat.S_ISREG(target_status.st_mode):
                raise ConfigError(f"Starter destination is not a regular file: {target}")
            existing.append(target)

    if existing and not force:
        display = ", ".join(str(path) for path in existing)
        raise ConfigError(f"Refusing to overwrite existing starter files: {display}")
    return True


def _require_directory(path: Path, *, kind: str) -> None:
    path_status = _lstat(path)
    if path_status is None or not stat.S_ISDIR(path_status.st_mode):
        raise ConfigError(f"{kind} is not a directory: {path}")


def _require_absent(path: Path) -> None:
    if _lstat(path) is not None:
        raise ConfigError(f"Project destination appeared during initialization: {path}")


def _require_regular_or_absent(path: Path, path_status: os.stat_result | None) -> None:
    if path_status is not None and not stat.S_ISREG(path_status.st_mode):
        raise ConfigError(f"Starter destination is not a regular file: {path}")


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _reject_symlink_components(path: Path) -> None:
    """Reject an existing symlink anywhere in an absolute lexical path."""

    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        current_status = _lstat(current)
        if current_status is not None and stat.S_ISLNK(current_status.st_mode):
            raise ConfigError(f"Project path cannot contain a symbolic link: {current}")


def _read_templates() -> tuple[tuple[str, str], ...]:
    template_root = files("agent_debate").joinpath("templates")
    contents: list[tuple[str, str]] = []
    for relative in _TEMPLATE_FILES:
        source = template_root.joinpath(*relative.split("/"))
        contents.append((relative, source.read_text(encoding="utf-8")))
    return tuple(contents)


def _stage_templates(
    payload_root: Path,
    contents: tuple[tuple[str, str], ...],
) -> None:
    payload_root.mkdir(mode=_SOURCE_DIRECTORY_MODE)
    payload_root.chmod(_SOURCE_DIRECTORY_MODE)
    for relative, content in contents:
        target = payload_root / relative
        if not target.parent.exists():
            target.parent.mkdir(parents=True, mode=_SOURCE_DIRECTORY_MODE)
            target.parent.chmod(_SOURCE_DIRECTORY_MODE)
        _write_source_file(target, content)


def _write_source_file(path: Path, content: str) -> None:
    data = content.encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        _SOURCE_FILE_MODE,
    )
    try:
        with os.fdopen(descriptor, "wb") as file_handle:
            descriptor = -1
            file_handle.write(data)
            file_handle.flush()
            os.fsync(file_handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    path.chmod(_SOURCE_FILE_MODE)


def _install_into_existing(
    payload_root: Path,
    backup_root: Path,
    destination: Path,
    journal: _CommitJournal,
) -> None:
    created_directories: list[Path] = []
    states: list[_CommitState] = []
    try:
        _reject_symlink_components(destination)
        _require_directory(destination, kind="Project destination")
        for relative in _TEMPLATE_FILES:
            target_parent = (destination / relative).parent
            target_parent_status = _lstat(target_parent)
            if target_parent_status is None:
                target_parent.mkdir(mode=_SOURCE_DIRECTORY_MODE)
                created_directories.append(target_parent)
                target_parent.chmod(_SOURCE_DIRECTORY_MODE)
            else:
                _require_directory(target_parent, kind="Starter file parent")

        for relative in _TEMPLATE_FILES:
            target = destination / relative
            source = payload_root / relative
            backup = backup_root / relative
            target_status = _lstat(target)
            _require_regular_or_absent(target, target_status)
            state = _CommitState(
                target=target,
                source=source,
                backup=backup,
                had_existing=target_status is not None,
            )
            states.append(state)
            if state.had_existing:
                backup.parent.mkdir(parents=True, exist_ok=True)
                target.replace(backup)
            source.replace(target)
    except BaseException as exc:
        rollback_errors = _rollback(states, created_directories, payload_root.parent)
        if rollback_errors:
            journal.cleanup_safe = False
        detail = (
            f"; rollback was incomplete and recovery data was preserved at "
            f"{payload_root.parent}: {'; '.join(rollback_errors)}"
            if rollback_errors
            else ""
        )
        if isinstance(exc, OSError):
            raise ConfigError(f"Could not install starter files: {exc}{detail}") from exc
        if detail:
            exc.add_note(detail.removeprefix("; "))
        raise


def _install_new(
    payload_root: Path,
    destination: Path,
    journal: _CommitJournal,
) -> None:
    """Commit a new directory and restore its prior absence on interruption."""

    _reject_symlink_components(destination)
    _require_absent(destination)
    try:
        payload_root.replace(destination)
    except BaseException as exc:
        rollback_errors: list[str] = []
        payload_status = _lstat(payload_root)
        destination_status = _lstat(destination)
        if payload_status is None and destination_status is not None:
            try:
                destination.replace(payload_root)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{destination}: {rollback_exc}")
        elif payload_status is not None and destination_status is not None:
            rollback_errors.append(
                f"{destination}: destination appeared independently during commit"
            )

        if rollback_errors:
            journal.cleanup_safe = False
            detail = (
                "rollback was incomplete and recovery data was preserved at "
                f"{payload_root.parent}: {'; '.join(rollback_errors)}"
            )
            if isinstance(exc, OSError):
                _raise_config_error(
                    f"Could not install starter directory: {exc}; {detail}",
                    exc,
                )
            exc.add_note(detail)
        elif isinstance(exc, OSError):
            _raise_config_error(f"Could not install starter directory: {exc}", exc)
        raise


def _rollback(
    states: list[_CommitState],
    created_directories: list[Path],
    staging_root: Path,
) -> list[str]:
    errors: list[str] = []
    rollback_root = staging_root / "rollback"
    for index, state in reversed(tuple(enumerate(states))):
        try:
            backup_status = _lstat(state.backup)
            target_status = _lstat(state.target)
            source_status = _lstat(state.source)
            if state.had_existing and backup_status is not None:
                if target_status is not None:
                    displaced = rollback_root / str(index)
                    displaced.parent.mkdir(parents=True, exist_ok=True)
                    state.target.replace(displaced)
                state.backup.replace(state.target)
            elif state.had_existing and target_status is None:
                errors.append(f"{state.target}: original file and backup are missing")
            elif not state.had_existing and source_status is None and target_status is not None:
                displaced = rollback_root / str(index)
                displaced.parent.mkdir(parents=True, exist_ok=True)
                state.target.replace(displaced)
            elif not state.had_existing and source_status is not None and target_status is not None:
                errors.append(f"{state.target}: target appeared independently during commit")
        except BaseException as exc:
            errors.append(f"{state.target}: {exc}")

    for directory in reversed(created_directories):
        try:
            directory.rmdir()
        except BaseException as exc:
            errors.append(f"{directory}: {exc}")
    return errors


def _raise_config_error(message: str, cause: OSError) -> NoReturn:
    raise ConfigError(message) from cause


__all__ = ["initialize_project"]
