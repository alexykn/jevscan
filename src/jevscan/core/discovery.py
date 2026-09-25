"""Lazy directory traversal with Git-style matching and nested .gitignore scopes."""

import os
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
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


def _local_scopes(directory: Path, inherited: tuple[IgnoreScope, ...], respect: bool) -> tuple[IgnoreScope, ...]:
    local = _read_ignore(directory) if respect else None
    return (*inherited, local) if local is not None else inherited


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
        yield from _walk_target(target, root, scopes, config, include, exclude)


@dataclass(slots=True)
class _DirectoryWalk:
    """Own the depth-first iterator stack and its inherited ignore scopes.

    Opening a child changes the next frame visited. No sibling directory lists
    are retained, and closing the walk releases every live directory descriptor.
    """

    root: Path
    config: ScanConfig
    include: GitIgnoreSpec
    exclude: GitIgnoreSpec
    frames: list[tuple[Path, tuple[IgnoreScope, ...], DirectoryEntries]] = field(default_factory=list)

    def enter(self, directory: Path, inherited: tuple[IgnoreScope, ...]) -> None:
        actual = _local_scopes(directory, inherited, self.config.respect_gitignore)
        self.frames.append((directory, actual, os.scandir(directory)))

    def leave(self) -> None:
        _, _, entries = self.frames.pop()
        entries.close()

    def close(self) -> None:
        while self.frames:
            self.leave()

    def entries(self) -> Iterator[tuple[os.DirEntry[str], tuple[IgnoreScope, ...]] | Diagnostic]:
        while self.frames:
            directory, scopes, entries = self.frames[-1]
            try:
                entry = next(entries)
            except StopIteration:
                self.leave()
                continue
            except OSError as exc:
                self.leave()
                yield Diagnostic(_display(directory, self.root), "discovery-error", str(exc), Severity.ERROR)
                continue
            yield entry, scopes

    def visit(self, entry: os.DirEntry[str], scopes: tuple[IgnoreScope, ...]) -> Iterator[FileJob | Diagnostic]:
        try:
            if entry.is_symlink():
                return
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                relative = _display(path, self.root)
                if not self.exclude.match_file(relative + "/") and not _ignored(path, True, scopes):
                    self.enter(path, scopes)
            elif entry.is_file(follow_symlinks=False):
                yield from _file(path, self.root, scopes, self.include, self.exclude)
        except (OSError, UnicodeError) as exc:
            yield Diagnostic(entry.path, "discovery-error", str(exc), Severity.ERROR)

    def walk(self, target: Path, scopes: tuple[IgnoreScope, ...]) -> Iterator[FileJob | Diagnostic]:
        try:
            self.enter(target, scopes)
            for item in self.entries():
                if isinstance(item, Diagnostic):
                    yield item
                else:
                    yield from self.visit(*item)
        except (OSError, UnicodeError) as exc:
            yield Diagnostic(_display(target, self.root), "discovery-error", str(exc), Severity.ERROR)
        finally:
            self.close()


def _walk_target(
    target: Path,
    root: Path,
    scopes: tuple[IgnoreScope, ...],
    config: ScanConfig,
    include: GitIgnoreSpec,
    exclude: GitIgnoreSpec,
) -> Iterator[FileJob | Diagnostic]:
    if target.is_file():
        # Explicit files still obey filters and explain exclusions.
        actual = _local_scopes(target.parent, scopes, config.respect_gitignore)
        yield from _file(target, root, actual, include, exclude, explicit=True)
    elif target.is_dir():
        yield from _DirectoryWalk(root, config, include, exclude).walk(target, scopes)
    else:
        yield Diagnostic(str(target), "unsupported-target", "target is not a regular file or directory", Severity.ERROR)


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
