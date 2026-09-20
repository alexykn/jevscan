"""Capture a frozen jevscan run without allowing network transport.

The runner is executed against a temporary copy of the source tree and cache.
Existing cache judgments are retained.  A missing judgment receives a marked
synthetic answer solely so the runner can finish inventorying later targets; the
synthetic answer is never emitted as calibration material.

The output is an intermediate capture, not version-1 calibration JSONL.  Use
``import_cases.py`` with an exact displayed-order adjudication manifest to
create validated cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from jevscan.cli.main import main as run_cli
from jevscan.core.client import JevClient, ReservationUsage
from jevscan.core.config import load_config
from jevscan.core.inference import Inference
from jevscan.core.protocol import Answer, ChoiceAnswer, JevResponse, NoulAnswer, ScoreAnswer, Usage
from jevscan.core.rules import Question

CAPTURE_VERSION = 1
SYNTHETIC_MODEL = "offline-capture-synthetic"
_request_material: list[dict[str, Any]] = []
_synthetic_requests: list[dict[str, Any]] = []
_cache_misses: list[dict[str, Any]] = []
_original_cached_judgments = Inference.cached_judgments


def _blocked_transport(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("network transport is blocked during cache-only capture")


def _synthetic_answer(question: Question) -> Answer:
    if question.type == "noul":
        return NoulAnswer(type="noul", noul=0.0)
    if question.type == "choice":
        choice = next(iter(question.criteria))
        return ChoiceAnswer(
            type="choice",
            choice=choice,
            confidence=1.0,
            probabilities={name: float(name == choice) for name in question.criteria},
        )
    return ScoreAnswer(
        type="score",
        score=0.0,
        confidence=1.0,
        probabilities={str(index): float(index == 0) for index in range(len(question.criteria))},
    )


async def _synthetic_evaluate(
    self: JevClient,
    body: bytes,
    questions: dict[str, Question],
    *,
    reservation: ReservationUsage | None = None,
) -> JevResponse:
    del self, reservation
    _synthetic_requests.append({
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "question_ids": sorted(questions),
    })
    return JevResponse(
        model=SYNTHETIC_MODEL,
        usage=Usage(input_tokens=0, output_tokens=0),
        answers={name: _synthetic_answer(question) for name, question in questions.items()},
    )


async def _cache_miss(
    self: JevClient,
    body: bytes,
    questions: dict[str, Question],
    *,
    reservation: ReservationUsage | None = None,
) -> JevResponse:
    del self, reservation
    _cache_misses.append({
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "question_ids": sorted(questions),
    })
    raise RuntimeError("cache miss during strict offline capture; transport remains blocked")


async def _capture_cached_judgments(
    self: Inference,
    state: bytes,
    question_wires: dict[str, bytes],
    questions: dict[str, Question],
) -> dict[str, tuple[Answer, str]]:
    entry: dict[str, Any] = {
        "state": json.loads(state),
        "state_sha256": hashlib.sha256(state).hexdigest(),
        "questions": {name: json.loads(wire) for name, wire in question_wires.items()},
        "question_ids": sorted(questions),
    }
    cached = await _original_cached_judgments(self, state, question_wires, questions)
    entry["cached"] = {
        name: {"answer": answer.model_dump(mode="json"), "model": model}
        for name, (answer, model) in sorted(cached.items())
    }
    _request_material.append(entry)
    return cached


def _install_guards(allow_synthetic: bool) -> None:
    httpx.AsyncClient.post = _blocked_transport
    JevClient.evaluate = _synthetic_evaluate if allow_synthetic else _cache_miss
    Inference.cached_judgments = _capture_cached_judgments


def _copy_source(source: Path, destination: Path) -> None:
    ignored = shutil.ignore_patterns(
        ".git",
        ".jevscan-cache",
        ".delta",
        ".venv",
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        ".ruff_cache",
    )
    shutil.copytree(source, destination, ignore=ignored)


def _source_commit(source: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 -- fixed read-only Git command
            ["git", "-C", str(source), "rev-parse", "HEAD"],  # noqa: S607 -- fixed executable and arguments
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _assert_clean_source(source: Path) -> str:
    commit = _source_commit(source)
    if commit is None:
        raise ValueError("source_root must be a Git checkout with a readable HEAD")
    try:
        result = subprocess.run(  # noqa: S603 -- fixed read-only Git command
            ["git", "-C", str(source), "status", "--porcelain"],  # noqa: S607 -- fixed executable and arguments
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"cannot verify source checkout cleanliness: {exc}") from exc
    if result.stdout.strip():
        raise ValueError("source_root must be clean; capture the frozen commit without working-tree edits")
    return commit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture frozen jevscan cache judgments while blocking all HTTP transport."
    )
    parser.add_argument("source_root", type=Path, help="frozen checkout to scan")
    parser.add_argument("cache", type=Path, help="existing results.sqlite3; it is copied, never modified")
    parser.add_argument("--events", type=Path, required=True, help="runner JSONL event output")
    parser.add_argument("--capture", type=Path, required=True, help="raw capture JSON output")
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="continue after misses with marked answers for exploratory cache inspection; output remains partial",
    )
    parser.add_argument(
        "--no-enrichment",
        action="store_true",
        help="disable follow-up evidence routing; use only when reproducing a documented baseline invocation",
    )
    return parser


def capture(
    source: Path,
    cache: Path,
    events: Path,
    capture_path: Path,
    *,
    allow_synthetic: bool = False,
    no_enrichment: bool = False,
) -> int:
    source = source.resolve()
    cache = cache.resolve()
    events = events.resolve()
    capture_path = capture_path.resolve()
    if not source.is_dir() or not cache.is_file():
        raise ValueError("source_root must be a directory and cache must be an existing file")

    commit = _assert_clean_source(source)
    _request_material.clear()
    _synthetic_requests.clear()
    _cache_misses.clear()
    _install_guards(allow_synthetic)
    with tempfile.TemporaryDirectory(prefix="jevscan-calibration-") as temporary:
        staging = Path(temporary) / "source"
        _copy_source(source, staging)
        cache_copy = staging / ".jevscan-cache" / "results.sqlite3"
        cache_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache, cache_copy)

        loaded = load_config([staging], None)
        events.parent.mkdir(parents=True, exist_ok=True)
        previous_directory = Path.cwd()
        os.chdir(staging)
        try:
            arguments = [".", "--format", "jsonl", "--verbose", "--output", str(events)]
            if no_enrichment:
                arguments.append("--no-enrichment")
            exit_code = run_cli(arguments)
        finally:
            os.chdir(previous_directory)

        capture_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CAPTURE_VERSION,
            "source_commit": commit,
            "source_root": str(source),
            "requested_model": loaded.config.jev.model,
            "no_enrichment": no_enrichment,
            "endpoint": "https://api.typesafe.ai",
            "rules": {name: rule.model_dump(mode="json") for name, rule in sorted(loaded.config.rules.items())},
            "transport_blocked": True,
            "partial": bool(_cache_misses or _synthetic_requests),
            "cache_misses": _cache_misses,
            "synthetic_model": SYNTHETIC_MODEL,
            "synthetic_requests": _synthetic_requests,
            "requests": _request_material,
        }
        capture_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return exit_code


def main() -> int:
    args = _parser().parse_args()
    try:
        os.environ.setdefault("TYPESAFE_API_KEY", "offline-capture-placeholder")
        exit_code = capture(
            args.source_root,
            args.cache,
            args.events,
            args.capture,
            allow_synthetic=args.allow_synthetic,
            no_enrichment=args.no_enrichment,
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"capture_cache: {exc}")
        return 2
    return 0 if exit_code in {0, 1} else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
