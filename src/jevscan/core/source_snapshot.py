"""Bounded source reads beneath a trusted root, without following path symlinks."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


def _relative_components(path: str) -> tuple[str, ...]:
    parts = Path(path).parts
    if not parts or Path(path).is_absolute() or any(part in {"..", "."} for part in parts):
        raise OSError("evidence path is not project-relative")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("safe evidence reads require no-follow, directory-relative file access")
    return parts


@contextmanager
def _parent_directory(root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def _read_stable_file(stream: BinaryIO, limit: int) -> bytes:
    before = os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode):
        raise OSError("evidence is not a regular file")
    source = stream.read(limit + 1)
    after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("evidence changed during snapshot read")
    return source


def read_source(root: Path, path: str, limit: int) -> bytes:
    """Return at most limit+1 bytes; the caller owns oversized-file accounting."""
    parts = _relative_components(path)
    with _parent_directory(root, parts[:-1]) as directory:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            return _read_stable_file(stream, limit)
