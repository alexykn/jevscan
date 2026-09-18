"""Lazy directory traversal with Git-style matching and nested .gitignore scopes."""

import os
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pathspec import GitIgnoreSpec

from jevscan.core.config import ScanConfig
from jevscan.core.languages import language_for
from jevscan.core.models import Diagnostic, FileJob, Severity


class DirectoryEntries(Protocol):
    def __next__(self) -> os.DirEntry[str]: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class IgnoreScope:
    directory: Path
    matcher: GitIgnoreSpec


def _display(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def _ignored(path: Path, is_dir: bool, scopes: tuple[IgnoreScope, ...]) -> bool:
    ignored = False
    for scope in scopes:
        if not path.is_relative_to(scope.directory):
            continue
        name = path.relative_to(scope.directory).as_posix() + ("/" if is_dir else "")
        decision = scope.matcher.check_file(name).include
        if decision is not None:
            ignored = decision
    return ignored


def _read_ignore(directory: Path) -> IgnoreScope | None:
    path = directory / ".gitignore"
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as stream:
        return IgnoreScope(directory, GitIgnoreSpec.from_lines(stream))


def _initial_scopes(directory: Path, root: Path, respect: bool) -> tuple[IgnoreScope, ...]:
    if not respect:
        return ()
    chain = []
    current = directory.parent
    while current.is_relative_to(root):
        chain.append(current)
        if current == root:
            break
        current = current.parent
    return tuple(scope for item in reversed(chain) if (scope := _read_ignore(item)) is not None)


def normalize_targets(targets: list[Path]) -> list[Path]:
    """Remove overlapping explicit targets, so the same file is never billed twice."""
    # resolve() would follow symlinks before discover() can enforce the no-symlink policy.
    resolved = sorted({Path(os.path.abspath(path)) for path in targets}, key=lambda p: (len(p.parts), str(p)))  # noqa: PTH100
    result: list[Path] = []
    for path in resolved:
        if not any(parent.is_dir() and path.is_relative_to(parent) for parent in result):
            result.append(path)
    return result


def discover(targets: list[Path], root: Path, config: ScanConfig) -> Generator[FileJob | Diagnostic, None, None]:
    include = GitIgnoreSpec.from_lines(config.include)
    exclude = GitIgnoreSpec.from_lines(config.exclude)
    for target in normalize_targets(targets):
        if target.is_symlink():
            yield Diagnostic(str(target), "symlink-target", "explicit symlink targets are not followed")
            continue
        if not target.exists():
            yield Diagnostic(str(target), "missing-target", "scan target does not exist", Severity.ERROR)
            continue
        directory = target if target.is_dir() else target.parent
        try:
            scopes = _initial_scopes(directory, root, config.respect_gitignore)
        except (OSError, UnicodeError) as exc:
            yield Diagnostic(str(directory), "gitignore-read-error", str(exc), Severity.ERROR)
            continue
        # Iterators, not lists of every directory entry, keep wide trees bounded too.
        yield from _walk_target(target, root, scopes, config, include, exclude)


def _walk_target(
    target: Path,
    root: Path,
    scopes: tuple[IgnoreScope, ...],
    config: ScanConfig,
    include: GitIgnoreSpec,
    exclude: GitIgnoreSpec,
) -> Iterator[FileJob | Diagnostic]:
    if target.is_file():
        # Explicit files still obey filters; print why rather than silently report a clean scan.
        local = _read_ignore(target.parent) if config.respect_gitignore else None
        actual = (*scopes, local) if local is not None else scopes
        yield from _file(target, root, actual, include, exclude, explicit=True)
        return
    if not target.is_dir():
        yield Diagnostic(str(target), "unsupported-target", "target is not a regular file or directory", Severity.ERROR)
        return
    # Keep one open directory iterator per depth, not a list of every sibling directory.
    # Closing the generator releases all descriptors, including on scan cancellation.
    frames: list[tuple[Path, tuple[IgnoreScope, ...], DirectoryEntries]] = []

    def enter(directory: Path, inherited: tuple[IgnoreScope, ...]) -> None:
        local = _read_ignore(directory) if config.respect_gitignore else None
        actual = (*inherited, local) if local is not None else inherited
        frames.append((directory, actual, os.scandir(directory)))

    try:
        enter(target, scopes)
        while frames:
            directory, actual, entries = frames[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                frames.pop()
                continue
            except OSError as exc:
                yield Diagnostic(_display(directory, root), "discovery-error", str(exc), Severity.ERROR)
                entries.close()
                frames.pop()
                continue
            try:
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    relative = _display(path, root)
                    if not exclude.match_file(relative + "/") and not _ignored(path, True, actual):
                        enter(path, actual)
                elif entry.is_file(follow_symlinks=False):
                    yield from _file(path, root, actual, include, exclude)
            except (OSError, UnicodeError) as exc:
                yield Diagnostic(entry.path, "discovery-error", str(exc), Severity.ERROR)
    except (OSError, UnicodeError) as exc:
        yield Diagnostic(_display(target, root), "discovery-error", str(exc), Severity.ERROR)
    finally:
        for _, _, entries in frames:
            entries.close()


def _file(
    path: Path,
    root: Path,
    scopes: tuple[IgnoreScope, ...],
    include: GitIgnoreSpec,
    exclude: GitIgnoreSpec,
    explicit: bool = False,
) -> Iterator[FileJob | Diagnostic]:
    relative = _display(path, root)
    spec = language_for(path)
    selected = include.match_file(relative) and not exclude.match_file(relative) and not _ignored(path, False, scopes)
    if spec and selected:
        yield FileJob(str(path), relative, spec.grammar, spec.language)
    elif explicit:
        yield Diagnostic(relative, "excluded-target", "explicit file is unsupported or excluded by scan filters")
