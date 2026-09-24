"""Offline planning and replay metrics for the focused-question experiment.

This module is repository-only research tooling. It emits ordinary project
rules, then delegates parsing, context selection, question binding, request
budgeting, response validation, and answer assessment to jevscan. Planning and
replay are offline; the explicit ``capture`` command is the only live path.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from httpx import AsyncBaseTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from calibration.validation_candidates import (
    DEFAULT_LIMITS,
    ExtractionKind,
    ExtractionLimits,
    FallbackReason,
    ValidationPair,
    extract_validation_candidates,
    pair_binding_metadata,
)
from jevscan.core.client import JevClient, ReservationUsage
from jevscan.core.config import BudgetConfig, JevConfig, load_config
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.languages import language_for
from jevscan.core.models import FileJob, Target
from jevscan.core.planning import Planner, Request
from jevscan.core.protocol import (
    PROMPT_VERSION,
    QUESTION_POLICY,
    QUESTION_POLICY_V5,
    Check,
    ChoiceAnswer,
    JevError,
    JevResponse,
    NoulAnswer,
    PromptRegistry,
    encode,
    validate_answer,
)
from jevscan.core.rules import NoulCriteria
from jevscan.core.semantic_calibration import CalibrationCase, load_cases, replay_case

MODEL = "jev-1.13.0"
INPUT_PRICE_PER_MILLION = 0.042
CUMULATIVE_CAP = 0.10
LOCALIZATION_OVERHEAD = 0.25
CAPTURE_RETRIES = 1
DEFAULT_ENDPOINT = "https://api.typesafe.ai"
FOCUSED_MANIFEST = PROJECT_ROOT / "examples" / "calibration" / "focused" / "MANIFEST.yaml"
EVOLUTION_CANDIDATES_CONFIG = PROJECT_ROOT / "calibration" / "evolution-candidates.yaml"

_CANDIDATE_RULES: dict[str, tuple[str, ...]] = {
    "jev01-baseline": ("FOCUS_JEV01_BASELINE",),
    "jev01-focused": ("FOCUS_JEV01_FOCUSED",),
    "jev01-strict-and": (
        "EXP_JEV01_RESPONSIBILITIES",
        "EXP_JEV01_INTERLEAVING",
    ),
    "jev02-baseline": ("FOCUS_JEV02_SCORE",),
    "jev02-presence-gated": ("FOCUS_JEV02_SCORE", "FOCUS_JEV02_PRESENCE"),
    "jev03-concrete-mechanics": ("EXP_JEV03_CONCRETE_MECHANICS",),
    "jev05-corrected": ("EXP_JEV05_CORRECTED",),
    "jev08-choice": ("EXP_JEV08_CHOICE",),
    "unaccounted-partial-state-transition": ("EXP_PARTIAL_STATE_TRANSITION",),
    "jev04-baseline": ("FOCUS_JEV04_BASELINE",),
    "jev04-focused-joint": ("FOCUS_JEV04_JOINT",),
    "jev04-focused-decomposed": (
        "FOCUS_JEV04_DECOMPOSED_GUARANTEE",
        "FOCUS_JEV04_DECOMPOSED_PRESERVATION",
    ),
    "jev04-pair-joint": ("FOCUS_JEV04_PAIR_JOINT",),
    "jev04-pair-preservation": ("FOCUS_JEV04_PAIR_PRESERVATION",),
    "jev04-pair-decomposed": (
        "FOCUS_JEV04_PAIR_DECOMPOSED_GUARANTEE",
        "FOCUS_JEV04_PAIR_DECOMPOSED_PRESERVATION",
    ),
    "jev04-pair-corrected": (
        "EXP_JEV04_PAIR_GUARANTEE",
        "EXP_JEV04_PAIR_PRESERVATION",
    ),
}
_HISTORICAL_CANDIDATES = frozenset({
    "jev01-baseline",
    "jev01-focused",
    "jev02-baseline",
    "jev02-presence-gated",
    "jev04-baseline",
    "jev04-focused-joint",
    "jev04-focused-decomposed",
    "jev04-pair-joint",
    "jev04-pair-preservation",
    "jev04-pair-decomposed",
})

_RULE_BASES = {
    "FOCUS_JEV01_BASELINE": "JEV01",
    "FOCUS_JEV01_FOCUSED": "JEV01",
    "EXP_JEV01_RESPONSIBILITIES": "JEV01",
    "EXP_JEV01_INTERLEAVING": "JEV01",
    "FOCUS_JEV02_SCORE": "JEV02",
    "FOCUS_JEV02_PRESENCE": "JEV02",
    "EXP_JEV03_CONCRETE_MECHANICS": "JEV03",
    "EXP_JEV05_CORRECTED": "JEV05",
    "EXP_JEV08_CHOICE": "JEV08",
    "EXP_PARTIAL_STATE_TRANSITION": "EXP_PARTIAL_STATE_TRANSITION",
    "FOCUS_JEV04_BASELINE": "JEV04",
    "FOCUS_JEV04_JOINT": "JEV04",
    "FOCUS_JEV04_DECOMPOSED_GUARANTEE": "JEV04",
    "FOCUS_JEV04_DECOMPOSED_PRESERVATION": "JEV04",
    "FOCUS_JEV04_PAIR_JOINT": "JEV04",
    "FOCUS_JEV04_PAIR_PRESERVATION": "JEV04",
    "FOCUS_JEV04_PAIR_DECOMPOSED_GUARANTEE": "JEV04",
    "FOCUS_JEV04_PAIR_DECOMPOSED_PRESERVATION": "JEV04",
    "EXP_JEV04_PAIR_GUARANTEE": "JEV04",
    "EXP_JEV04_PAIR_PRESERVATION": "JEV04",
}
_METRIC_ALIASES = {
    "FOCUS_JEV01_BASELINE": {"FOCUS_JEV01_BASELINE", "JEV01"},
    "FOCUS_JEV02_SCORE": {"FOCUS_JEV02_SCORE", "JEV02"},
    "FOCUS_JEV04_BASELINE": {"FOCUS_JEV04_BASELINE", "JEV04"},
}

_FOCUSED_JEV01 = (
    "Does the target itself interleave two or more independently meaningful policies or state transitions "
    "in a way that materially harms understanding? Count truly interleaved responsibilities. Do not count "
    "a cohesive lifecycle coordinator, sequential delegation, cleanup, callback or await boundaries that "
    "belong to one operation, or immutable captured values by themselves."
)
_FOCUSED_JEV02_PRESENCE = (
    "Is there a concrete control-flow structure in the target that obscures an important execution transition? "
    "Answer true only for an actual traceability problem. A merely local guard, one uncomplicated nesting level, "
    "a cohesive lifecycle, or an immutable captured value is false; the separate score question measures extent."
)
_FOCUSED_JEV02_PRESENCE_CRITERIA = NoulCriteria(
    true="The target contains a concrete control-flow structure that obscures an important execution transition.",
    false="The target does not show that traceability problem; local guards, cohesive lifecycles, and immutable captures do not qualify.",
)
_FOCUSED_JEV04_JOINT = (
    "Select exactly one outcome for repeated validation in the target. Choose demonstrably_redundant only when "
    "the same invariant is visibly established and checked again without a new boundary, mutation, or concurrency "
    "risk. An await, callback, external call, mutation, or possible aliasing boundary can invalidate an invariant; "
    "immutable captured values and sequential cohesive lifecycle phases do not create a boundary. Choose "
    "insufficient_context only when the repeated check is visible but the missing guarantee determines the result."
)
_FOCUSED_JEV04_PAIR_PRESERVATION = (
    "Classify only the bound validation pair. Choose demonstrably_redundant when both checks test the same invariant "
    "for the same value or state and the supplied code cannot invalidate it between them. A callback, await, or call "
    "matters only if it can mutate, alias, or replace that checked value or state. A captured primitive immutable "
    "value that is never reassigned remains preserved across a callback. Choose justified_or_absent when there is no "
    "repeat or invalidation is possible. Choose insufficient_context only when a missing boundary fact decides."
)
_FOCUSED_JEV04_GUARANTEE = (
    "Does the target visibly establish the same validation invariant before the later validation? Answer true only "
    "when both validation operations and their equivalent predicate or value invariant are present. Answer false "
    "when no repeated validation is visible; do not infer a guarantee from names or comments."
)
_FOCUSED_JEV04_PRESERVATION = (
    "Does the target preserve the established validation invariant between the two checks? Answer true only when "
    "the supplied code shows no await, callback, external call, mutation, aliasing, or concurrency boundary that "
    "could invalidate it. Answer false when such a boundary is visible; do not multiply probabilities."
)
_FOCUSED_JEV04_GUARANTEE_CRITERIA = NoulCriteria(
    true="The target visibly establishes the same validation invariant before the later validation.",
    false="The target does not visibly establish that equivalent invariant before a later validation.",
)
_FOCUSED_JEV04_PRESERVATION_CRITERIA = NoulCriteria(
    true="The established validation invariant remains preserved between the two checks with no invalidating boundary.",
    false="A visible await, callback, external call, mutation, aliasing, or concurrency boundary can invalidate the invariant.",
)
_PAIR_TASK_PREFIX = (
    "Judge only the bound earlier/later operations below as one same-value/state relationship. "
    "Their locations refer to the complete supplied target; do not assess other checks."
)
_PAIR_CANDIDATES = frozenset({
    "jev04-pair-joint",
    "jev04-pair-preservation",
    "jev04-pair-decomposed",
    "jev04-pair-corrected",
})
_PAIR_FALLBACK_CANDIDATES = frozenset({
    "jev04-pair-joint",
    "jev04-pair-preservation",
    "jev04-pair-corrected",
})


def candidate_rule_ids(candidate: str) -> tuple[str, ...]:
    try:
        return _CANDIDATE_RULES[candidate]
    except KeyError as exc:
        raise ValueError(f"unknown focused candidate {candidate!r}") from exc


def candidate_names() -> tuple[str, ...]:
    return tuple(_CANDIDATE_RULES)


def evolution_candidate_documents() -> dict[str, dict[str, Any]]:
    """Load non-production candidate rules; corpus and split data stay external."""
    loaded = load_config(
        [EVOLUTION_CANDIDATES_CONFIG.parent],
        explicit=EVOLUTION_CANDIDATES_CONFIG,
        cwd=PROJECT_ROOT,
    ).config
    return {
        name: rule.model_dump(mode="json")
        for name, rule in loaded.rules.items()
        if name
        in {
            rule_id
            for candidate in (
                "jev01-strict-and",
                "jev03-concrete-mechanics",
                "jev05-corrected",
                "jev08-choice",
                "unaccounted-partial-state-transition",
                "jev04-pair-corrected",
            )
            for rule_id in candidate_rule_ids(candidate)
        }
    }


def _json_hash(value: Any) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def source_snapshot(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in source_root.rglob("*") if path.is_file()):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _rule_document(
    base: Any,
    *,
    title: str,
    instructions: str | None = None,
    criteria: NoulCriteria | None = None,
) -> dict[str, Any]:
    document = base.model_dump(mode="json")
    document["title"] = title
    document["ruleset"] = "project"
    if instructions is not None:
        document["question"]["instructions"] = instructions
    if criteria is not None:
        document["question"]["criteria"] = criteria.model_dump(mode="json")
    return document


def candidate_documents() -> dict[str, dict[str, Any]]:
    """Return standalone project-rule documents for every candidate child."""
    rules = load_config([PROJECT_ROOT], cwd=PROJECT_ROOT).config.rules

    def noul_rule(
        base: Any,
        report: Any,
        *,
        title: str,
        instructions: str,
        criteria: NoulCriteria | None = None,
    ) -> dict[str, Any]:
        document = _rule_document(base, title=title)
        document["question"] = report.question.model_dump(mode="json")
        document["question"]["instructions"] = instructions
        if criteria is not None:
            document["question"]["criteria"] = criteria.model_dump(mode="json")
        document["report"] = report.report.model_dump(mode="json")
        document["targeted_enrichment"] = None
        return document

    return {
        "FOCUS_JEV01_BASELINE": _rule_document(rules["JEV01"], title="focused-jev01-baseline"),
        "FOCUS_JEV01_FOCUSED": _rule_document(
            rules["JEV01"], title="focused-jev01-focused", instructions=_FOCUSED_JEV01
        ),
        "FOCUS_JEV02_SCORE": _rule_document(rules["JEV02"], title="focused-jev02-score"),
        "FOCUS_JEV02_PRESENCE": _rule_document(
            rules["JEV01"],
            title="focused-jev02-presence",
            instructions=_FOCUSED_JEV02_PRESENCE,
            criteria=_FOCUSED_JEV02_PRESENCE_CRITERIA,
        ),
        "FOCUS_JEV04_BASELINE": _rule_document(rules["JEV04"], title="focused-jev04-baseline"),
        "FOCUS_JEV04_JOINT": _rule_document(
            rules["JEV04"], title="focused-jev04-joint", instructions=_FOCUSED_JEV04_JOINT
        ),
        "FOCUS_JEV04_PAIR_JOINT": _rule_document(
            rules["JEV04"], title="focused-jev04-pair-joint", instructions=_FOCUSED_JEV04_JOINT
        ),
        "FOCUS_JEV04_PAIR_PRESERVATION": _rule_document(
            rules["JEV04"],
            title="focused-jev04-pair-preservation",
            instructions=_FOCUSED_JEV04_PAIR_PRESERVATION,
        ),
        "FOCUS_JEV04_DECOMPOSED_GUARANTEE": noul_rule(
            rules["JEV04"],
            rules["JEV01"],
            title="focused-jev04-decomposed-guarantee",
            instructions=_FOCUSED_JEV04_GUARANTEE,
            criteria=_FOCUSED_JEV04_GUARANTEE_CRITERIA,
        ),
        "FOCUS_JEV04_DECOMPOSED_PRESERVATION": noul_rule(
            rules["JEV04"],
            rules["JEV01"],
            title="focused-jev04-decomposed-preservation",
            instructions=_FOCUSED_JEV04_PRESERVATION,
            criteria=_FOCUSED_JEV04_PRESERVATION_CRITERIA,
        ),
        "FOCUS_JEV04_PAIR_DECOMPOSED_GUARANTEE": noul_rule(
            rules["JEV04"],
            rules["JEV01"],
            title="focused-jev04-pair-decomposed-guarantee",
            instructions=_FOCUSED_JEV04_GUARANTEE,
            criteria=_FOCUSED_JEV04_GUARANTEE_CRITERIA,
        ),
        "FOCUS_JEV04_PAIR_DECOMPOSED_PRESERVATION": noul_rule(
            rules["JEV04"],
            rules["JEV01"],
            title="focused-jev04-pair-decomposed-preservation",
            instructions=_FOCUSED_JEV04_PRESERVATION,
            criteria=_FOCUSED_JEV04_PRESERVATION_CRITERIA,
        ),
    }


def write_candidate_config(path: Path) -> Path:
    """Write historical focused and evolution candidate rules for experiments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    documents = {**candidate_documents(), **evolution_candidate_documents()}
    document = {
        "version": 4,
        "lint": {"select": ["ALL"]},
        "rulesets": {
            "JEV": {"enabled": False},
            "project": {"enabled": True},
            "experiment-candidates": {
                "enabled": True,
                "description": "Non-production candidate definitions; no corpus or split seal",
            },
        },
        "rules": [{"name": name, **rule} for name, rule in documents.items()],
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def load_manifest(path: Path = FOCUSED_MANIFEST) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read focused manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise TypeError("focused manifest must be a mapping")
    if document.get("source_snapshot", {}).get("digest") != source_snapshot(path.parent / document["source_root"]):
        raise ValueError("focused source snapshot does not match the manifest")
    return document


def _expanded_case_source(case: CalibrationCase, source_root: Path) -> tuple[Path, str, bytes]:
    source_hash = case.hashes.source_documents.get(case.target.path)
    if source_hash is None:
        raise ValueError(f"{case.case_id}: imported case is missing target source hash")
    source = source_root / case.target.path
    if not source.is_file():
        raise ValueError(f"{case.case_id}: imported source document does not exist: {source}")
    if source_hash != _sha256_text(source.read_text(encoding="utf-8")):
        raise ValueError(f"{case.case_id}: imported source hash does not match {source}")
    source_bytes = source.read_bytes()
    start, end = case.target.start_byte, case.target.end_byte
    if start < 0 or end < start or end > len(source_bytes):
        raise ValueError(f"{case.case_id}: imported target span is outside source")
    return source, source_hash, source_bytes[start:end]


def _expanded_case_entry(case: CalibrationCase, source_root: Path, source_commit: str) -> dict[str, Any] | None:
    if case.rule_id not in {"JEV01", "JEV02", "JEV04"}:
        return None
    if case.requested_model != MODEL or case.returned_model != MODEL:
        raise ValueError(f"{case.case_id}: imported case model is not pinned to {MODEL}")
    supported_prompts = {(5, QUESTION_POLICY_V5), (PROMPT_VERSION, QUESTION_POLICY)}
    if (case.prompt.version, case.prompt.policy) not in supported_prompts:
        raise ValueError(f"{case.case_id}: imported case prompt policy is not supported experiment source material")
    source, source_hash, target_bytes = _expanded_case_source(case, source_root)
    expected_score = None
    if case.rule_id == "JEV02":
        raw_score = case.provenance.get("expected_answer")
        if type(raw_score) not in {int, float} or isinstance(raw_score, bool):
            raise ValueError(f"{case.case_id}: imported JEV02 expected score is missing")
        expected_score = raw_score
    label = {"Agree": "positive", "Disagree": "negative", "Partial": "Partial"}[case.label]
    return {
        "id": case.case_id,
        "source": source.as_posix(),
        "source_group": case.provenance.get("source_group", case.case_id),
        "source_partition_in_expanded": case.split,
        "development_only": True,
        "counts_as_fresh_heldout": False,
        "rule": case.rule_id,
        "label": label,
        "target": {
            "scope": case.target.scope,
            "name": case.target.qualified_name,
            "kind": case.target.kind.value if case.target.kind is not None else None,
        },
        "source_commit": source_commit,
        "source_sha256": source_hash,
        "target_sha256": f"sha256:{hashlib.sha256(target_bytes).hexdigest()}",
        "expected_score": expected_score,
        "validation_pair": case.provenance.get("validation_pair"),
        "note": (
            f"Imported from prompt v{case.prompt.version} as development-only source and label material; "
            "stored provider answer is never reused for focused candidates."
        ),
    }


def _expanded_development_entries(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    spec = manifest.get("development_import")
    if not isinstance(spec, Mapping):
        return []
    if spec.get("treat_all_as_development") is not True:
        raise ValueError("focused development_import must explicitly treat imported cases as development")
    source_path = Path(str(spec.get("source", ""))).expanduser()
    source_path = (source_path if source_path.is_absolute() else PROJECT_ROOT / source_path).resolve()
    source_root = Path(str(spec.get("source_root", ""))).expanduser()
    source_root = (source_root if source_root.is_absolute() else PROJECT_ROOT / source_root).resolve()
    source_commit = spec.get("source_commit")
    if not isinstance(source_commit, str) or not source_commit:
        raise ValueError("focused development_import.source_commit is required")
    try:
        imported = load_cases(source_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot validate focused development import {source_path}: {exc}") from exc
    return [entry for case in imported if (entry := _expanded_case_entry(case, source_root, source_commit)) is not None]


def _entries(manifest: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    key = "heldout" if phase == "heldout" else "development_references"
    entries = manifest.get(key)
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"focused manifest has no {key}")
    result = [entry for entry in entries if isinstance(entry, dict)]
    if phase == "development":
        existing = {str(entry.get("id")) for entry in result}
        result.extend(entry for entry in _expanded_development_entries(manifest) if entry["id"] not in existing)
    return result


def _entry_source(manifest_path: Path, entry: Mapping[str, Any], phase: str) -> Path:
    if phase == "heldout":
        return (manifest_path.parent / str(entry["source"])).resolve()
    declared = Path(str(entry["source"])).expanduser()
    source = (declared if declared.is_absolute() else PROJECT_ROOT / declared).resolve()
    if not source.is_file():
        raise ValueError(f"development reference source does not exist: {source}")
    return source


def _source_identity(entry: Mapping[str, Any], source: Path) -> str:
    declared = Path(str(entry["source"])).expanduser()
    if declared.is_absolute():
        return source.as_posix()
    try:
        return source.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return source.as_posix()


def _focused_config(source: Path, config_path: Path) -> Any:
    config = load_config([source.parent], explicit=config_path, cwd=source.parent).config
    return config.model_copy(update={"jev": config.jev.model_copy(update={"model": MODEL})})


def _target_matches(target: Target, expected: Mapping[str, Any]) -> bool:
    return (
        target.scope == expected["scope"]
        and target.qualified_name == expected["name"]
        and (expected.get("kind") is None or target.kind is not None and target.kind.value == expected["kind"])
    )


def _parse_source(path: Path) -> tuple[Any, Any]:
    spec = language_for(path)
    if spec is None:
        raise ValueError(f"unsupported focused fixture language: {path}")
    from jevscan.core.parser import parse_source

    job = FileJob(str(path), path.name, spec.grammar, spec.language)
    parsed = parse_source(path.read_bytes(), job)
    if parsed.failed:
        raise ValueError(f"focused source failed to parse: {path}: {parsed.diagnostics}")
    return parsed, spec


def _candidate_rules_for(base_rule: str) -> Iterable[tuple[str, str]]:
    for candidate, rule_ids in _CANDIDATE_RULES.items():
        bases = {_RULE_BASES[rule_id] for rule_id in rule_ids}
        if base_rule in {candidate, *bases, *rule_ids}:
            yield from ((candidate, rule_id) for rule_id in rule_ids)


def _additional_source_records(entry: Mapping[str, Any], primary_source: Path) -> list[dict[str, Any]]:
    documents = entry.get("additional_sources", [])
    if documents is None:
        return []
    if not isinstance(documents, list):
        raise TypeError(f"{entry['id']}: additional_sources must be a list")
    result = []
    for document in documents:
        if not isinstance(document, Mapping) or not isinstance(document.get("source"), str):
            raise TypeError(f"{entry['id']}: additional source needs a source path")
        record = {
            "source": document["source"],
            "role": document.get("role", "context"),
            "source_sha256": document.get("source_sha256"),
        }
        declared = Path(document["source"]).expanduser()
        local = (declared if declared.is_absolute() else PROJECT_ROOT / declared).resolve()
        if local != primary_source.resolve():
            raise ValueError(
                f"{entry['id']}: additional source {document['source']!r} is not part of the "
                "production evidence; add validated evidence or remove it"
            )
        if not local.is_file():
            raise ValueError(f"{entry['id']}: additional source does not exist: {local}")
        actual_hash = hashlib.sha256(local.read_bytes()).hexdigest()
        if record["source_sha256"] is None:
            record["source_sha256"] = actual_hash
        elif record["source_sha256"] not in {actual_hash, f"sha256:{actual_hash}"}:
            raise ValueError(f"{entry['id']}: additional source hash does not match {local}")
        result.append(record)
    return result


@dataclass(frozen=True, slots=True)
class _PairBinding:
    outcome: Any
    pair: ValidationPair | None
    reason: str | None
    detail: str

    @property
    def eligible(self) -> bool:
        return self.pair is not None and self.reason is None

    def metadata(self, candidate: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "candidate": candidate,
            "status": "bound" if self.eligible else "whole_target_fallback",
            "target_id": self.outcome.target_id,
            "source_path": self.outcome.source_path,
            "extractor_kind": self.outcome.kind.value,
            "candidate_count": len(self.outcome.pairs),
            "reason": self.reason,
            "detail": self.detail,
        }
        if self.outcome.fallback_reason is not None:
            result["extractor_fallback_reason"] = self.outcome.fallback_reason.value
        if self.pair is not None:
            result["pair"] = pair_binding_metadata(self.pair)
            result["pair_id"] = self.pair.id
        return result


def _pair_binding(
    parsed: Any,
    target: Target,
    evidence: Evidence | None = None,
    *,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> _PairBinding:
    if target.scope != "unit":
        return _PairBinding(
            outcome=extract_validation_candidates(parsed, target, limits=limits),
            pair=None,
            reason=FallbackReason.INVALID_TARGET.value,
            detail="pair extraction requires the original unit target",
        )
    outcome = extract_validation_candidates(parsed, target, limits=limits)
    if outcome.kind == ExtractionKind.CANDIDATES:
        if len(outcome.pairs) == 1:
            pair = outcome.pairs[0]
            metadata = pair_binding_metadata(pair)
            spans = [
                metadata["earlier"]["operation_span"],
                metadata["later"]["operation_span"],
                metadata["intervening_span"],
            ]
            if evidence is not None and not all(
                evidence.contains(target.path, span["start_byte"], span["end_byte"]) for span in spans
            ):
                return _PairBinding(
                    outcome,
                    None,
                    "evidence_incomplete",
                    "the exact pair locations are not fully covered by the unchanged requested evidence",
                )
            return _PairBinding(outcome, pair, None, "exactly one bounded validation pair")
        if not outcome.pairs:
            return _PairBinding(
                outcome,
                None,
                FallbackReason.NO_EXACT_PREDICATE_GROUP.value,
                "zero bounded validation pairs were extracted",
            )
        return _PairBinding(
            outcome,
            None,
            FallbackReason.MULTIPLE_PAIRS.value,
            f"{len(outcome.pairs)} bounded validation pairs were extracted; exactly one is required",
        )
    if outcome.kind == ExtractionKind.NOT_APPLICABLE:
        return _PairBinding(outcome, None, "not_applicable", "the target has no admitted validation operation")
    reason = outcome.fallback_reason.value if outcome.fallback_reason is not None else "extraction_fallback"
    return _PairBinding(
        outcome, None, reason, outcome.detail or "bounded pair extraction requested whole-target fallback"
    )


def _pair_bound_check(check: Check, candidate: str, pair: ValidationPair) -> Check:
    binding = {
        "candidate": candidate,
        **pair_binding_metadata(pair),
    }
    task = (
        f"{check.rule.question.instructions}\n\n{_PAIR_TASK_PREFIX}\n"
        "Pair binding metadata (machine-readable location metadata only):\n"
        f"{json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    )
    question = check.rule.question.model_copy(update={"instructions": task})
    return replace(check, rule=check.rule.model_copy(update={"question": question}))


def _allowed_candidates(
    candidates: Iterable[str] | None,
    freeze: Mapping[str, Any] | None,
) -> set[str] | None:
    requested = None if candidates is None else set(candidates)
    unknown = requested - set(_CANDIDATE_RULES) if requested is not None else set()
    if unknown:
        raise ValueError(f"unknown focused candidates: {sorted(unknown)}")
    frozen = set(freeze["candidates"]) if freeze else None
    if frozen is not None and requested is not None:
        rejected = requested - frozen
        if rejected:
            raise ValueError(f"heldout candidates are not frozen: {sorted(rejected)}")
    if requested is not None:
        return requested
    if frozen is not None:
        return frozen
    return set(_HISTORICAL_CANDIDATES)


def _prepare_candidate(
    planner: Planner,
    parsed: Any,
    entry: Mapping[str, Any],
    source: Path,
    candidate: str,
    rule_id: str,
) -> tuple[Check, Evidence, dict[str, Any] | None, bool]:
    matches = [
        check for check in planner.checks if check.rule_id == rule_id and _target_matches(check.target, entry["target"])
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{entry['id']}: expected one planned {rule_id} target, found "
            f"{len(matches)} ({[check.target.metadata() for check in matches]})"
        )
    check = matches[0]
    _validate_declared_hashes(entry, source, check.target)
    evidence = planner.requested_evidence(check)
    if candidate not in _PAIR_CANDIDATES:
        return check, evidence, None, True
    binding = _pair_binding(parsed, check.target, evidence)
    metadata = binding.metadata(candidate)
    if not binding.eligible:
        return check, evidence, metadata, False
    assert binding.pair is not None
    return _pair_bound_check(check, candidate, binding.pair), evidence, metadata, True


def _pack_requests(planner: Planner, items: list[tuple[Check, Evidence]]) -> list[Request]:
    grouped: dict[tuple[str, str], tuple[Evidence, list[Check]]] = {}
    for check, evidence in items:
        group_key = evidence.key, check.target.scope
        checks = grouped.setdefault(group_key, (evidence, []))[1]
        if check.id not in {item.id for item in checks}:
            checks.append(check)
    requests: list[Request] = []
    for evidence, checks in sorted(grouped.values(), key=lambda item: (item[0].key, item[1][0].target.scope)):
        rubric = PromptRegistry.from_questions(check.rule.question for check in checks)
        rubric.validate_questions(check.rule.question for check in checks)
        state = rubric.state_bytes(evidence.state)
        current: list[Check] = []
        for check in sorted(checks, key=lambda item: item.id):
            proposed = (*current, check)
            proposed_wires = {item.id: encode(item.question()) for item in proposed}
            if current and not planner.budget.fits(state, proposed_wires):
                question_wires = {item.id: encode(item.question()) for item in current}
                body = planner.budget.body(state, question_wires)
                requests.append(Request(evidence, tuple(current), body, state, question_wires, rubric))
                current = []
            current.append(check)
        if current:
            question_wires = {item.id: encode(item.question()) for item in current}
            body = planner.budget.body(state, question_wires)
            requests.append(Request(evidence, tuple(current), body, state, question_wires, rubric))
    return requests


@dataclass(frozen=True, slots=True)
class _CaptureItem:
    entry: Mapping[str, Any]
    candidate: str
    check: Check
    evidence: Evidence
    source: Path
    phase: str
    context_complete: bool
    target_complete: bool
    pair_binding: dict[str, Any] | None = None

    @property
    def parent_case_id(self) -> str:
        return str(self.entry["id"])

    @property
    def opportunity_key(self) -> tuple[str, str, str]:
        return (self.parent_case_id, self.check.target.id, str(self.entry["rule"]))


def _capture_items(
    manifest_path: Path,
    *,
    phase: str,
    config_path: Path,
    freeze_path: Path | None,
    candidates: Iterable[str] | None = None,
) -> tuple[list[_CaptureItem], list[Request], dict[Path, Planner], str, dict[str, Any], list[dict[str, Any]]]:
    manifest = load_manifest(manifest_path)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    freeze = _assert_frozen(freeze_path, manifest_sha256) if phase == "heldout" else None
    allowed_candidates = _allowed_candidates(candidates, freeze)
    entries = _entries(manifest, phase)
    items: list[_CaptureItem] = []
    all_items: list[tuple[Check, Evidence]] = []
    extractor_outcomes: list[dict[str, Any]] = []
    planners: dict[Path, Planner] = {}
    parsed_files: dict[Path, Any] = {}
    selected_count = 0

    for entry in entries:
        source = _entry_source(manifest_path, entry, phase)
        parsed = parsed_files.get(source)
        if parsed is None:
            parsed, _ = _parse_source(source)
            parsed_files[source] = parsed
            planners[source] = Planner(ContextBuilder(parsed), _focused_config(source, config_path))
        planner = planners[source]
        for candidate, rule_id in _candidate_rules_for(str(entry["rule"])):
            if allowed_candidates is not None and candidate not in allowed_candidates:
                continue
            if allowed_candidates is None and not any(check.rule_id == rule_id for check in planner.checks):
                continue
            selected_count += 1
            check, evidence, pair_binding, eligible = _prepare_candidate(
                planner, parsed, entry, source, candidate, rule_id
            )
            if pair_binding is not None:
                extractor_outcomes.append({
                    "case_id": entry["id"],
                    "candidate": candidate,
                    "rule_ids": list(candidate_rule_ids(candidate)),
                    "target": check.target.metadata(),
                    "binding": pair_binding,
                })
            if not eligible:
                continue
            description = planner.context.describe(check, evidence)
            items.append(
                _CaptureItem(
                    entry,
                    candidate,
                    check,
                    evidence,
                    source,
                    phase,
                    bool(description["context_complete"]),
                    bool(description["target_complete"]),
                    pair_binding,
                )
            )
            all_items.append((check, evidence))
    if not selected_count:
        raise ValueError("freeze selects no candidates in the requested phase")
    requests = _pack_requests(next(iter(planners.values())), all_items) if all_items else []
    return items, requests, planners, manifest_sha256, manifest, extractor_outcomes


def _evidence_sources(evidence: Evidence, source: Path, expected_path: str) -> dict[str, str]:
    documents = evidence.state.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("production evidence has no documents")
    result: dict[str, str] = {}
    for document in documents:
        if not isinstance(document, Mapping):
            raise TypeError("production evidence document is not an object")
        path, content = document.get("path"), document.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise TypeError("production evidence document path/content is invalid")
        if path != expected_path:
            raise ValueError("focused capture does not support multi-file evidence")
        result[path] = source.read_bytes().decode("utf-8")
    return result


def _body_reservation(body: bytes, budget: Any) -> int:
    return math.ceil(len(body) / budget.bytes_per_token) + budget.token_reserve


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _sha256_bytes_hash(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _validate_declared_hashes(entry: Mapping[str, Any], source: Path, target: Target) -> None:
    source_bytes = source.read_bytes()
    expected = {
        "source_sha256": _sha256_bytes_hash(source_bytes),
        "target_sha256": _sha256_bytes_hash(source_bytes[target.start_byte : target.end_byte]),
    }
    for field, actual in expected.items():
        declared = entry.get(field)
        if declared is None:
            continue
        if not isinstance(declared, str) or not declared.startswith("sha256:") or len(declared) != 71:
            raise ValueError(f"{entry['id']}: {field} must use canonical sha256:<hex> format")
        if declared != actual:
            raise ValueError(f"{entry['id']}: {field} does not match the current source")


def _plan_record(
    entry: Mapping[str, Any],
    *,
    candidate: str,
    rule_id: str,
    check: Check,
    evidence: Evidence,
    source: Path,
    phase: str,
    pair_binding: dict[str, Any] | None,
    eligible: bool,
) -> dict[str, Any]:
    source_bytes = source.read_bytes()
    return {
        "case_id": entry["id"],
        "candidate": candidate,
        "rule_id": rule_id,
        "question_id": check.id if eligible else None,
        "group": entry["group"] if phase == "heldout" else entry["source_group"],
        "label": entry["label"],
        "source_commit": entry.get("source_commit"),
        "source": _source_identity(entry, source),
        "target": check.target.metadata(),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "target_sha256": hashlib.sha256(source_bytes[check.target.start_byte : check.target.end_byte]).hexdigest(),
        "expected_score": entry.get("expected_score"),
        "additional_sources": _additional_source_records(entry, source),
        "question_sha256": f"sha256:{_json_hash(check.question())}" if eligible else None,
        "evidence_sha256": f"sha256:{_json_hash(evidence.state)}",
        "validation_pair": (
            pair_binding["pair"]
            if pair_binding is not None and "pair" in pair_binding
            else None
            if pair_binding is not None
            else entry.get("validation_pair")
        ),
        "pair_binding": pair_binding,
        "fallback": pair_binding if pair_binding is not None and not eligible else None,
        "actual_provider_input_tokens": None,
    }


def _case_hashes(
    item: _CaptureItem,
    evidence_sources: Mapping[str, str],
    wire_state: dict[str, Any],
    *,
    endpoint: str,
    returned_model: str,
) -> dict[str, Any]:
    prompt = {"version": PROMPT_VERSION, "policy": QUESTION_POLICY}
    return {
        "question": f"sha256:{_json_hash(item.check.question())}",
        "evidence": f"sha256:{_json_hash(wire_state)}",
        "source_documents": {path: _sha256_text(content) for path, content in sorted(evidence_sources.items())},
        "rule": f"sha256:{_json_hash(item.check.rule.model_dump(mode='json'))}",
        "report": f"sha256:{_json_hash(item.check.rule.report.model_dump(mode='json'))}",
        "prompt": f"sha256:{_json_hash(prompt)}",
        "endpoint": _sha256_text(endpoint),
        "requested_model": _sha256_text(MODEL),
        "returned_model": _sha256_text(returned_model),
    }


def _require_pinned_model(model: str) -> None:
    if model != MODEL:
        raise ValueError(f"provider returned model {model!r}; expected pinned {MODEL!r}")


def _capture_case(
    item: _CaptureItem,
    answer: Any,
    wire_state: dict[str, Any],
    *,
    endpoint: str,
    manifest_sha256: str,
    returned_model: str,
    usage: Mapping[str, Any],
    reserved_input_tokens: int,
) -> CalibrationCase:
    validate_answer(answer, item.check.rule.question, item.check.id)
    evidence_sources = _evidence_sources(item.evidence, item.source, item.check.target.path)
    target_complete = item.target_complete
    context_complete = item.context_complete
    entry = item.entry
    label_map = {"positive": "Agree", "negative": "Disagree", "Partial": "Partial"}
    try:
        label = label_map[str(entry["label"])]
    except KeyError as exc:
        raise ValueError(f"{entry['id']}: unsupported focused label") from exc
    source_bytes = item.source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    target_hash = hashlib.sha256(source_bytes[item.check.target.start_byte : item.check.target.end_byte]).hexdigest()
    provenance = {
        "source": "focused-live-capture",
        "candidate": item.candidate,
        "parent_case_id": item.parent_case_id,
        "source_group": entry.get("group", entry.get("source_group")),
        "source_commit": entry.get("source_commit"),
        "source_path": _source_identity(entry, item.source),
        "source_sha256": source_hash,
        "target_sha256": target_hash,
        "manifest_sha256": manifest_sha256,
        "validation_pair": item.pair_binding["pair"] if item.pair_binding is not None else entry.get("validation_pair"),
        "pair_binding": item.pair_binding,
        "expected_score": entry.get("expected_score"),
        "usage": dict(usage),
        "actual_provider_input_tokens": usage.get("reported_input_tokens"),
        "reported_input_tokens": usage.get("reported_input_tokens"),
        "reported_output_tokens": usage.get("reported_output_tokens"),
        "reserved_input_tokens": reserved_input_tokens,
        "request_index": usage.get("request_index"),
        "body_sha256": usage.get("body_sha256"),
        "source_documents": sorted(evidence_sources),
    }
    hashes = _case_hashes(item, evidence_sources, wire_state, endpoint=endpoint, returned_model=returned_model)
    prompt = {"version": PROMPT_VERSION, "policy": QUESTION_POLICY}
    return CalibrationCase.model_validate({
        "version": 1,
        "case_id": f"{item.parent_case_id}::{item.candidate}::{item.check.rule_id}",
        "rule_id": item.check.rule_id,
        "rule": item.check.rule.model_dump(mode="json"),
        "target": item.check.target.metadata(),
        "answer": answer.model_dump(mode="json"),
        "context_complete": context_complete,
        "target_complete": target_complete,
        "question_wire": item.check.question(),
        "split": item.phase,
        "label": label,
        "explanation": "Focused experiment manifest label; provider answer is replayed through production assessment.",
        "provenance": provenance,
        "evidence": {"state": wire_state, "source_documents": evidence_sources},
        "prompt": prompt,
        "endpoint": endpoint,
        "requested_model": MODEL,
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


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model": MODEL,
        "input_price_per_million": INPUT_PRICE_PER_MILLION,
        "cumulative_cap": CUMULATIVE_CAP,
        "reserved_cost": 0.0,
        "reported_cost": 0.0,
        "invocations": [],
    }


def _load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_ledger()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read focused capture ledger {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("focused capture ledger schema_version 1 is required")
    for key, expected in (
        ("model", MODEL),
        ("input_price_per_million", INPUT_PRICE_PER_MILLION),
        ("cumulative_cap", CUMULATIVE_CAP),
    ):
        if value.get(key) != expected:
            raise ValueError(f"focused capture ledger {key} does not match the pinned experiment")
    if not isinstance(value.get("invocations"), list):
        raise TypeError("focused capture ledger invocations must be a list")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _capture_reservations(
    requests: list[Request],
    planner: Planner,
    parent_counts: Mapping[tuple[str, str], set[tuple[str, str, str]]],
    retries: int,
) -> tuple[list[dict[str, Any]], int, int, float]:
    documents: list[dict[str, Any]] = []
    planned_tokens = 0
    reserved_tokens = 0
    for index, request in enumerate(requests):
        estimate = planner.budget.budget(request.state, request.question_wires)
        body_reserved = _body_reservation(request.body, planner.budget.limits)
        attempts_reserved = body_reserved * (retries + 1)
        margin_reserved = math.ceil(attempts_reserved * LOCALIZATION_OVERHEAD)
        total_reserved = attempts_reserved + margin_reserved
        planned_tokens += estimate.total_tokens
        reserved_tokens += total_reserved
        documents.append({
            "request_index": index,
            "question_ids": [check.id for check in request.checks],
            "question_count": len(request.checks),
            "parent_opportunity_count": len(
                set().union(*(parent_counts[(request.evidence.key, check.id)] for check in request.checks))
            ),
            "source_evidence_sha256": f"sha256:{hashlib.sha256(request.evidence.encoded).hexdigest()}",
            "evidence_sha256": f"sha256:{hashlib.sha256(request.state).hexdigest()}",
            "body_sha256": f"sha256:{hashlib.sha256(request.body).hexdigest()}",
            "planned_input_tokens": estimate.total_tokens,
            "request_body_reserved_input_tokens": body_reserved,
            "retry_count_reserved": retries,
            "attempts_reserved_input_tokens": attempts_reserved,
            "margin_reserved_input_tokens": margin_reserved,
            "reserved_input_tokens": total_reserved,
        })
    return documents, planned_tokens, reserved_tokens, reserved_tokens * INPUT_PRICE_PER_MILLION / 1_000_000


def _client_counters(client: JevClient) -> dict[str, int | float]:
    return {
        "requests": client.requests,
        "completed_requests": client.completed_requests,
        "retry_attempts": client.retry_attempts,
        "estimated_input_tokens": client.estimated_input_tokens,
        "estimated_cost": client.estimated_cost,
    }


def _cases_from_response(
    response: JevResponse,
    request: Request,
    by_question: Mapping[tuple[str, str], list[_CaptureItem]],
    reservation: Mapping[str, Any],
    *,
    endpoint: str,
    manifest_sha256: str,
) -> list[CalibrationCase]:
    _require_pinned_model(response.model)
    usage = {
        "reported_input_tokens": response.usage.input_tokens,
        "reported_output_tokens": response.usage.output_tokens,
        "planned_input_tokens": reservation["planned_input_tokens"],
        "request_body_reserved_input_tokens": reservation["request_body_reserved_input_tokens"],
        "attempts_reserved_input_tokens": reservation["attempts_reserved_input_tokens"],
        "margin_reserved_input_tokens": reservation["margin_reserved_input_tokens"],
        "reserved_input_tokens": reservation["reserved_input_tokens"],
        "request_index": reservation["request_index"],
        "body_sha256": reservation["body_sha256"],
        "parent_opportunity_count": reservation["parent_opportunity_count"],
        "purchased_question_count": reservation["question_count"],
    }
    cases: list[CalibrationCase] = []
    wire_state = json.loads(request.state)
    for check in request.checks:
        for item in by_question[(request.evidence.key, check.id)]:
            case = _capture_case(
                item,
                response.answers[check.id],
                wire_state,
                endpoint=endpoint,
                manifest_sha256=manifest_sha256,
                returned_model=response.model,
                usage=usage,
                reserved_input_tokens=reservation["reserved_input_tokens"],
            )
            replay_case(case)
            cases.append(case)
    return cases


def _capture_receipt(
    reservation: Mapping[str, Any],
    response: JevResponse | None,
    error: str | None,
    before: Mapping[str, int | float],
    after: Mapping[str, int | float],
) -> dict[str, Any]:
    reported_input = response.usage.input_tokens if response is not None else None
    reported_output = response.usage.output_tokens if response is not None else None
    return {
        **reservation,
        "returned_model": response.model if response is not None else None,
        "reported_input_tokens": reported_input,
        "reported_output_tokens": reported_output,
        "reserved_cost": reservation["reserved_input_tokens"] * INPUT_PRICE_PER_MILLION / 1_000_000,
        "reported_cost": (reported_input * INPUT_PRICE_PER_MILLION / 1_000_000 if reported_input is not None else None),
        "client_counters_before": before,
        "client_counters_after": after,
        "attempts": int(after["requests"]) - int(before["requests"]),
        "retry_attempts": int(after["retry_attempts"]) - int(before["retry_attempts"]),
        "error": error,
    }


async def _capture_live(
    *,
    items: list[_CaptureItem],
    requests: list[Request],
    planner: Planner,
    manifest_sha256: str,
    phase: str,
    endpoint: str,
    api_key: str,
    transport: AsyncBaseTransport | None,
    reservation_documents: list[dict[str, Any]],
    ledger_invocation: dict[str, Any],
    ledger: dict[str, Any],
    ledger_path: Path,
    output: Path,
    receipts_path: Path,
) -> dict[str, Any]:
    client_config = JevConfig(
        model=MODEL,
        concurrency=1,
        requests_per_minute=600,
        retries=CAPTURE_RETRIES,
    )
    input_budget = BudgetConfig(
        max_requests=max(1, len(requests) * (CAPTURE_RETRIES + 1)),
        max_input_tokens=math.floor(CUMULATIVE_CAP * 1_000_000 / INPUT_PRICE_PER_MILLION),
        max_cost=CUMULATIVE_CAP,
        input_cost_per_million=INPUT_PRICE_PER_MILLION,
    )
    by_question: dict[tuple[str, str], list[_CaptureItem]] = defaultdict(list)
    for item in items:
        by_question[(item.evidence.key, item.check.id)].append(item)
    cases: list[CalibrationCase] = []
    receipts: list[dict[str, Any]] = []
    async with JevClient(
        client_config,
        api_key,
        base_url=endpoint,
        transport=transport,
        budget=input_budget,
        bytes_per_token=planner.budget.limits.bytes_per_token,
        token_reserve=planner.budget.limits.token_reserve,
    ) as client:
        for reservation, request in zip(reservation_documents, requests, strict=True):
            before = _client_counters(client)
            response: JevResponse | None = None
            error: str | None = None
            try:
                response = await client.evaluate(request.body, request.questions, reservation=ReservationUsage())
                cases.extend(
                    _cases_from_response(
                        response,
                        request,
                        by_question,
                        reservation,
                        endpoint=endpoint,
                        manifest_sha256=manifest_sha256,
                    )
                )
            except (JevError, KeyError, TypeError, ValueError) as exc:
                error = str(exc)
            after = _client_counters(client)
            receipt = _capture_receipt(reservation, response, error, before, after)
            receipts.append(receipt)
            if error is not None:
                _write_jsonl(output, [case.model_dump(mode="json") for case in cases])
                _write_jsonl(receipts_path, receipts)
                ledger_invocation["status"] = "failed"
                ledger_invocation["receipts"] = receipts
                ledger_invocation["cases"] = len(cases)
                ledger_invocation["output"] = str(output)
                ledger_invocation["receipts_path"] = str(receipts_path)
                failure_costs = [
                    receipt["reported_cost"] for receipt in receipts if receipt["reported_cost"] is not None
                ]
                reported_failure_cost = sum(failure_costs) if failure_costs else None
                ledger["reported_cost"] = ledger.get("reported_cost", 0.0) + (reported_failure_cost or 0.0)
                ledger_invocation["reported_cost"] = reported_failure_cost
                ledger_invocation["client_counters"] = {
                    "requests": client.requests,
                    "completed_requests": client.completed_requests,
                    "retry_attempts": client.retry_attempts,
                    "estimated_input_tokens": client.estimated_input_tokens,
                    "estimated_cost": client.estimated_cost,
                }
                _write_json(ledger_path, ledger)
                raise RuntimeError(error)
    _write_jsonl(output, [case.model_dump(mode="json") for case in cases])
    _write_jsonl(receipts_path, receipts)
    reported_costs = [receipt["reported_cost"] for receipt in receipts if receipt["reported_cost"] is not None]
    reported_cost = sum(reported_costs) if reported_costs else None
    ledger_invocation.update({
        "status": "complete",
        "phase": phase,
        "cases": len(cases),
        "receipts": receipts,
        "reported_input_tokens": (
            sum(receipt["reported_input_tokens"] or 0 for receipt in receipts)
            if any(receipt["reported_input_tokens"] is not None for receipt in receipts)
            else None
        ),
        "reported_output_tokens": (
            sum(receipt["reported_output_tokens"] or 0 for receipt in receipts)
            if any(receipt["reported_output_tokens"] is not None for receipt in receipts)
            else None
        ),
        "reported_usage_available": any(receipt["reported_input_tokens"] is not None for receipt in receipts),
        "client_counters": {
            "requests": client.requests,
            "completed_requests": client.completed_requests,
            "retry_attempts": client.retry_attempts,
            "estimated_input_tokens": client.estimated_input_tokens,
            "estimated_cost": client.estimated_cost,
        },
    })
    ledger_invocation["reported_cost"] = reported_cost
    ledger["reported_cost"] = ledger.get("reported_cost", 0.0) + (reported_cost or 0.0)
    _write_json(ledger_path, ledger)
    return {
        "schema_version": 1,
        "phase": phase,
        "model": MODEL,
        "endpoint": endpoint,
        "manifest_sha256": manifest_sha256,
        "cases": len(cases),
        "receipts": len(receipts),
        "reserved_cost": ledger_invocation["reserved_cost"],
        "reported_cost": reported_cost,
        "output": str(output),
        "receipts_path": str(receipts_path),
        "selected_candidates": ledger_invocation.get("selected_candidates", []),
        "extractor_outcomes_path": ledger_invocation.get("extractor_outcomes_path"),
        "extractor_outcomes": ledger_invocation.get("extractor_outcomes", []),
        "ledger": str(ledger_path),
    }


def capture_manifest(
    manifest_path: Path = FOCUSED_MANIFEST,
    *,
    phase: str,
    config_path: Path,
    output: Path,
    ledger_path: Path,
    receipts_path: Path | None = None,
    freeze_path: Path | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    transport: AsyncBaseTransport | None = None,
    candidates: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Capture bounded pinned answers through the production request/response path."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is unavailable; capture was not executed")
    if phase not in {"development", "heldout"}:
        raise ValueError("phase must be development or heldout")
    candidate_list = list(candidates) if candidates is not None else None
    items, requests, planners, manifest_sha256, _manifest, extractor_outcomes = _capture_items(
        manifest_path,
        phase=phase,
        config_path=config_path,
        freeze_path=freeze_path,
        candidates=candidate_list,
    )
    planner = next(iter(planners.values()))
    parent_counts: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for item in items:
        parent_counts[(item.evidence.key, item.check.id)].add(item.opportunity_key)
    reservation_documents, planned_tokens, reserved_tokens, reserved_cost = _capture_reservations(
        requests,
        planner,
        parent_counts,
        CAPTURE_RETRIES,
    )
    ledger = _load_ledger(ledger_path)
    if ledger["reserved_cost"] + reserved_cost > CUMULATIVE_CAP:
        raise RuntimeError(
            f"focused capture reservation ${ledger['reserved_cost'] + reserved_cost:.6f} "
            f"exceeds cumulative cap ${CUMULATIVE_CAP:.6f} after retry/localization margin"
        )
    invocation = {
        "invocation": len(ledger["invocations"]) + 1,
        "phase": phase,
        "manifest_sha256": manifest_sha256,
        "model": MODEL,
        "planned_input_tokens": planned_tokens,
        "reserved_input_tokens": reserved_tokens,
        "reserved_cost": reserved_cost,
        "parent_opportunities": len(
            {item.opportunity_key for item in items}
            | {(str(outcome["case_id"]), str(outcome["target"]["id"]), "JEV04") for outcome in extractor_outcomes}
        ),
        "purchased_questions": sum(len(request.checks) for request in requests),
        "selected_candidates": sorted(
            set(candidate_list)
            if candidate_list is not None
            else {item.candidate for item in items} | {outcome["candidate"] for outcome in extractor_outcomes}
        ),
        "extractor_outcomes": extractor_outcomes,
        "status": "reserved",
        "requests": reservation_documents,
    }
    extractor_outcomes_path = output.with_suffix(".extractor.jsonl")
    _write_jsonl(extractor_outcomes_path, extractor_outcomes)
    invocation["extractor_outcomes_path"] = str(extractor_outcomes_path)
    ledger["reserved_cost"] += reserved_cost
    ledger["invocations"].append(invocation)
    _write_json(ledger_path, ledger)
    return asyncio.run(
        _capture_live(
            items=items,
            requests=requests,
            planner=planner,
            manifest_sha256=manifest_sha256,
            phase=phase,
            endpoint=endpoint,
            api_key=api_key,
            transport=transport,
            reservation_documents=reservation_documents,
            ledger_invocation=invocation,
            ledger=ledger,
            ledger_path=ledger_path,
            output=output,
            receipts_path=receipts_path or output.with_suffix(".receipts.jsonl"),
        )
    )


def _assert_frozen(freeze_path: Path | None, manifest_sha256: str) -> dict[str, Any]:
    if freeze_path is None:
        raise ValueError("heldout planning is refused until a development candidate is frozen")
    try:
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read freeze record {freeze_path}: {exc}") from exc
    if freeze.get("manifest_sha256") != manifest_sha256:
        raise ValueError("freeze record does not match the focused manifest")
    if freeze.get("phase") != "frozen" or not isinstance(freeze.get("candidates"), list):
        raise ValueError("freeze record is incomplete")
    return freeze


def plan_manifest(
    manifest_path: Path = FOCUSED_MANIFEST,
    *,
    phase: str,
    config_path: Path,
    freeze_path: Path | None = None,
    candidates: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Plan candidate questions with production parsing/context/budgeting only."""
    if phase not in {"development", "heldout"}:
        raise ValueError("phase must be development or heldout")
    candidate_list = list(candidates) if candidates is not None else None
    manifest = load_manifest(manifest_path)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    freeze = _assert_frozen(freeze_path, manifest_sha256) if phase == "heldout" else None
    allowed_candidates = _allowed_candidates(candidate_list, freeze)
    entries = _entries(manifest, phase)
    planned: list[dict[str, Any]] = []
    all_items: list[tuple[Check, Evidence]] = []
    planned_items: list[tuple[Check, Evidence, dict[str, Any]]] = []
    planners: dict[Path, Planner] = {}
    parsed_files: dict[Path, Any] = {}
    selected_count = 0

    for entry in entries:
        source = _entry_source(manifest_path, entry, phase)
        parsed = parsed_files.get(source)
        if parsed is None:
            parsed, _ = _parse_source(source)
            parsed_files[source] = parsed
            config = _focused_config(source, config_path)
            planners[source] = Planner(ContextBuilder(parsed), config)
        planner = planners[source]
        for candidate, rule_id in _candidate_rules_for(str(entry["rule"])):
            if allowed_candidates is not None and candidate not in allowed_candidates:
                continue
            if allowed_candidates is None and not any(check.rule_id == rule_id for check in planner.checks):
                continue
            selected_count += 1
            planned_check, evidence, pair_binding, eligible = _prepare_candidate(
                planner, parsed, entry, source, candidate, rule_id
            )
            record = _plan_record(
                entry,
                candidate=candidate,
                rule_id=rule_id,
                check=planned_check,
                evidence=evidence,
                source=source,
                phase=phase,
                pair_binding=pair_binding,
                eligible=eligible,
            )
            planned.append(record)
            if eligible:
                all_items.append((planned_check, evidence))
                planned_items.append((planned_check, evidence, record))

    if not selected_count:
        raise ValueError("freeze selects no candidates in the requested phase")
    requests = _pack_requests(next(iter(planners.values())), all_items) if all_items else []
    planned_tokens = 0
    reserved_tokens = 0
    batch_documents: list[dict[str, Any]] = []
    parent_counts: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for check, evidence, record in planned_items:
        parent_counts[(evidence.key, check.id)].add((
            str(record["case_id"]),
            check.target.id,
            _RULE_BASES[record["rule_id"]],
        ))
    for index, request in enumerate(requests):
        estimate = next(iter(planners.values())).budget.budget(request.state, request.question_wires)
        body_reserved = _body_reservation(request.body, next(iter(planners.values())).budget.limits)
        attempts_reserved = body_reserved * (CAPTURE_RETRIES + 1)
        margin_reserved = math.ceil(attempts_reserved * LOCALIZATION_OVERHEAD)
        planned_tokens += estimate.total_tokens
        reserved_tokens += attempts_reserved + margin_reserved
        batch_documents.append({
            "request_index": index,
            "source_evidence_sha256": f"sha256:{hashlib.sha256(request.evidence.encoded).hexdigest()}",
            "evidence_sha256": f"sha256:{hashlib.sha256(request.state).hexdigest()}",
            "question_ids": [check.id for check in request.checks],
            "question_count": len(request.checks),
            "parent_opportunity_count": len(
                set().union(*(parent_counts[(request.evidence.key, check.id)] for check in request.checks))
            ),
            "body_sha256": f"sha256:{hashlib.sha256(request.body).hexdigest()}",
            "planned_input_tokens": estimate.total_tokens,
            "request_body_reserved_input_tokens": body_reserved,
            "retry_count_reserved": CAPTURE_RETRIES,
            "attempts_reserved_input_tokens": attempts_reserved,
            "margin_reserved_input_tokens": margin_reserved,
            "reserved_input_tokens": attempts_reserved + margin_reserved,
            "actual_provider_input_tokens": None,
            "reported_input_tokens": None,
        })
    attempts_reserved_total = sum(
        (int(batch["attempts_reserved_input_tokens"]) for batch in batch_documents),
        0,
    )
    margin_reserved_total = sum(
        (int(batch["margin_reserved_input_tokens"]) for batch in batch_documents),
        0,
    )
    purchased_questions = sum((int(batch["question_count"]) for batch in batch_documents), 0)
    selected_parent_opportunities = {
        (str(record["case_id"]), str(record["target"]["id"]), _RULE_BASES[record["rule_id"]]) for record in planned
    }
    result = {
        "schema_version": 1,
        "phase": phase,
        "manifest_sha256": manifest_sha256,
        "model": MODEL,
        "input_price_per_million": INPUT_PRICE_PER_MILLION,
        "localization_overhead": LOCALIZATION_OVERHEAD,
        "cumulative_cap": CUMULATIVE_CAP,
        "planned_input_tokens": planned_tokens,
        "reserved_input_tokens": reserved_tokens,
        "reserved_cost": reserved_tokens * INPUT_PRICE_PER_MILLION / 1_000_000,
        "planned_cost": planned_tokens * INPUT_PRICE_PER_MILLION / 1_000_000,
        "retry_count_reserved": CAPTURE_RETRIES,
        "attempts_reserved_input_tokens": attempts_reserved_total,
        "margin_reserved_input_tokens": margin_reserved_total,
        "actual_provider_input_tokens": None,
        "reported_input_tokens": None,
        "frozen_candidates": freeze["candidates"] if freeze else None,
        "selected_candidates": sorted(
            set(candidate_list) if candidate_list is not None else {record["candidate"] for record in planned}
        ),
        "extractor_outcomes": [
            {
                "case_id": record["case_id"],
                "candidate": record["candidate"],
                "binding": record["pair_binding"],
            }
            for record in planned
            if record["pair_binding"] is not None
        ],
        "cases": planned,
        "batches": batch_documents,
        "accounting": {
            "parent_opportunities": len(selected_parent_opportunities),
            "purchased_questions": purchased_questions,
            "whole_target_fallbacks": sum(record["fallback"] is not None for record in planned),
        },
    }
    if result["reserved_cost"] > CUMULATIVE_CAP:
        raise ValueError("offline reserved plan exceeds the experiment cap after retries and localization margin")
    return result


def freeze_candidates(
    manifest_path: Path,
    development_plan: Path,
    candidates: list[str],
    output: Path,
) -> None:
    load_manifest(manifest_path)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    try:
        plan = json.loads(development_plan.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read development plan {development_plan}: {exc}") from exc
    if plan.get("phase") != "development" or plan.get("manifest_sha256") != manifest_sha256:
        raise ValueError("freeze requires a development plan for the same manifest")
    if not candidates or any(candidate not in _CANDIDATE_RULES for candidate in candidates):
        raise ValueError("freeze candidates must be named focused candidates")
    selected = plan.get("selected_candidates")
    if isinstance(selected, list) and any(candidate not in selected for candidate in candidates):
        raise ValueError("freeze candidate was not selected in the development plan")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "frozen",
                "manifest_sha256": manifest_sha256,
                "development_plan_sha256": hashlib.sha256(development_plan.read_bytes()).hexdigest(),
                "candidates": sorted(set(candidates)),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _review_signal(record: Any) -> bool:
    return record.signal


JEV01_AND_WARNING = 0.71
JEV01_AND_ERROR = 0.90
JEV04_PAIR_WARNING_GUARANTEE = 0.71
JEV04_PAIR_WARNING_PRESERVATION = 0.60
JEV04_PAIR_WARNING_CONFIDENCE = 0.45
JEV04_PAIR_ERROR_GUARANTEE = 0.90
JEV04_PAIR_ERROR_PRESERVATION = 0.92
JEV04_PAIR_ERROR_CONFIDENCE = 0.70


def _probability(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite probability")
    if not 0 <= value <= 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return float(value)


def compose_noul_and(probabilities: Iterable[float]) -> str:
    """Compose Noul child probabilities without multiplying them.

    The returned status is the status of the offline experiment signal. Child
    assessments are deliberately not consulted, so a child can never become a
    separately promoted finding.
    """
    values = tuple(_probability(value, "Noul probability") for value in probabilities)
    if not values:
        raise ValueError("Noul composition requires at least one child probability")
    if all(value >= JEV01_AND_ERROR for value in values):
        return "error"
    if all(value >= JEV01_AND_WARNING for value in values):
        return "warning"
    return "ok"


def compose_jev04_pair(
    guarantee_probability: float,
    preservation_choice: str,
    preservation_probability: float,
    preservation_confidence: float,
) -> str:
    """Compose the exact-pair guarantee and preservation answers offline."""
    guarantee = _probability(guarantee_probability, "guarantee probability")
    preservation = _probability(preservation_probability, "preservation probability")
    confidence = _probability(preservation_confidence, "preservation confidence")
    if preservation_choice == "insufficient_context":
        return "unknown"
    if preservation_choice != "preserved":
        return "ok"
    if (
        guarantee >= JEV04_PAIR_ERROR_GUARANTEE
        and preservation >= JEV04_PAIR_ERROR_PRESERVATION
        and confidence >= JEV04_PAIR_ERROR_CONFIDENCE
    ):
        return "error"
    if (
        guarantee >= JEV04_PAIR_WARNING_GUARANTEE
        and preservation >= JEV04_PAIR_WARNING_PRESERVATION
        and confidence >= JEV04_PAIR_WARNING_CONFIDENCE
    ):
        return "warning"
    return "ok"


def _bundle_identity(record: Any) -> bytes:
    case = record.case
    return encode({
        "label": case.label,
        "target": case.target.metadata(),
        "evidence": case.hashes.evidence,
        "evidence_identity": case.comparability.evidence,
        "requested_model": case.requested_model,
        "returned_model": case.returned_model,
        "prompt": case.prompt.model_dump(mode="json"),
        "prompt_hash": case.hashes.prompt,
        "prompt_identity": case.comparability.prompt.model_dump(mode="json"),
        "source_hashes": case.hashes.source_documents,
    })


def _children_by_parent(
    child_records: Mapping[str, list[Any]],
    *,
    expected_rule_ids: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    expected = set(expected_rule_ids or child_records)
    if expected != set(child_records):
        raise ValueError(
            f"composed candidate child registry mismatch: expected {sorted(expected)}, received {sorted(child_records)}"
        )
    by_case: dict[str, dict[str, Any]] = defaultdict(dict)
    for rule_id, records in child_records.items():
        for record in records:
            parent_case_id = record.case.provenance.get("parent_case_id")
            if not isinstance(parent_case_id, str) or not parent_case_id:
                raise ValueError(f"{record.case.case_id}: composed child is missing parent_case_id")
            if rule_id in by_case[parent_case_id]:
                raise ValueError(f"composed candidate has duplicate child records for {parent_case_id}/{rule_id}")
            by_case[parent_case_id][rule_id] = record
    for parent_case_id, children in by_case.items():
        received = set(children)
        if received != expected:
            missing = sorted(expected - received)
            extra = sorted(received - expected)
            raise ValueError(
                f"composed candidate has incomplete child bundle for {parent_case_id}: missing={missing}, extra={extra}"
            )
        identities = {_bundle_identity(record) for record in children.values()}
        if len(identities) != 1:
            raise ValueError(f"composed candidate child bundle metadata mismatch for {parent_case_id}")
    return dict(by_case)


def _jev01_strict_and_status(children: Mapping[str, Any]) -> str:
    answers = [record.case.answer for record in children.values()]
    if not all(isinstance(answer, NoulAnswer) for answer in answers):
        raise TypeError("JEV01 strict composition requires Noul child answers")
    return compose_noul_and(answer.noul for answer in answers if isinstance(answer, NoulAnswer))


def _jev04_pair_status(children: Mapping[str, Any]) -> str:
    guarantee = children["EXP_JEV04_PAIR_GUARANTEE"].case.answer
    preservation = children["EXP_JEV04_PAIR_PRESERVATION"].case.answer
    if not isinstance(guarantee, NoulAnswer) or not isinstance(preservation, ChoiceAnswer):
        raise TypeError("JEV04 pair composition requires Noul guarantee and Choice preservation answers")
    return compose_jev04_pair(
        guarantee.noul,
        preservation.choice,
        preservation.probabilities[preservation.choice],
        preservation.confidence,
    )


def _metric_label(record: Any) -> str:
    """Use reviewed JEV02 levels for class membership when capture preserves them."""
    expected_score = record.case.provenance.get("expected_score")
    if (
        record.case.rule_id in {"JEV02", "FOCUS_JEV02_SCORE", "FOCUS_JEV02_PRESENCE"}
        and type(expected_score) in {int, float}
        and not isinstance(expected_score, bool)
    ):
        return "Agree" if expected_score >= 2 else "Disagree"
    return record.case.label


_MISSING_CANDIDATE = object()
_HISTORICAL_CANDIDATE_ALIASES: dict[str, frozenset[str]] = {
    "jev01-baseline": frozenset({"baseline"}),
    "jev02-baseline": frozenset({"baseline"}),
    "jev04-baseline": frozenset({"baseline", "fallback"}),
}


def _historical_baseline_compatible(candidate: str, declared: object) -> bool:
    aliases = _HISTORICAL_CANDIDATE_ALIASES.get(candidate)
    return aliases is not None and (declared is _MISSING_CANDIDATE or declared in aliases)


def _candidate_matches(case: CalibrationCase, candidate: str) -> bool:
    declared = case.provenance.get("candidate", _MISSING_CANDIDATE)
    if declared == candidate:
        return True
    return _historical_baseline_compatible(candidate, declared)


def _validate_candidate_rule_membership(
    cases: Iterable[CalibrationCase],
    candidate: str,
    expected_rule_ids: tuple[str, ...],
) -> None:
    expected = set(expected_rule_ids)
    unexpected = sorted({
        case.rule_id
        for case in cases
        if case.provenance.get("candidate", _MISSING_CANDIDATE) == candidate and case.rule_id not in expected
    })
    if unexpected:
        raise ValueError(f"{candidate}: unexpected child rule records: {unexpected}")


def _metric_records(
    cases: Iterable[CalibrationCase],
    rule_ids: tuple[str, ...],
    candidate: str,
) -> list[Any]:
    accepted = set().union(*(set(_METRIC_ALIASES.get(rule_id, {rule_id})) for rule_id in rule_ids))
    return [replay_case(case) for case in cases if case.rule_id in accepted and _candidate_matches(case, candidate)]


def _record_signal(record: Any, signal_by_case: Mapping[str, bool] | None) -> bool:
    if signal_by_case is None:
        return _review_signal(record)
    key = str(record.case.provenance.get("parent_case_id", record.case.case_id))
    return signal_by_case[key]


def _metric_partition(records: list[Any]) -> tuple[list[Any], list[Any], list[Any]]:
    positives = [record for record in records if _metric_label(record) == "Agree"]
    negatives = [record for record in records if _metric_label(record) == "Disagree"]
    partial = [record for record in records if _metric_label(record) == "Partial"]
    return positives, negatives, partial


def _metric_group_values(records: list[Any]) -> list[list[Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for record in records:
        groups[str(record.case.provenance.get("source_group", record.case.case_id))].append(record)
    return list(groups.values())


def _metric_severity_and_attribution(
    records: list[Any],
    signal_by_case: Mapping[str, bool] | None,
) -> tuple[dict[str, int], dict[str, int], list[bool], dict[str, int]]:
    confirmed_severity: defaultdict[str, int] = defaultdict(int)
    tentative_severity: defaultdict[str, int] = defaultdict(int)
    attribution: list[bool] = []
    expected_scores: defaultdict[str, int] = defaultdict(int)
    for record in records:
        expected_score = record.case.provenance.get("expected_score")
        if expected_score is not None:
            expected_scores[str(expected_score)] += 1
        if _record_signal(record, signal_by_case):
            finding = record.assessment.finding or record.assessment.tentative_finding
            attribution.append(finding is not None and finding.target == record.case.target)
        if record.assessment.finding is not None:
            confirmed_severity[str(record.assessment.finding.severity)] += 1
        if record.assessment.tentative_finding is not None:
            tentative_severity[str(record.assessment.tentative_finding.severity)] += 1
    return (
        dict(confirmed_severity),
        dict(tentative_severity),
        attribution,
        dict(expected_scores),
    )


def _metrics_for_records(records: list[Any], signal_by_case: Mapping[str, bool] | None = None) -> dict[str, Any]:
    positives, negatives, partial = _metric_partition(records)
    hits = [record for record in positives if _record_signal(record, signal_by_case)]
    false_reviews = [record for record in negatives if _record_signal(record, signal_by_case)]
    groups = _metric_group_values(records)
    positive_groups = [group for group in groups if any(_metric_label(item) == "Agree" for item in group)]
    group_hits = [group for group in positive_groups if any(_record_signal(item, signal_by_case) for item in group)]
    within_group = [
        sum(_record_signal(item, signal_by_case) for item in group if _metric_label(item) == "Agree")
        / sum(_metric_label(item) == "Agree" for item in group)
        for group in positive_groups
    ]
    confirmed_severity, tentative_severity, attribution, expected_scores = _metric_severity_and_attribution(
        records,
        signal_by_case,
    )
    return {
        "cases": len(records),
        "case_recall": len(hits) / len(positives) if positives else None,
        "group_hit_coverage": len(group_hits) / len(positive_groups) if positive_groups else None,
        "averaged_within_group_recall": sum(within_group) / len(within_group) if within_group else None,
        "false_review_workload": {
            "count": len(false_reviews),
            "per_negative_case": len(false_reviews) / len(negatives) if negatives else None,
        },
        "partial": {"count": len(partial), "fraction": len(partial) / len(records) if records else None},
        "expected_scores": dict(sorted(expected_scores.items())),
        "expected_score_distribution": dict(sorted(expected_scores.items())),
        "severity": {
            "confirmed": dict(sorted(confirmed_severity.items())),
            "tentative": dict(sorted(tentative_severity.items())),
        },
        "target_attribution": {
            "exact": sum(attribution),
            "signals": len(attribution),
            "fraction": sum(attribution) / len(attribution) if attribution else None,
        },
    }


_PAIR_TASK_MARKER = "Pair binding metadata (machine-readable location metadata only):\n"


def _valid_pair_span(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"start_byte", "end_byte", "start_line", "end_line"}:
        return False
    start_byte, end_byte = value["start_byte"], value["end_byte"]
    start_line, end_line = value["start_line"], value["end_line"]
    return (
        type(start_byte) is int
        and type(end_byte) is int
        and type(start_line) is int
        and type(end_line) is int
        and 0 <= start_byte <= end_byte
        and 1 <= start_line <= end_line
    )


def _valid_pair_occurrence(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"operation_span", "callable_boundary"}:
        return False
    operation_span = value["operation_span"]
    boundary = value["callable_boundary"]
    return (
        _valid_pair_span(operation_span)
        and operation_span["start_byte"] < operation_span["end_byte"]
        and isinstance(boundary, Mapping)
        and set(boundary) == {"owner_kind", "depth"}
        and isinstance(boundary["owner_kind"], str)
        and bool(boundary["owner_kind"])
        and type(boundary["depth"]) is int
        and boundary["depth"] >= 0
    )


def _record_pair_metadata(record: Any) -> Mapping[str, Any]:
    provenance = record.case.provenance
    binding = provenance.get("pair_binding")
    if not isinstance(binding, Mapping) or not isinstance(binding.get("pair"), Mapping):
        raise TypeError(f"{record.case.case_id}: missing pair binding provenance")
    pair = binding["pair"]
    wire = record.case.question_wire
    try:
        instructions = wire["instructions"]
        if "rubric" in instructions:
            rubric_ref = instructions["rubric"]
            task = record.case.evidence.state["jevscan_prompt"]["rubrics"][rubric_ref]["instructions"]
        else:
            task = instructions["task"]
        encoded = task.split(_PAIR_TASK_MARKER, 1)[1]
        task_binding = json.loads(encoded)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{record.case.case_id}: question metadata has no pair binding") from exc
    if not isinstance(task_binding, Mapping):
        raise TypeError(f"{record.case.case_id}: question pair binding is not an object")
    if set(pair) != {
        "pair_id",
        "source_path",
        "earlier",
        "later",
        "intervening_span",
        "callable_boundary",
    }:
        raise ValueError(f"{record.case.case_id}: pair metadata does not match the canonical shape")
    boundary = pair["callable_boundary"]
    if (
        not isinstance(pair["pair_id"], str)
        or not pair["pair_id"]
        or not isinstance(pair["source_path"], str)
        or not pair["source_path"]
        or not _valid_pair_occurrence(pair["earlier"])
        or not _valid_pair_occurrence(pair["later"])
        or not _valid_pair_span(pair["intervening_span"])
        or not isinstance(boundary, Mapping)
        or set(boundary) != {"crossed"}
        or type(boundary["crossed"]) is not bool
    ):
        raise ValueError(f"{record.case.case_id}: pair metadata does not match the canonical shape")
    target = record.case.target
    earlier_span = pair["earlier"]["operation_span"]
    later_span = pair["later"]["operation_span"]
    intervening_span = pair["intervening_span"]
    if (
        pair["source_path"] != target.path
        or earlier_span["start_byte"] < target.start_byte
        or earlier_span["end_byte"] != intervening_span["start_byte"]
        or intervening_span["end_byte"] != later_span["start_byte"]
        or later_span["end_byte"] > target.end_byte
        or earlier_span["start_line"] < target.start_line
        or later_span["end_line"] > target.end_line
    ):
        raise ValueError(f"{record.case.case_id}: pair metadata is not bound to the target span")
    if task_binding.get("candidate") != binding.get("candidate"):
        raise ValueError(f"{record.case.case_id}: question and provenance candidates disagree")
    if task_binding.get("pair_id") != pair.get("pair_id"):
        raise ValueError(f"{record.case.case_id}: question and provenance pair IDs disagree")
    if encode(task_binding) != encode({"candidate": binding.get("candidate"), **pair}):
        raise ValueError(f"{record.case.case_id}: question and provenance pair metadata disagree")
    return pair


def _composed_metrics(child_records: Mapping[str, list[Any]], *, require_pair_binding: bool = False) -> dict[str, Any]:
    """Compose aligned child replay signals without changing production assessment."""
    common = _children_by_parent(child_records)
    if not common:
        return {"aligned_cases": 0, "metrics": None}
    pair_metadata: dict[str, Mapping[str, Any]] = {}
    if require_pair_binding:
        for parent_case_id, children in common.items():
            child_pairs = {rule_id: _record_pair_metadata(record) for rule_id, record in children.items()}
            if len({str(pair.get("pair_id")) for pair in child_pairs.values()}) != 1:
                raise ValueError(f"composed candidate children disagree on pair ID for {parent_case_id}")
            serialized = {encode(pair) for pair in child_pairs.values()}
            if len(serialized) != 1:
                raise ValueError(f"composed candidate children disagree on pair metadata for {parent_case_id}")
            pair_metadata[parent_case_id] = next(iter(child_pairs.values()))
    primary_rule = next(iter(child_records))
    primary = [children[primary_rule] for children in common.values()]
    signals = {
        parent_case_id: all(record.signal for record in children.values())
        for parent_case_id, children in common.items()
    }
    result: dict[str, Any] = {"aligned_cases": len(primary), "metrics": _metrics_for_records(primary, signals)}
    if require_pair_binding:
        result["pair_ids"] = {parent_case_id: metadata["pair_id"] for parent_case_id, metadata in pair_metadata.items()}
    return result


def _combined_pair_metrics(
    pair_records: list[Any],
    fallback_records: list[Any],
    pair_signal_by_case: Mapping[str, bool] | None = None,
    *,
    suppress_pair_child_assessment: bool = False,
) -> dict[str, Any]:
    """Combine eligible pair signals with deployed whole-target fallbacks."""
    eligible: dict[str, Any] = {}
    for record in pair_records:
        parent_case_id = str(record.case.provenance.get("parent_case_id", record.case.case_id))
        if parent_case_id in eligible:
            raise ValueError(f"duplicate pair-bound record for {parent_case_id}")
        _record_pair_metadata(record)
        eligible[parent_case_id] = record

    fallback: dict[str, Any] = {}
    for record in fallback_records:
        parent_case_id = str(record.case.provenance.get("parent_case_id", record.case.case_id))
        if parent_case_id in fallback:
            raise ValueError(f"duplicate baseline fallback record for {parent_case_id}")
        fallback[parent_case_id] = record

    missing = sorted(set(fallback) - set(eligible))
    combined = [eligible[parent_case_id] for parent_case_id in sorted(eligible)]
    signals = {
        parent_case_id: (pair_signal_by_case[parent_case_id] if pair_signal_by_case is not None else record.signal)
        for parent_case_id, record in eligible.items()
    }
    for parent_case_id in missing:
        baseline = fallback[parent_case_id]
        combined.append(baseline)
        signals[parent_case_id] = baseline.signal
    for parent_case_id, pair_record in eligible.items():
        baseline = fallback.get(parent_case_id)
        if baseline is not None and _bundle_identity(baseline) != _bundle_identity(pair_record):
            raise ValueError(f"pair and whole-target fallback identities disagree for {parent_case_id}")
    metrics = _metrics_for_records(combined, signals) if combined else None
    if metrics is not None and suppress_pair_child_assessment:
        fallback_only = [fallback[parent_case_id] for parent_case_id in missing]
        fallback_signals = {parent_case_id: fallback[parent_case_id].signal for parent_case_id in missing}
        fallback_metrics = _metrics_for_records(fallback_only, fallback_signals) if fallback_only else None
        metrics["severity"] = (
            fallback_metrics["severity"]
            if fallback_metrics
            else {
                "confirmed": {},
                "tentative": {},
            }
        )
        metrics["target_attribution"] = (
            fallback_metrics["target_attribution"]
            if fallback_metrics
            else {
                "exact": 0,
                "signals": 0,
                "fraction": None,
            }
        )
    return {
        "eligible_pair_cases": len(eligible),
        "whole_target_fallback_cases": len(missing),
        "fallback_records_available": bool(fallback_records),
        "missing_fallback_cases": sorted(set(eligible) - set(fallback)),
        "complete": not (set(eligible) - set(fallback)),
        "metrics": metrics,
    }


def _offline_composed_metrics(records: list[Any], signals: Mapping[str, bool]) -> dict[str, Any]:
    """Measure only the composed signal, never a child assessment."""
    result = _metrics_for_records(records, signals)
    result.pop("severity", None)
    result.pop("target_attribution", None)
    return result


def _usage_metrics(cases: Iterable[CalibrationCase]) -> dict[str, Any]:
    unique_cases: list[tuple[CalibrationCase, Mapping[str, Any]]] = []
    seen_requests: set[str] = set()
    for case in cases:
        raw_usage = case.provenance.get("usage")
        usage = raw_usage if isinstance(raw_usage, Mapping) else {}
        request_hash = usage.get("body_sha256")
        if not isinstance(request_hash, str):
            request_hash = case.provenance.get("body_sha256")
        if isinstance(request_hash, str) and request_hash in seen_requests:
            continue
        if isinstance(request_hash, str):
            seen_requests.add(request_hash)
        unique_cases.append((case, usage))

    def token_values(key: str) -> list[int]:
        values = []
        for case, usage in unique_cases:
            value = usage.get(key)
            if value is None:
                value = case.provenance.get(key)
            if value is None:
                continue
            if type(value) is not int or value < 0:
                raise ValueError(f"{key} must be a nonnegative integer when present")
            values.append(value)
        return values

    reported = token_values("reported_input_tokens")
    reported_output = token_values("reported_output_tokens")
    planned = token_values("planned_input_tokens")
    request_reserved = token_values("request_body_reserved_input_tokens")
    attempts_reserved = token_values("attempts_reserved_input_tokens")
    margin_reserved = token_values("margin_reserved_input_tokens")
    reserved = token_values("reserved_input_tokens")
    parent_opportunities = token_values("parent_opportunity_count")
    purchased_questions = token_values("purchased_question_count")
    return {
        "reported_input_tokens": sum(reported) if reported else None,
        "reported_output_tokens": sum(reported_output) if reported_output else None,
        "planned_input_tokens": sum(planned) if planned else None,
        "request_body_reserved_input_tokens": sum(request_reserved) if request_reserved else None,
        "attempts_reserved_input_tokens": sum(attempts_reserved) if attempts_reserved else None,
        "margin_reserved_input_tokens": sum(margin_reserved) if margin_reserved else None,
        "reserved_input_tokens": sum(reserved) if reserved else None,
        "reported_cost": sum(reported) * INPUT_PRICE_PER_MILLION / 1_000_000 if reported else None,
        "reserved_cost": sum(reserved) * INPUT_PRICE_PER_MILLION / 1_000_000 if reserved else None,
        "parent_opportunities": sum(parent_opportunities) if parent_opportunities else None,
        "purchased_questions": sum(purchased_questions) if purchased_questions else None,
        "actual_usage_available": bool(reported),
    }


def replay_metrics(cases_path: Path) -> dict[str, Any]:
    """Compute declared metrics through production replay, without provider calls."""
    cases = load_cases(cases_path)
    result: dict[str, Any] = {"usage": _usage_metrics(cases)}
    for candidate, rule_ids in _CANDIDATE_RULES.items():
        _validate_candidate_rule_membership(cases, candidate, rule_ids)
    for candidate, rule_ids in _CANDIDATE_RULES.items():
        if candidate in _PAIR_FALLBACK_CANDIDATES and candidate != "jev04-pair-corrected":
            pair_records = _metric_records(cases, rule_ids, candidate)
            fallback_records = _metric_records(cases, ("FOCUS_JEV04_BASELINE",), "jev04-baseline")
            result[candidate] = {
                "composition": (
                    "pair-bound focused preservation signal with deployed whole-target JEV04 fallback for ineligible cases"
                    if candidate == "jev04-pair-preservation"
                    else "pair-bound focused joint signal with deployed whole-target JEV04 fallback for ineligible cases"
                ),
                "eligible": _metrics_for_records(pair_records),
                "combined": _combined_pair_metrics(pair_records, fallback_records),
            }
            continue
        if len(rule_ids) == 1:
            result[candidate] = _metrics_for_records(_metric_records(cases, rule_ids, candidate))
            continue
        child_records = {rule_id: _metric_records(cases, (rule_id,), candidate) for rule_id in rule_ids}
        if candidate == "jev01-strict-and":
            by_case = _children_by_parent(child_records, expected_rule_ids=rule_ids)
            statuses = {
                parent_case_id: _jev01_strict_and_status(children) for parent_case_id, children in by_case.items()
            }
            primary = [children[rule_ids[0]] for children in by_case.values()]
            signals = {parent_case_id: status in {"warning", "error"} for parent_case_id, status in statuses.items()}
            result[candidate] = {
                "composition": "strict AND of independently meaningful responsibilities and visible interleaving",
                "warning_probability": [JEV01_AND_WARNING, JEV01_AND_WARNING],
                "error_probability": [JEV01_AND_ERROR, JEV01_AND_ERROR],
                "child_findings_promoted": 0,
                "composed": {
                    "statuses": statuses,
                    "metrics": _offline_composed_metrics(primary, signals) if primary else None,
                },
            }
            continue
        if candidate == "jev04-pair-corrected":
            by_case = _children_by_parent(child_records, expected_rule_ids=rule_ids)
            for children in by_case.values():
                pairs = [_record_pair_metadata(record) for record in children.values()]
                if len({str(pair["pair_id"]) for pair in pairs}) != 1 or len({encode(pair) for pair in pairs}) != 1:
                    raise ValueError("composed JEV04 children disagree on pair metadata")
            statuses = {parent_case_id: _jev04_pair_status(children) for parent_case_id, children in by_case.items()}
            primary = [children[rule_ids[0]] for children in by_case.values()]
            signals = {parent_case_id: status in {"warning", "error"} for parent_case_id, status in statuses.items()}
            fallback_records = _metric_records(
                cases,
                ("FOCUS_JEV04_BASELINE", "JEV04"),
                "jev04-baseline",
            )
            result[candidate] = {
                "composition": "strict AND of exact-pair guarantee and preserved-value/state evidence",
                "warning_gates": {
                    "guarantee_probability": JEV04_PAIR_WARNING_GUARANTEE,
                    "preservation_probability": JEV04_PAIR_WARNING_PRESERVATION,
                    "preservation_confidence": JEV04_PAIR_WARNING_CONFIDENCE,
                },
                "error_gates": {
                    "guarantee_probability": JEV04_PAIR_ERROR_GUARANTEE,
                    "preservation_probability": JEV04_PAIR_ERROR_PRESERVATION,
                    "preservation_confidence": JEV04_PAIR_ERROR_CONFIDENCE,
                },
                "child_findings_promoted": 0,
                "eligible": {
                    "statuses": statuses,
                    "metrics": _offline_composed_metrics(primary, signals) if primary else None,
                },
                "combined": _combined_pair_metrics(
                    primary,
                    fallback_records,
                    signals,
                    suppress_pair_child_assessment=True,
                ),
            }
            continue
        if candidate == "jev04-pair-decomposed":
            composed = _composed_metrics(child_records, require_pair_binding=True)
            by_child = {
                rule_id: {
                    str(record.case.provenance.get("parent_case_id", record.case.case_id)): record for record in records
                }
                for rule_id, records in child_records.items()
            }
            common_ids = set.intersection(*(set(records) for records in by_child.values())) if by_child else set()
            primary_rule = rule_ids[0]
            primary = [by_child[primary_rule][parent_case_id] for parent_case_id in sorted(common_ids)]
            pair_signals = {
                parent_case_id: all(by_child[rule_id][parent_case_id].signal for rule_id in rule_ids)
                for parent_case_id in common_ids
            }
            fallback_records = _metric_records(cases, ("FOCUS_JEV04_BASELINE",), "jev04-baseline")
            result[candidate] = {
                "composition": "conservative AND of pair-bound guarantee and preservation signals; probabilities are not multiplied",
                "diagnostic_only": True,
                "pair_binding": "one locally identified earlier/later pair with matching question metadata is required",
                "children": {rule_id: _metrics_for_records(records) for rule_id, records in child_records.items()},
                "composed": composed,
                "combined": _combined_pair_metrics(primary, fallback_records, pair_signals),
            }
            continue
        if candidate == "jev04-focused-decomposed":
            pair_bound = all(child_records.values()) and all(
                isinstance(record.case.provenance.get("pair_binding"), Mapping)
                for records in child_records.values()
                for record in records
            )
            result[candidate] = {
                "composition": "AND of guarantee-equivalence and preservation review signals; probabilities are not multiplied",
                "diagnostic_only": not pair_bound,
                "pair_binding": "one locally identified earlier/later pair with intervening locations is required",
                "children": {rule_id: _metrics_for_records(records) for rule_id, records in child_records.items()},
                "composed": _composed_metrics(child_records, require_pair_binding=True) if pair_bound else None,
            }
        else:
            result[candidate] = {
                "composition": "presence Noul is an explicit gate; existing Score remains the extent signal",
                "children": {rule_id: _metrics_for_records(records) for rule_id, records in child_records.items()},
                "composed": _composed_metrics(child_records),
            }
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="emit ordinary custom candidate rules")
    config.add_argument("--output", type=Path, required=True)
    plan = commands.add_parser("plan", help="plan source-only requests without contacting a provider")
    plan.add_argument("--manifest", type=Path, default=FOCUSED_MANIFEST)
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--phase", choices=("development", "heldout"), required=True)
    plan.add_argument("--freeze", type=Path)
    plan.add_argument("--candidate", action="append")
    plan.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("capture", help="capture pinned provider answers under the cumulative cap")
    capture.add_argument("--manifest", type=Path, default=FOCUSED_MANIFEST)
    capture.add_argument("--config", type=Path, required=True)
    capture.add_argument("--phase", choices=("development", "heldout"), required=True)
    capture.add_argument("--freeze", type=Path)
    capture.add_argument("--candidate", action="append")
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--receipts", type=Path)
    capture.add_argument("--ledger", type=Path, default=Path(".jevscan-calibration/focused-ledger.json"))
    capture.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    freeze = commands.add_parser("freeze", help="freeze development candidates before heldout planning")
    freeze.add_argument("--manifest", type=Path, default=FOCUSED_MANIFEST)
    freeze.add_argument("--development-plan", type=Path, required=True)
    freeze.add_argument("--candidate", action="append", required=True)
    freeze.add_argument("--output", type=Path, required=True)
    metrics = commands.add_parser("metrics", help="replay captured calibration cases and compute metrics")
    metrics.add_argument("--cases", type=Path, required=True)
    metrics.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "config":
        write_candidate_config(args.output)
        return 0
    if args.command == "plan":
        result = plan_manifest(
            args.manifest,
            phase=args.phase,
            config_path=args.config,
            freeze_path=args.freeze,
            candidates=args.candidate,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if args.command == "capture":
        result = capture_manifest(
            args.manifest,
            phase=args.phase,
            config_path=args.config,
            output=args.output,
            receipts_path=args.receipts,
            ledger_path=args.ledger,
            freeze_path=args.freeze,
            endpoint=args.endpoint,
            candidates=args.candidate,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "freeze":
        freeze_candidates(args.manifest, args.development_plan, args.candidate, args.output)
        return 0
    result = replay_metrics(args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
