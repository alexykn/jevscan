"""Bounded live calibration experiment and private case capture.

This file deliberately contains no repository source or adjudicated labels.
Those are supplied by a local JSON manifest and the resulting JSONL cases are
private artifacts.  The workflow has three separate phases:

* ``dev`` evaluates the baseline and three fixed wording bundles;
* ``freeze`` records an explicit development-selected bundle; and
* ``heldout`` evaluates only that frozen bundle.

The live path uses the production parser, context builder, protocol encoder,
client, and assessment/replay code.  It does not alter packaged rules.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from jevscan.core.client import JevClient, ReservationUsage
from jevscan.core.config import BudgetConfig, EvaluationConfig, JevConfig, load_config
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.model_limits import TokenCalibration
from jevscan.core.models import FileJob, ParsedFile, Target
from jevscan.core.parser import parse_source
from jevscan.core.planning import RequestBudget
from jevscan.core.protocol import (
    PROMPT_VERSION,
    QUESTION_POLICY,
    Answer,
    Check,
    JevResponse,
    ScoreAnswer,
    encode,
)
from jevscan.core.rules import NoulQuestion, Rule, ScoreQuestion
from jevscan.core.semantic_calibration import (
    CalibrationCase,
    load_cases,
    replay_case,
    replay_cases,
)

MODEL = "jev-1.13.0"
INPUT_PRICE_PER_MILLION = 0.042
SPEND_CEILING = 1.00
SAFETY_MARGIN = 0.01
MANIFEST_VERSION = 1
MAX_QUESTIONS = 64
BYTES_PER_TOKEN = 3.0
TOKEN_RESERVE = 128

JEV01_BALANCED_A = (
    "Does the target itself implement two or more independently meaningful policies or state transitions "
    "whose handling is interleaved enough to materially harm understanding? Do not count delegated "
    "orchestration, sequential phases, cleanup, metrics, or other cohesive support as mixed responsibilities."
)
JEV01_BALANCED_B = (
    "Does the target combine separate, independently meaningful responsibilities in a way that interleaves "
    "their policy or state decisions and materially harms understanding? A cohesive coordinator, separate "
    "phases, cleanup, or metrics alone is false."
)
JEV02_CLARIFIED = (
    "Select the level that describes how many important execution transitions in the target are obscured by "
    "its control-flow structure. Judge path traceability, not domain difficulty or line count. Level 1 means "
    "all important transitions remain easy to trace and is not, by itself, a warning; use levels 2 or 3 only "
    "when an important transition is actually obscured."
)

CANDIDATE_BUNDLES: dict[str, dict[str, str]] = {
    "baseline": {"JEV01": "baseline", "JEV02": "baseline"},
    "jev01-balanced-a": {"JEV01": "balanced-a", "JEV02": "baseline"},
    "jev01-balanced-b": {"JEV01": "balanced-b", "JEV02": "baseline"},
    "jev02-clarified": {"JEV01": "baseline", "JEV02": "clarified"},
}
REPORT_CANDIDATE = "baseline-report-conservative"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _hash_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json_hash(value: Any) -> str:
    return _sha256_bytes(encode(value))


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _jsonl_dump(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"{field} must be a relative path without '..'")
    return value.replace("\\", "/")


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


@dataclass(frozen=True, slots=True)
class Judgment:
    label: str
    explanation: str
    expected_score: int | None
    adjudicated_severity: str | None


@dataclass(frozen=True, slots=True)
class TargetRecord:
    case_key: str
    repo: str
    root: Path
    relative_path: str
    display_path: str
    owner_group: str
    split: str
    source_sha256: str
    target_sha256: str
    source_commit: str
    parsed: ParsedFile
    target: Target
    judgments: dict[str, Judgment]
    evidence: Evidence
    context_complete: bool
    target_complete: bool


@dataclass(frozen=True, slots=True)
class RequestBatch:
    evidence: Evidence
    records: tuple[TargetRecord, ...]
    questions: dict[str, Any]
    wires: dict[str, bytes]


def _builtin_rules() -> dict[str, Rule]:
    # Use the packaged configuration as the sole semantic source of truth.
    return load_config([PROJECT_ROOT], cwd=PROJECT_ROOT).config.rules


def _variant_rule(base: Rule, variant: str) -> Rule:
    question = base.question
    if base.title == "mixed-responsibilities" and variant != "baseline":
        assert isinstance(question, NoulQuestion)
        return base.model_copy(
            update={
                "question": question.model_copy(
                    update={"instructions": JEV01_BALANCED_A if variant == "balanced-a" else JEV01_BALANCED_B}
                )
            }
        )
    if base.title == "unclear-control-flow" and variant == "clarified":
        assert isinstance(question, ScoreQuestion)
        return base.model_copy(update={"question": question.model_copy(update={"instructions": JEV02_CLARIFIED})})
    return base


def _selected_report_policies(rules: dict[str, Rule]) -> dict[str, dict[str, Any]]:
    """Return the frozen reporting-only policy selected from development answers."""
    jev01 = rules["JEV01"].report
    jev01_levels = jev01.levels.model_copy(
        update={"warning": jev01.levels.warning.model_copy(update={"min_probability": 0.6})}
    )
    jev01_policy = jev01.model_copy(update={"levels": jev01_levels})

    jev02 = rules["JEV02"].report
    jev02_levels = jev02.levels.model_copy(
        update={
            "warning": jev02.levels.warning.model_copy(
                update={
                    "score_levels": [2, 3],
                    "min_probability": 0.6,
                    "min_confidence": 0.6,
                    "min_score": None,
                    "max_score": None,
                }
            ),
            "error": jev02.levels.error.model_copy(
                update={
                    "score_levels": [3],
                    "min_probability": 0.8,
                    "min_confidence": 0.8,
                    "min_score": None,
                    "max_score": None,
                }
            ),
        }
    )
    jev02_policy = jev02.model_copy(update={"levels": jev02_levels})
    return {
        "JEV01": jev01_policy.model_dump(mode="json"),
        "JEV02": jev02_policy.model_dump(mode="json"),
    }


def _policy_metrics(cases: list[CalibrationCase], overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    """Measure reporting candidates without inventing another assessment path."""
    result: dict[str, Any] = {}
    for rule_id in ("JEV01", "JEV02"):
        selected = [case for case in cases if case.rule_id == rule_id]
        report_override = overrides.get(rule_id) if overrides else None
        records = [replay_case(case, report_override) for case in selected]
        if rule_id == "JEV01":
            positive = [record for record in records if record.case.label == "Agree"]
            partial = [record for record in records if record.case.label == "Partial"]
        else:
            positive = [record for record in records if record.case.provenance.get("expected_score", 0) >= 2]
            partial = []
        confirmed = [record for record in records if record.assessment.finding is not None]
        tentative = [record for record in records if record.assessment.tentative_finding is not None]
        review = confirmed + tentative
        result[rule_id] = {
            "cases": len(records),
            "positive_denominator": len(positive),
            "confirmed_signal": len(confirmed),
            "tentative_signal": len(tentative),
            "confirmed_true_positive": sum(record in positive for record in confirmed),
            "review_true_positive": sum(record in positive for record in review),
            "confirmed_precision": _fraction(sum(record in positive for record in confirmed), len(confirmed)),
            "confirmed_positive_recall": _fraction(sum(record in positive for record in confirmed), len(positive)),
            "combined_positive_recall": _fraction(sum(record in positive for record in review), len(positive)),
            "partial_denominator": len(partial),
            "partial_confirmed_signal": sum(record in partial for record in confirmed),
            "partial_tentative_signal": sum(record in partial for record in tentative),
            "disagree_confirmed_signal": sum(
                record.case.label == "Disagree" and record.assessment.finding is not None for record in records
            ),
            "disagree_tentative_signal": sum(
                record.case.label == "Disagree" and record.assessment.tentative_finding is not None
                for record in records
            ),
        }
    return result


def _score_distributions(cases: list[CalibrationCase]) -> dict[str, Any]:
    """Retain raw score evidence; nearest and argmax are explicitly diagnostics."""
    scores = [(case, case.answer) for case in cases if case.rule_id == "JEV02" and isinstance(case.answer, ScoreAnswer)]
    raw = Counter(str(answer.score) for _, answer in scores)
    expected = Counter(str(case.provenance.get("expected_score")) for case, _ in scores)
    nearest_errors = 0
    argmax_errors = 0
    for case, answer in scores:
        expected_score = case.provenance.get("expected_score")
        if not isinstance(expected_score, int):
            continue
        nearest = min(len(answer.probabilities) - 1, max(0, math.floor(answer.score + 0.5)))
        argmax = max(answer.probabilities, key=lambda label: (answer.probabilities[label], -int(label)))
        nearest_errors += nearest != expected_score
        argmax_errors += int(argmax) != expected_score
    return {
        "raw_score_counts": dict(sorted(raw.items())),
        "expected_score_counts": dict(sorted(expected.items())),
        "nearest_level_error": {"count": nearest_errors, "denominator": len(scores)},
        "argmax_level_error": {"count": argmax_errors, "denominator": len(scores)},
        "note": "nearest-level and argmax counts are diagnostics; raw provider scores remain authoritative.",
    }


def _load_manifest(path: Path) -> tuple[dict[str, Any], list[TargetRecord]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != MANIFEST_VERSION:
        raise ValueError(f"manifest version {MANIFEST_VERSION} is required")
    if raw.get("model", MODEL) != MODEL:
        raise ValueError(f"manifest model must be {MODEL!r}")
    entries = raw.get("targets")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest.targets must be a nonempty list")
    rules = _builtin_rules()
    records: list[TargetRecord] = []
    seen_keys: set[str] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TypeError(f"targets[{index}] must be an object")
        repo = _required_string(entry.get("repo"), f"targets[{index}].repo")
        root = Path(_required_string(entry.get("root"), f"targets[{index}].root")).expanduser().resolve()
        relative_path = _relative_path(entry.get("path"), f"targets[{index}].path")
        source_path = root / relative_path
        if not source_path.is_file():
            raise ValueError(f"targets[{index}] source file does not exist: {source_path}")
        display_path = f"{repo}/{relative_path}"
        language = _required_string(entry.get("language"), f"targets[{index}].language").lower()
        grammar = _required_string(entry.get("grammar", language), f"targets[{index}].grammar").lower()
        if grammar not in {"rust", "typescript", "tsx", "javascript", "python", "perl"}:
            raise ValueError(f"targets[{index}].grammar is unsupported: {grammar}")
        if language not in {"rust", "typescript", "javascript", "python", "perl"}:
            raise ValueError(f"targets[{index}].language is unsupported: {language}")
        source = source_path.read_bytes()
        source_hash = _sha256_bytes(source)
        declared_source_hash = _required_string(entry.get("source_sha256"), f"targets[{index}].source_sha256")
        if source_hash != declared_source_hash:
            raise ValueError(
                f"{display_path}: source hash mismatch (manifest {declared_source_hash}, current {source_hash})"
            )
        parsed = parse_source(source, FileJob(str(source_path), display_path, grammar, language))
        if parsed.failed or parsed.diagnostics:
            diagnostics = ", ".join(f"{item.code}@{item.line}" for item in parsed.diagnostics)
            raise ValueError(f"{display_path}: parser did not produce clean source ({diagnostics or 'failed'})")
        qualified_name = _required_string(entry.get("qualified_name"), f"targets[{index}].qualified_name")
        matches = [unit for unit in parsed.units if unit.qualified_name == qualified_name]
        if len(matches) != 1:
            raise ValueError(
                f"{display_path}: expected one exact qualified target {qualified_name!r}, found {len(matches)}"
            )
        target = Target.from_unit(matches[0])
        expected_kind = entry.get("kind")
        if expected_kind is not None and str(target.kind) != expected_kind:
            raise ValueError(f"{display_path}:{qualified_name}: kind mismatch")
        review_start_line = entry.get("review_start_line", target.start_line)
        review_end_line = entry.get("review_end_line", target.end_line)
        if (
            type(review_start_line) is not int
            or type(review_end_line) is not int
            or review_start_line < 1
            or review_end_line < review_start_line
        ):
            raise ValueError(f"{display_path}:{qualified_name}: invalid review line range")
        source_lines = source.splitlines(keepends=True)
        if review_end_line > len(source_lines):
            raise ValueError(f"{display_path}:{qualified_name}: review line range exceeds source")
        hash_method = entry.get("hash_method", "line_slice")
        if hash_method == "byte_span":
            review_bytes = source[target.start_byte : target.end_byte]
        elif hash_method == "line_slice":
            # Blind-review records use inclusive UTF-8 line slices, including
            # each line terminator.  This is separate from the production byte
            # span, which remains the target used for evidence and attribution.
            review_bytes = b"".join(source_lines[review_start_line - 1 : review_end_line])
        else:
            raise ValueError(f"{display_path}:{qualified_name}: unsupported hash_method {hash_method!r}")
        target_hash = _sha256_bytes(review_bytes)
        declared_target_hash = _required_string(entry.get("target_sha256"), f"targets[{index}].target_sha256")
        if target_hash != declared_target_hash:
            raise ValueError(
                f"{display_path}:{qualified_name}: target hash mismatch (manifest {declared_target_hash}, current {target_hash})"
            )
        owner_group = _required_string(entry.get("owner_group"), f"targets[{index}].owner_group")
        split = _required_string(entry.get("split"), f"targets[{index}].split").lower()
        if split not in {"dev", "heldout"}:
            raise ValueError(f"{display_path}:{qualified_name}: split must be dev or heldout")
        case_key = f"{repo}|{relative_path}|{qualified_name}"
        if case_key in seen_keys:
            raise ValueError(f"duplicate target {case_key}")
        seen_keys.add(case_key)
        judgment_values = entry.get("judgments")
        if not isinstance(judgment_values, dict) or not judgment_values:
            raise ValueError(f"{case_key}: judgments must be a nonempty mapping")
        judgments: dict[str, Judgment] = {}
        for rule_id, value in judgment_values.items():
            if rule_id not in {"JEV01", "JEV02"} or rule_id not in rules:
                raise ValueError(f"{case_key}: unsupported judgment rule {rule_id!r}")
            if not isinstance(value, dict):
                raise TypeError(f"{case_key}:{rule_id}: judgment must be an object")
            label = _required_string(value.get("label"), f"{case_key}:{rule_id}.label")
            if label not in {"Agree", "Partial", "Disagree"}:
                raise ValueError(f"{case_key}:{rule_id}: label must be Agree, Partial, or Disagree")
            expected_score = value.get("expected_score")
            if expected_score is not None and (type(expected_score) is not int or not 0 <= expected_score <= 63):
                raise ValueError(f"{case_key}:{rule_id}: expected_score must be a nonnegative integer")
            severity = value.get("adjudicated_severity")
            if severity is not None and severity not in {"warning", "error"}:
                raise ValueError(f"{case_key}:{rule_id}: invalid adjudicated_severity")
            if label == "Disagree" and severity is not None:
                raise ValueError(f"{case_key}:{rule_id}: Disagree cannot carry severity")
            judgments[rule_id] = Judgment(
                label,
                _required_string(value.get("explanation"), f"{case_key}:{rule_id}.explanation"),
                expected_score,
                severity,
            )
        context = ContextBuilder(parsed)
        representative = Check(case_key, target, "JEV01", rules["JEV01"])
        evidence = context.requested(representative)
        context_complete = evidence.contains(
            context.requested_target(representative).path, target.start_byte, target.end_byte
        )
        target_complete = evidence.contains(target.path, target.start_byte, target.end_byte)
        records.append(
            TargetRecord(
                case_key,
                repo,
                root,
                relative_path,
                display_path,
                owner_group,
                split,
                source_hash,
                target_hash,
                str(entry.get("source_commit", "")),
                parsed,
                target,
                judgments,
                evidence,
                context_complete,
                target_complete,
            )
        )

    _validate_split(records)
    metadata = {
        "manifest_path": str(path),
        "manifest_sha256": _hash_file(path),
        "model": MODEL,
        "source_roots": sorted({str(record.root) for record in records}),
        "target_hash_method": "inclusive UTF-8 line slice with line terminators",
        "split_validation": _split_metadata(records),
    }
    return metadata, records


def _validate_split(records: list[TargetRecord]) -> None:
    by_split = {split: [record for record in records if record.split == split] for split in ("dev", "heldout")}
    if not by_split["dev"] or not by_split["heldout"]:
        raise ValueError("manifest must contain both dev and heldout records")
    dev_groups = {record.owner_group for record in by_split["dev"]}
    heldout_groups = {record.owner_group for record in by_split["heldout"]}
    if overlap := sorted(dev_groups & heldout_groups):
        raise ValueError(f"owner groups leak across dev/heldout: {', '.join(overlap)}")
    dev_sources = {record.source_sha256 for record in by_split["dev"]}
    heldout_sources = {record.source_sha256 for record in by_split["heldout"]}
    if overlap := sorted(dev_sources & heldout_sources):
        raise ValueError(f"source files leak across dev/heldout: {', '.join(overlap)}")


def _split_metadata(records: list[TargetRecord]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for split in ("dev", "heldout"):
        selected = [record for record in records if record.split == split]
        result[split] = {
            "targets": len(selected),
            "owner_groups": sorted({record.owner_group for record in selected}),
            "source_sha256": sorted({record.source_sha256 for record in selected}),
            "target_sha256": sorted(record.target_sha256 for record in selected),
        }
    return result


def _question_id(record: TargetRecord, candidate: str, rule_id: str) -> str:
    return f"{record.case_key}::{candidate}::{rule_id}"


def _request_batches(
    records: list[TargetRecord], candidates: str | tuple[str, ...], rules: dict[str, Rule]
) -> list[RequestBatch]:
    candidate_names = (candidates,) if isinstance(candidates, str) else candidates
    groups: dict[str, list[TargetRecord]] = defaultdict(list)
    for record in records:
        groups[record.evidence.key].append(record)
    batches: list[RequestBatch] = []
    for evidence_key in sorted(groups):
        group = sorted(groups[evidence_key], key=lambda item: item.case_key)
        current: list[TargetRecord] = []
        current_questions: dict[str, Any] = {}
        current_wires: dict[str, bytes] = {}
        for record in group:
            proposed = dict(current_questions)
            proposed_wires = dict(current_wires)
            for candidate in candidate_names:
                for rule_id in sorted(record.judgments):
                    variant = CANDIDATE_BUNDLES[candidate][rule_id]
                    rule = _variant_rule(rules[rule_id], variant)
                    question_id = _question_id(record, candidate, rule_id)
                    proposed[question_id] = rule.question
                    proposed_wires[question_id] = encode(
                        Check(record.case_key, record.target, rule_id, rule).question()
                    )
            if current and len(proposed) > MAX_QUESTIONS:
                batches.append(RequestBatch(record.evidence, tuple(current), current_questions, current_wires))
                current, current_questions, current_wires = [], {}, {}
                proposed = {}
                proposed_wires = {}
                for candidate in candidate_names:
                    for rule_id in sorted(record.judgments):
                        variant = CANDIDATE_BUNDLES[candidate][rule_id]
                        rule = _variant_rule(rules[rule_id], variant)
                        question_id = _question_id(record, candidate, rule_id)
                        proposed[question_id] = rule.question
                        proposed_wires[question_id] = encode(
                            Check(record.case_key, record.target, rule_id, rule).question()
                        )
            current.append(record)
            current_questions, current_wires = proposed, proposed_wires
        if current:
            batches.append(RequestBatch(group[0].evidence, tuple(current), current_questions, current_wires))
    return batches


def _build_body(batch: RequestBatch, config: JevConfig) -> bytes:
    budget = RequestBudget(EvaluationConfig(max_questions=MAX_QUESTIONS), config.model, TokenCalibration())
    return budget.body(batch.evidence.encoded, batch.wires)


def _case(
    record: TargetRecord,
    rule_id: str,
    candidate: str,
    answer: Answer,
    requested_model: str,
    returned_model: str,
    endpoint: str,
    rules: dict[str, Rule],
) -> CalibrationCase:
    variant = CANDIDATE_BUNDLES[candidate][rule_id]
    rule = _variant_rule(rules[rule_id], variant)
    judgment = record.judgments[rule_id]
    case_id = _question_id(record, candidate, rule_id)
    prompt = {"version": PROMPT_VERSION, "policy": QUESTION_POLICY}
    source_documents = {record.display_path: record.parsed.source.decode("utf-8")}
    hashes = {
        "question": f"sha256:{_json_hash(Check(record.case_key, record.target, rule_id, rule).question())}",
        "evidence": f"sha256:{_json_hash(record.evidence.state)}",
        "source_documents": {record.display_path: f"sha256:{record.source_sha256}"},
        "rule": f"sha256:{_json_hash(rule.model_dump(mode='json'))}",
        "report": f"sha256:{_json_hash(rule.report.model_dump(mode='json'))}",
        "prompt": f"sha256:{_json_hash(prompt)}",
        "endpoint": f"sha256:{_sha256_text(endpoint)}",
        "requested_model": f"sha256:{_sha256_text(requested_model)}",
        "returned_model": f"sha256:{_sha256_text(returned_model)}",
    }
    return CalibrationCase.model_validate({
        "version": 1,
        "case_id": case_id,
        "rule_id": rule_id,
        "rule": rule.model_dump(mode="json"),
        "target": record.target.metadata(),
        "answer": answer.model_dump(mode="json"),
        "context_complete": record.context_complete,
        "target_complete": record.target_complete,
        "split": record.split,
        "label": judgment.label,
        "adjudicated_severity": judgment.adjudicated_severity,
        "explanation": judgment.explanation,
        "provenance": {
            "source": "private-blind-review",
            "repo": record.repo,
            "relative_path": record.relative_path,
            "source_commit": record.source_commit,
            "source_sha256": record.source_sha256,
            "target_sha256": record.target_sha256,
            "owner_group": record.owner_group,
            "candidate": candidate,
            "variant": variant,
            "expected_score": judgment.expected_score,
        },
        "evidence": {"state": record.evidence.state, "source_documents": source_documents},
        "prompt": prompt,
        "endpoint": endpoint,
        "requested_model": requested_model,
        "returned_model": returned_model,
        "hashes": hashes,
        "comparability": {
            "question": hashes["question"],
            "evidence": hashes["evidence"],
            "prompt": {"version": PROMPT_VERSION, "identity": hashes["prompt"]},
            "endpoint": endpoint,
            "model": returned_model,
        },
    })


def _fraction(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(
    cases: list[CalibrationCase],
    candidate: str,
    split: str,
    report_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = [case for case in cases if case.split == split and case.provenance.get("candidate") == candidate]
    by_rule: dict[str, list[CalibrationCase]] = defaultdict(list)
    for case in selected:
        by_rule[case.rule_id].append(case)
    result: dict[str, Any] = {}
    for rule_id, rule_cases in sorted(by_rule.items()):
        override = report_overrides.get(rule_id) if report_overrides else None
        records = [replay_case(case, override) for case in rule_cases]
        confirmed = sum(record.assessment.finding is not None for record in records)
        tentative = sum(record.assessment.tentative_finding is not None for record in records)
        entry: dict[str, Any] = {
            "cases": len(records),
            "confirmed_signal": confirmed,
            "tentative_signal": tentative,
            "confirmed_or_tentative_signal": sum(
                record.assessment.finding is not None or record.assessment.tentative_finding is not None
                for record in records
            ),
        }
        if rule_id == "JEV01":
            agree = [record for record in records if record.case.label == "Agree"]
            disagree = [record for record in records if record.case.label == "Disagree"]
            partial = [record for record in records if record.case.label == "Partial"]
            entry.update({
                "agree_confirmed_recall": _fraction(
                    sum(record.assessment.finding is not None for record in agree), len(agree)
                ),
                "agree_review_list_recall": _fraction(
                    sum(
                        record.assessment.finding is not None or record.assessment.tentative_finding is not None
                        for record in agree
                    ),
                    len(agree),
                ),
                "disagree_confirmed_rate": _fraction(
                    sum(record.assessment.finding is not None for record in disagree), len(disagree)
                ),
                "partial_confirmed_rate": _fraction(
                    sum(record.assessment.finding is not None for record in partial), len(partial)
                ),
                "partial_review_list_rate": _fraction(
                    sum(
                        record.assessment.finding is not None or record.assessment.tentative_finding is not None
                        for record in partial
                    ),
                    len(partial),
                ),
            })
        elif rule_id == "JEV02":
            exact = []
            high_mass: list[float] = []
            expected_high: list[bool] = []
            for record in records:
                answer = record.case.answer
                expected = record.case.provenance.get("expected_score")
                if (
                    not isinstance(answer.model_dump(mode="python"), dict)
                    or answer.type != "score"
                    or not isinstance(expected, int)
                ):
                    continue
                probabilities = answer.probabilities
                high_mass.append(sum(probabilities[str(level)] for level in (2, 3) if str(level) in probabilities))
                expected_high.append(expected >= 2)
                nearest_score = min(len(probabilities) - 1, max(0, math.floor(answer.score + 0.5)))
                exact.append(nearest_score == expected)
            predicted_high = [mass >= 0.5 for mass in high_mass]
            entry.update({
                "exact_expected_score": _fraction(sum(exact), len(exact)),
                "mean_mass_levels_2_plus_3": (sum(high_mass) / len(high_mass)) if high_mass else None,
                "mass_2_plus_3_candidate_threshold": 0.5,
                "mass_high_precision": _fraction(
                    sum(
                        predicted and expected
                        for predicted, expected in zip(predicted_high, expected_high, strict=True)
                    ),
                    sum(predicted_high),
                ),
                "mass_high_recall": _fraction(
                    sum(
                        predicted and expected
                        for predicted, expected in zip(predicted_high, expected_high, strict=True)
                    ),
                    sum(expected_high),
                ),
                "expected_score_counts": {
                    str(score): sum(record.case.provenance.get("expected_score") == score for record in records)
                    for score in range(4)
                },
                "confirmed_signal_on_expected_high": sum(
                    record.assessment.finding is not None
                    for record in records
                    if record.case.provenance.get("expected_score", 0) >= 2
                ),
                "tentative_signal_on_expected_high": sum(
                    record.assessment.tentative_finding is not None
                    for record in records
                    if record.case.provenance.get("expected_score", 0) >= 2
                ),
                "confirmed_signal_on_expected_level1": sum(
                    record.assessment.finding is not None
                    for record in records
                    if record.case.provenance.get("expected_score") == 1
                ),
                "tentative_signal_on_expected_level1": sum(
                    record.assessment.tentative_finding is not None
                    for record in records
                    if record.case.provenance.get("expected_score") == 1
                ),
            })
        result[rule_id] = entry
    # Keep the production replay report available without imposing a pass gate.
    result["production_replay"] = replay_cases(selected).as_dict()["rules"]
    return result


async def _capture_live(
    records: list[TargetRecord],
    candidates: str | tuple[str, ...],
    rules: dict[str, Rule],
    endpoint: str,
    requested_model: str,
    api_key: str,
    max_cost: float,
) -> tuple[list[CalibrationCase], list[dict[str, Any]], dict[str, Any]]:
    jev_config = JevConfig(model=requested_model, concurrency=1, requests_per_minute=600, retries=1)
    budget_config = BudgetConfig(
        max_requests=256,
        max_input_tokens=math.floor(max_cost * 1_000_000 / INPUT_PRICE_PER_MILLION),
        max_cost=max_cost,
        input_cost_per_million=INPUT_PRICE_PER_MILLION,
    )
    candidate_names = (candidates,) if isinstance(candidates, str) else candidates
    batches = _request_batches(records, candidate_names, rules)
    planned: list[dict[str, Any]] = []
    for index, batch in enumerate(batches):
        body = _build_body(batch, jev_config)
        planned.append({
            "request_index": index,
            "evidence_sha256": batch.evidence.key,
            "question_count": len(batch.questions),
            "question_ids": sorted(batch.questions),
            "body_sha256": _sha256_bytes(body),
            "estimated_reserved_input_tokens": math.ceil(len(body) / BYTES_PER_TOKEN) + TOKEN_RESERVE,
        })
    planned_cost = (
        sum(item["estimated_reserved_input_tokens"] for item in planned) * INPUT_PRICE_PER_MILLION / 1_000_000
    )
    if planned_cost > max_cost:
        raise RuntimeError(f"preflight reservation ${planned_cost:.6f} exceeds remaining budget ${max_cost:.6f}")

    cases: list[CalibrationCase] = []
    receipts: list[dict[str, Any]] = []
    async with JevClient(
        jev_config,
        api_key,
        base_url=endpoint,
        budget=budget_config,
        bytes_per_token=BYTES_PER_TOKEN,
        token_reserve=TOKEN_RESERVE,
    ) as client:
        for item, batch in zip(planned, batches, strict=True):
            body = _build_body(batch, jev_config)
            reservation = ReservationUsage()
            response: JevResponse = await client.evaluate(body, batch.questions, reservation=reservation)
            for record in batch.records:
                for candidate in candidate_names:
                    for rule_id in sorted(record.judgments):
                        answer = response.answers[_question_id(record, candidate, rule_id)]
                        cases.append(
                            _case(record, rule_id, candidate, answer, requested_model, response.model, endpoint, rules)
                        )
            reported_input = response.usage.input_tokens
            reported_output = response.usage.output_tokens
            receipts.append({
                **item,
                "candidates": list(candidate_names),
                "returned_model": response.model,
                "reserved_input_tokens": reservation.input_tokens,
                "reported_input_tokens": reported_input,
                "reported_output_tokens": reported_output,
                "reserved_cost": reservation.input_tokens * INPUT_PRICE_PER_MILLION / 1_000_000,
                "reported_cost": (
                    reported_input * INPUT_PRICE_PER_MILLION / 1_000_000 if reported_input is not None else None
                ),
            })
    total_reserved = sum(item["reserved_cost"] for item in receipts)
    total_reported = sum(item["reported_cost"] or 0 for item in receipts)
    return (
        cases,
        receipts,
        {
            "requests": len(receipts),
            "planned_reserved_cost": planned_cost,
            "reserved_cost": total_reserved,
            "reported_cost": total_reported,
            "reserved_input_tokens": sum(item["reserved_input_tokens"] for item in receipts),
            "reported_input_tokens": sum(item["reported_input_tokens"] or 0 for item in receipts),
            "reported_output_tokens": sum(item["reported_output_tokens"] or 0 for item in receipts),
        },
    )


def _plan_only(
    records: list[TargetRecord], candidates: str | tuple[str, ...], rules: dict[str, Rule], endpoint: str
) -> dict[str, Any]:
    config = JevConfig(model=MODEL, concurrency=1)
    planned = []
    candidate_names = (candidates,) if isinstance(candidates, str) else candidates
    for index, batch in enumerate(_request_batches(records, candidate_names, rules)):
        body = _build_body(batch, config)
        planned.append({
            "request_index": index,
            "evidence_sha256": batch.evidence.key,
            "question_count": len(batch.questions),
            "body_sha256": _sha256_bytes(body),
            "estimated_reserved_input_tokens": math.ceil(len(body) / BYTES_PER_TOKEN) + TOKEN_RESERVE,
        })
    reserved = sum(int(item["estimated_reserved_input_tokens"]) for item in planned)
    return {
        "endpoint": endpoint,
        "model": MODEL,
        "candidates": list(candidate_names),
        "requests": len(planned),
        "reserved_input_tokens": reserved,
        "reserved_cost": reserved * INPUT_PRICE_PER_MILLION / 1_000_000,
        "batches": planned,
    }


def _manifest_records(path: Path, split: str) -> tuple[dict[str, Any], list[TargetRecord]]:
    metadata, records = _load_manifest(path)
    selected = [record for record in records if record.split == split]
    return metadata, selected


def _write_dev_ledger(
    path: Path,
    metadata: dict[str, Any],
    records: list[TargetRecord],
    candidate_runs: dict[str, dict[str, Any]],
    private_cases: Path | None,
) -> None:
    _json_dump(
        path,
        {
            "version": 1,
            "created_at": _now(),
            "manifest": metadata,
            "model": MODEL,
            "input_cost_per_million": INPUT_PRICE_PER_MILLION,
            "spend_ceiling": SPEND_CEILING,
            "safety_margin": SAFETY_MARGIN,
            "candidate_order": list(CANDIDATE_BUNDLES),
            "rounds": [
                ["baseline"],
                ["jev01-balanced-a", "jev01-balanced-b"],
                ["jev02-clarified"],
            ],
            "dev_targets": len(records),
            "private_cases": str(private_cases) if private_cases else None,
            "candidate_runs": candidate_runs,
            "total_reserved_cost": sum(
                run.get("spend", {}).get("reserved_cost", 0.0) for run in candidate_runs.values()
            ),
            "candidate_frozen": False,
        },
    )


def cmd_validate(args: argparse.Namespace) -> int:
    metadata, records = _load_manifest(args.manifest)
    print(
        json.dumps(
            {
                "manifest_sha256": metadata["manifest_sha256"],
                "model": MODEL,
                "targets": len(records),
                "split_validation": metadata["split_validation"],
                "source_roots": metadata["source_roots"],
            },
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    all_cases = load_cases(args.cases)
    dev_cases = [case for case in all_cases if case.split == "dev" and case.provenance.get("candidate") == "baseline"]
    if not dev_cases:
        raise ValueError("development cases contain no baseline candidate")
    rules = _builtin_rules()
    policies = _selected_report_policies(rules)
    selection = {
        "version": 1,
        "created_at": _now(),
        "question_candidate": "baseline",
        "report_candidate": REPORT_CANDIDATE,
        "report_policies": policies,
        "report_policy_sha256": f"sha256:{_json_hash(policies)}",
        "baseline_metrics": _policy_metrics(dev_cases, None),
        "selected_metrics": _policy_metrics(dev_cases, policies),
        "score_distributions": _score_distributions(dev_cases),
        "dev_cases": len(dev_cases),
        "rationale": (
            "Keep the baseline question wording: both balanced JEV01 variants erased the only Agree signal. "
            "Raise JEV01 warning probability to 0.6 to remove tentative-only noise while retaining the sole "
            "confirmed positive. For JEV02, report probability mass on levels 2+3, keep the current 0.6 "
            "confidence gate, require 0.8 mass and confidence for level-3 error, and do not make traceable "
            "level 1 a warning. These are reporting-only thresholds; no rigid pass gate is applied."
        ),
    }
    _json_dump(args.output, selection)
    print(json.dumps(selection, indent=2))
    return 0


def cmd_dev(args: argparse.Namespace) -> int:
    metadata, records = _manifest_records(args.manifest, "dev")
    rules = _builtin_rules()
    endpoint = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
    candidate_runs: dict[str, dict[str, Any]] = {}
    all_cases: list[CalibrationCase] = []
    all_receipts: list[dict[str, Any]] = []
    total_reserved = 0.0
    remaining = SPEND_CEILING - SAFETY_MARGIN
    rounds = (("baseline",), ("jev01-balanced-a", "jev01-balanced-b"), ("jev02-clarified",))
    for round_number, candidates in enumerate(rounds, 1):
        plan = _plan_only(records, candidates, rules, endpoint)
        if args.plan:
            for candidate in candidates:
                candidate_runs[candidate] = {
                    "round": round_number,
                    "shared_round_candidates": list(candidates),
                    "plan": plan,
                    "metrics": None,
                    "receipts": [],
                }
            continue
        api_key = os.environ.get("TYPESAFE_API_KEY")
        if not api_key:
            raise RuntimeError("TYPESAFE_API_KEY is unavailable; development capture was not executed")
        cases, receipts, spend = asyncio.run(
            _capture_live(records, candidates, rules, endpoint, MODEL, api_key, remaining)
        )
        for index, candidate in enumerate(candidates):
            candidate_cases = [case for case in cases if case.provenance.get("candidate") == candidate]
            candidate_runs[candidate] = {
                "round": round_number,
                "shared_round_candidates": list(candidates),
                "plan": plan,
                # Charge a combined round once, not once per candidate.
                "spend": spend if index == 0 else {},
                "receipts": receipts if index == 0 else [],
                "metrics": _metrics(candidate_cases, candidate, "dev"),
            }
        all_cases.extend(cases)
        all_receipts.extend(receipts)
        total_reserved += spend["reserved_cost"]
        remaining = SPEND_CEILING - SAFETY_MARGIN - total_reserved
        if remaining <= 0:
            raise RuntimeError("development capture exhausted the safe task-wide spend reserve")

    case_path = None if args.plan else args.cases
    if case_path is not None:
        _jsonl_dump(
            case_path, [case.model_dump(mode="json") for case in sorted(all_cases, key=lambda item: item.case_id)]
        )
    _jsonl_dump(args.receipts, all_receipts)
    _write_dev_ledger(args.ledger, metadata, records, candidate_runs, case_path)
    print(json.dumps({"ledger": str(args.ledger), "cases": len(all_cases), "reserved_cost": total_reserved}, indent=2))
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    try:
        ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read development ledger: {exc}") from exc
    candidate = args.candidate
    if candidate not in CANDIDATE_BUNDLES:
        raise ValueError(f"unknown candidate {candidate!r}")
    if ledger.get("candidate_frozen"):
        raise ValueError("development ledger is already frozen")
    if not ledger.get("candidate_runs", {}).get(candidate):
        raise ValueError(f"candidate {candidate!r} has no development run")
    spent = float(ledger.get("total_reserved_cost", 0.0))
    if spent >= SPEND_CEILING - SAFETY_MARGIN:
        raise ValueError("no safe budget remains for the heldout call")
    if not args.selection:
        raise ValueError("--selection is required to freeze the reporting-only policy")
    try:
        selection = json.loads(args.selection.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read development selection: {exc}") from exc
    if selection.get("question_candidate") != candidate or selection.get("report_candidate") != REPORT_CANDIDATE:
        raise ValueError("selection must freeze the baseline question and conservative reporting candidate")
    policies = selection.get("report_policies")
    policy_hash = selection.get("report_policy_sha256")
    if not isinstance(policies, dict) or not isinstance(policy_hash, str):
        raise TypeError("selection does not contain a complete reporting policy")
    if policy_hash != f"sha256:{_json_hash(policies)}":
        raise ValueError("selection reporting-policy hash does not match its policy")
    freeze = {
        "version": 1,
        "frozen_at": _now(),
        "candidate": candidate,
        "report_candidate": REPORT_CANDIDATE,
        "report_policies": policies,
        "report_policy_sha256": policy_hash,
        "selection_sha256": _hash_file(args.selection),
        "manifest_sha256": ledger["manifest"]["manifest_sha256"],
        "dev_ledger_sha256": _hash_file(args.ledger),
        "dev_reserved_cost": spent,
        "heldout_reserved_cost_limit": SPEND_CEILING - SAFETY_MARGIN - spent,
        "candidate_order": list(CANDIDATE_BUNDLES),
        "reason": "explicit development decision; heldout labels were not consulted",
    }
    _json_dump(args.output, freeze)
    ledger["candidate_frozen"] = True
    ledger["frozen_candidate"] = candidate
    ledger["freeze_file"] = str(args.output)
    _json_dump(args.ledger, ledger)
    print(json.dumps(freeze, indent=2))
    return 0


def cmd_heldout(args: argparse.Namespace) -> int:
    try:
        freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read freeze record: {exc}") from exc
    candidate = freeze.get("candidate")
    if candidate not in CANDIDATE_BUNDLES:
        raise ValueError("freeze record does not contain a valid candidate")
    if candidate != "baseline" or freeze.get("report_candidate") != REPORT_CANDIDATE:
        raise ValueError("heldout capture requires the frozen baseline question and reporting candidate")
    report_policies = freeze.get("report_policies")
    report_policy_hash = freeze.get("report_policy_sha256")
    if not isinstance(report_policies, dict) or report_policy_hash != f"sha256:{_json_hash(report_policies)}":
        raise ValueError("freeze record reporting policy is incomplete or has a mismatched hash")
    metadata, records = _manifest_records(args.manifest, "heldout")
    if metadata["manifest_sha256"] != freeze.get("manifest_sha256"):
        raise ValueError("heldout manifest does not match the manifest used for development")
    rules = _builtin_rules()
    endpoint = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
    plan = _plan_only(records, candidate, rules, endpoint)
    remaining = float(freeze.get("heldout_reserved_cost_limit", 0.0))
    if remaining <= 0:
        raise RuntimeError("freeze record leaves no safe budget for heldout capture")
    if args.plan:
        print(json.dumps({"candidate": candidate, "plan": plan, "heldout": len(records)}, indent=2))
        return 0
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is unavailable; heldout capture was not executed")
    cases, receipts, spend = asyncio.run(_capture_live(records, candidate, rules, endpoint, MODEL, api_key, remaining))
    _jsonl_dump(args.cases, [case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.case_id)])
    _jsonl_dump(args.receipts, receipts)
    report = {
        "version": 1,
        "captured_at": _now(),
        "candidate": candidate,
        "manifest_sha256": metadata["manifest_sha256"],
        "freeze_sha256": _hash_file(args.freeze),
        "spend": spend,
        "baseline_metrics": _metrics(cases, candidate, "heldout"),
        "selected_metrics": _metrics(cases, candidate, "heldout", report_policies),
        "report_candidate": REPORT_CANDIDATE,
        "report_policy_sha256": report_policy_hash,
        "cases": len(cases),
    }
    _json_dump(args.report, report)
    print(json.dumps(report, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded JEV01/JEV02 calibration experiment harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate exact source hashes, targets, and split isolation")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.set_defaults(function=cmd_validate)

    select = subparsers.add_parser(
        "select", help="evaluate reporting-only threshold candidates from captured dev cases"
    )
    select.add_argument("--cases", type=Path, required=True)
    select.add_argument("--output", type=Path, default=Path(".jevscan-calibration/dev-selection.json"))
    select.set_defaults(function=cmd_select)

    dev = subparsers.add_parser("dev", help="run or plan the four development candidate bundles")
    dev.add_argument("--manifest", type=Path, required=True)
    dev.add_argument("--cases", type=Path, default=Path(".jevscan-calibration/dev-cases.jsonl"))
    dev.add_argument("--receipts", type=Path, default=Path(".jevscan-calibration/dev-receipts.jsonl"))
    dev.add_argument("--ledger", type=Path, default=Path(".jevscan-calibration/dev-ledger.json"))
    dev.add_argument("--plan", action="store_true", help="preflight only; never contacts Jev")
    dev.set_defaults(function=cmd_dev)

    freeze = subparsers.add_parser("freeze", help="freeze one development candidate before heldout capture")
    freeze.add_argument("--ledger", type=Path, required=True)
    freeze.add_argument("--candidate", choices=("baseline",), required=True)
    freeze.add_argument("--selection", type=Path, required=True)
    freeze.add_argument("--output", type=Path, default=Path(".jevscan-calibration/freeze.json"))
    freeze.set_defaults(function=cmd_freeze)

    heldout = subparsers.add_parser("heldout", help="capture only the already frozen candidate on heldout")
    heldout.add_argument("--manifest", type=Path, required=True)
    heldout.add_argument("--freeze", type=Path, required=True)
    heldout.add_argument("--cases", type=Path, default=Path(".jevscan-calibration/heldout-cases.jsonl"))
    heldout.add_argument("--receipts", type=Path, default=Path(".jevscan-calibration/heldout-receipts.jsonl"))
    heldout.add_argument("--report", type=Path, default=Path(".jevscan-calibration/heldout-report.json"))
    heldout.add_argument("--plan", action="store_true", help="preflight only; never contacts Jev")
    heldout.set_defaults(function=cmd_heldout)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.function(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"calibration experiment: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
