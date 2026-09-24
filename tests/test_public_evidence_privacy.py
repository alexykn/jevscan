"""Guard the public evidence set against machine- and source-local leakage."""

from __future__ import annotations

import base64
import fnmatch
import os
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]

# These values are encoded so the regression itself does not publish the
# identifiers it protects.  They cover repository names and distinctive source
# namespace components used by the local-only evidence.
PRIVATE_IDENTIFIERS = tuple(
    base64.b64decode(value).decode()
    for value in (
        "QUFLWg==",
        "bG9va3VwLWRhZW1vbg==",
        "cGlsb3QtZGFlbW9u",
        "d2hp",
        "YWFrel9kYWVtb24=",
        "bG9va3VwX2RhZW1vbg==",
        "cGlsb3RfbnVtYmVyX2RhZW1vbg==",
    )
)

LOCAL_ONLY_PATTERNS = (
    ".jevscan-calibration/**",
    ".jevscan-cache/**",
)

ABSOLUTE_LOCAL_PATH = re.compile(
    r"(?i)(?:/Users/|/home/|/private/var/|\\.delta/worktrees/|file://|"
    r"(?<![A-Za-z0-9])[A-Za-z]:\\\\(?:Users|home|private|workspace))"
)
LOCATOR_FIELD = re.compile(
    r"(?i)(?:source_sha256|target_sha256|source_commit|target_commit|"
    r"target_hash_method)\s*[`\"':=]\s*(?:sha256:)?[0-9a-f]{8,}"
)


def _is_local_only(relative: str) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in LOCAL_ONLY_PATTERNS)


def _public_files() -> list[Path]:
    files: list[Path] = []
    pruned_directories = {
        ".delta",
        ".git",
        ".venv",
        ".ruff_cache",
        ".pytest_cache",
        ".jevscan-cache",
        "__pycache__",
        ".egg-info",
    }
    for directory, subdirectories, filenames in os.walk(ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in pruned_directories]
        for filename in filenames:
            path = Path(directory) / filename
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if (
                relative.startswith((".git", ".venv", ".ruff_cache", ".pytest_cache"))
                or "__pycache__/" in relative
                or relative.endswith(".pyc")
                or ".egg-info/" in relative
                or relative == "tests/test_public_evidence_privacy.py"
                or _is_local_only(relative)
            ):
                continue
            files.append(path)
    return files


def test_public_evidence_has_no_machine_local_paths_or_private_identifiers() -> None:
    leaked: list[str] = []
    for path in _public_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(ROOT).as_posix()
        if ABSOLUTE_LOCAL_PATH.search(text):
            leaked.append(f"{relative}: absolute local path")
        if any(
            re.search(
                rf"(?i)(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])",
                text,
            )
            for identifier in PRIVATE_IDENTIFIERS
        ):
            leaked.append(f"{relative}: private identifier")
    assert not leaked, "\n".join(sorted(leaked))


def test_public_markdown_has_no_real_source_locator_fields() -> None:
    leaked: list[str] = []
    for path in _public_files():
        if path.suffix.lower() not in {".md", ".markdown"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if LOCATOR_FIELD.search(line):
                leaked.append(f"{path.relative_to(ROOT)}:{line_number}")
    assert not leaked, "\n".join(sorted(leaked))
