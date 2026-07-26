from __future__ import annotations

import stat
from pathlib import Path
from typing import NoReturn

import pytest

from agent_debate import initializer
from agent_debate.errors import ConfigError
from agent_debate.initializer import initialize_project


def test_initialize_project_writes_self_contained_starter(tmp_path: Path) -> None:
    created = initialize_project(tmp_path)

    assert tmp_path / "debate.yaml" in created
    assert (tmp_path / "prompts/judge.md").is_file()
    assert stat.S_IMODE((tmp_path / "debate.yaml").stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "prompts").stat().st_mode) == 0o755
    config = (tmp_path / "debate.yaml").read_text(encoding="utf-8")
    assert "permission: read_only" in config
    assert "model:" not in config


def test_initialize_project_refuses_partial_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "debate.yaml"
    existing.write_text("keep me", encoding="utf-8")

    with pytest.raises(ConfigError, match="Refusing to overwrite"):
        initialize_project(tmp_path)

    assert existing.read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "prompts").exists()


def test_initialize_project_force_replaces_files(tmp_path: Path) -> None:
    config = tmp_path / "debate.yaml"
    config.write_text("old", encoding="utf-8")

    initialize_project(tmp_path, force=True)

    assert "schema_version: 1" in config.read_text(encoding="utf-8")


def test_initialize_project_does_not_change_existing_root_mode(tmp_path: Path) -> None:
    tmp_path.chmod(0o751)

    initialize_project(tmp_path)

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o751


def test_initialize_project_rejects_non_directory_parent_before_writing(
    tmp_path: Path,
) -> None:
    blocking_parent = tmp_path / "not-a-directory"
    blocking_parent.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ConfigError, match="parent is not a directory"):
        initialize_project(blocking_parent / "project")

    assert blocking_parent.read_text(encoding="utf-8") == "unchanged"


def test_initialize_project_rejects_non_directory_template_parent(
    tmp_path: Path,
) -> None:
    prompts = tmp_path / "prompts"
    prompts.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ConfigError, match="parent is not a directory"):
        initialize_project(tmp_path, force=True)

    assert prompts.read_text(encoding="utf-8") == "unchanged"
    assert not (tmp_path / "debate.yaml").exists()


def test_initialize_project_rejects_destination_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    real_destination = tmp_path / "real"
    real_destination.mkdir()
    marker = real_destination / "marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    destination_link = tmp_path / "linked-project"
    destination_link.symlink_to(real_destination, target_is_directory=True)

    with pytest.raises(ConfigError, match="cannot contain a symbolic link"):
        initialize_project(destination_link, force=True)

    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert not (real_destination / "debate.yaml").exists()


def test_initialize_project_rejects_symlink_ancestor_without_following_it(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ConfigError, match="cannot contain a symbolic link"):
        initialize_project(linked_parent / "project")

    assert not (real_parent / "project").exists()


@pytest.mark.parametrize("interrupt_at", range(1, 13))
def test_initialize_project_rolls_back_keyboard_interrupt_after_every_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_at: int,
) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir(mode=0o750)
    expected: dict[Path, tuple[bytes, int]] = {}
    for index, relative in enumerate(initializer._TEMPLATE_FILES):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"original-{index}".encode())
        target.chmod(0o600 + index)
        expected[target] = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("leave alone", encoding="utf-8")

    original_replace = Path.replace
    call_count = 0

    def interrupting_replace(source: Path, target: Path) -> Path:
        nonlocal call_count
        result = original_replace(source, target)
        call_count += 1
        if call_count == interrupt_at:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "replace", interrupting_replace)

    with pytest.raises(KeyboardInterrupt):
        initialize_project(tmp_path, force=True)

    for target, (content, mode) in expected.items():
        assert target.read_bytes() == content
        assert stat.S_IMODE(target.stat().st_mode) == mode
    assert stat.S_IMODE(prompts.stat().st_mode) == 0o750
    assert unrelated.read_text(encoding="utf-8") == "leave alone"
    assert not tuple(tmp_path.parent.glob(".agent-debate-init-*"))


def test_initialize_project_rolls_back_non_exception_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CommitAbort(BaseException):
        pass

    config = tmp_path / "debate.yaml"
    config.write_text("original", encoding="utf-8")

    def abort_replace(_source: Path, _target: Path) -> NoReturn:
        raise CommitAbort

    monkeypatch.setattr(Path, "replace", abort_replace)

    with pytest.raises(CommitAbort):
        initialize_project(tmp_path, force=True)

    assert config.read_text(encoding="utf-8") == "original"
    assert not tuple(tmp_path.parent.glob(".agent-debate-init-*"))


def test_initialize_project_restores_absent_destination_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "new-project"
    original_replace = Path.replace
    interrupted = False

    def interrupt_after_replace(source: Path, target: Path) -> Path:
        nonlocal interrupted
        result = original_replace(source, target)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "replace", interrupt_after_replace)

    with pytest.raises(KeyboardInterrupt):
        initialize_project(destination)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".agent-debate-init-*"))


def test_initialize_project_rollback_removes_new_files_and_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "debate.yaml"
    config.write_text("original", encoding="utf-8")
    original_replace = Path.replace
    call_count = 0

    def interrupt_third_replace(source: Path, target: Path) -> Path:
        nonlocal call_count
        result = original_replace(source, target)
        call_count += 1
        if call_count == 3:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Path, "replace", interrupt_third_replace)

    with pytest.raises(KeyboardInterrupt):
        initialize_project(tmp_path, force=True)

    assert config.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "prompts").exists()
    assert not tuple(tmp_path.parent.glob(".agent-debate-init-*"))


def test_initialize_project_preserves_backup_when_rollback_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "debate.yaml"
    config.write_text("original", encoding="utf-8")

    original_replace = Path.replace
    call_count = 0

    def fail_commit_and_rollback(source: Path, target: Path) -> Path:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            original_replace(source, target)
            raise KeyboardInterrupt
        if call_count == 2:
            raise OSError("rollback fault")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_commit_and_rollback)

    with pytest.raises(KeyboardInterrupt):
        initialize_project(tmp_path, force=True)

    staging_roots = tuple(tmp_path.parent.glob(".agent-debate-init-*"))
    assert len(staging_roots) == 1
    backup = staging_roots[0] / "backup/debate.yaml"
    assert backup.read_text(encoding="utf-8") == "original"
