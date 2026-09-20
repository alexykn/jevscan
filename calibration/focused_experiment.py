"""Offline planning and replay metrics for the focused-question experiment.

This module is repository-only research tooling.  It emits ordinary project
rules, then delegates parsing, context selection, question binding, request
budgeting, and answer assessment to jevscan.  It never contacts a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from jevscan.core.config import load_config
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.languages import language_for
from jevscan.core.models import FileJob, Target
from jevscan.core.planning import Planner, Request
from jevscan.core.protocol import Check, encode
from jevscan.core.semantic_calibration import CalibrationCase, load_cases, replay_case

MODEL = "jev-1.13.0"
INPUT_PRICE_PER_MILLION = 0.042
CUMULATIVE_CAP = 0.10
LOCALIZATION_OVERHEAD = 0.25
FOCUSED_MANIFEST = PROJECT_ROOT / "examples" / "calibration" / "focused" / "MANIFEST.yaml"

_CANDIDATE_RULES: dict[str, tuple[str, ...]] = {
    "jev01-baseline": ("FOCUS_JEV01_BASELINE",),
    "jev01-focused": ("FOCUS_JEV01_FOCUSED",),
    "jev02-baseline": ("FOCUS_JEV02_SCORE",),
    "jev02-presence-gated": ("FOCUS_JEV02_SCORE", "FOCUS_JEV02_PRESENCE"),
    "jev04-baseline": ("FOCUS_JEV04_BASELINE",),
    "jev04-focused-joint": ("FOCUS_JEV04_JOINT",),
    "jev04-focused-decomposed": (
        "FOCUS_JEV04_DECOMPOSED_GUARANTEE",
        "FOCUS_JEV04_DECOMPOSED_PRESERVATION",
    ),
}

_RULE_BASES = {
    "FOCUS_JEV01_BASELINE": "JEV01",
    "FOCUS_JEV01_FOCUSED": "JEV01",
    "FOCUS_JEV02_SCORE": "JEV02",
    "FOCUS_JEV02_PRESENCE": "JEV02",
    "FOCUS_JEV04_BASELINE": "JEV04",
    "FOCUS_JEV04_JOINT": "JEV04",
    "FOCUS_JEV04_DECOMPOSED_GUARANTEE": "JEV04",
    "FOCUS_JEV04_DECOMPOSED_PRESERVATION": "JEV04",
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
_FOCUSED_JEV04_JOINT = (
    "Select exactly one outcome for repeated validation in the target. Choose demonstrably_redundant only when "
    "the same invariant is visibly established and checked again without a new boundary, mutation, or concurrency "
    "risk. An await, callback, external call, mutation, or possible aliasing boundary can invalidate an invariant; "
    "immutable captured values and sequential cohesive lifecycle phases do not create a boundary. Choose "
    "insufficient_context only when the repeated check is visible but the missing guarantee determines the result."
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


def candidate_rule_ids(candidate: str) -> tuple[str, ...]:
    try:
        return _CANDIDATE_RULES[candidate]
    except KeyError as exc:
        raise ValueError(f"unknown focused candidate {candidate!r}") from exc


def candidate_names() -> tuple[str, ...]:
    return tuple(_CANDIDATE_RULES)


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


def _rule_document(base: Any, *, title: str, instructions: str | None = None) -> dict[str, Any]:
    document = base.model_dump(mode="json")
    document["title"] = title
    document["ruleset"] = "project"
    if instructions is not None:
        document["question"]["instructions"] = instructions
    return document


def candidate_documents() -> dict[str, dict[str, Any]]:
    """Return standalone project-rule documents for every candidate child."""
    rules = load_config([PROJECT_ROOT], cwd=PROJECT_ROOT).config.rules

    def noul_rule(base: Any, report: Any, *, title: str, instructions: str) -> dict[str, Any]:
        document = _rule_document(base, title=title)
        document["question"] = report.question.model_dump(mode="json")
        document["question"]["instructions"] = instructions
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
            rules["JEV01"], title="focused-jev02-presence", instructions=_FOCUSED_JEV02_PRESENCE
        ),
        "FOCUS_JEV04_BASELINE": _rule_document(rules["JEV04"], title="focused-jev04-baseline"),
        "FOCUS_JEV04_JOINT": _rule_document(
            rules["JEV04"], title="focused-jev04-joint", instructions=_FOCUSED_JEV04_JOINT
        ),
        "FOCUS_JEV04_DECOMPOSED_GUARANTEE": noul_rule(
            rules["JEV04"],
            rules["JEV01"],
            title="focused-jev04-decomposed-guarantee",
            instructions=_FOCUSED_JEV04_GUARANTEE,
        ),
        "FOCUS_JEV04_DECOMPOSED_PRESERVATION": noul_rule(
            rules["JEV04"],
            rules["JEV01"],
            title="focused-jev04-decomposed-preservation",
            instructions=_FOCUSED_JEV04_PRESERVATION,
        ),
    }


def write_candidate_config(path: Path) -> Path:
    """Write a config that enables only ordinary focused project rules."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 4,
        "rulesets": {
            "JEV": {"enabled": False},
            "project": {"enabled": True},
        },
        "rules": [{"name": name, **rule} for name, rule in candidate_documents().items()],
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


def _entries(manifest: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    key = "heldout" if phase == "heldout" else "development_references"
    entries = manifest.get(key)
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"focused manifest has no {key}")
    return [entry for entry in entries if isinstance(entry, dict)]


def _entry_source(manifest_path: Path, entry: Mapping[str, Any], phase: str) -> Path:
    if phase == "heldout":
        return (manifest_path.parent / str(entry["source"])).resolve()
    source = (PROJECT_ROOT / str(entry["source"])).resolve()
    if not source.is_file():
        raise ValueError(f"development reference source does not exist: {source}")
    return source


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
        for rule_id in rule_ids:
            if _RULE_BASES[rule_id] == base_rule:
                yield candidate, rule_id


def _additional_source_records(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
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
        local = (PROJECT_ROOT / document["source"]).resolve()
        if record["source_sha256"] is None and local.is_file():
            record["source_sha256"] = hashlib.sha256(local.read_bytes()).hexdigest()
        result.append(record)
    return result


def _pack_requests(planner: Planner, items: list[tuple[Check, Evidence]]) -> list[Request]:
    grouped: dict[str, tuple[Evidence, list[Check]]] = {}
    for check, evidence in items:
        grouped.setdefault(evidence.key, (evidence, []))[1].append(check)
    requests: list[Request] = []
    for evidence, checks in sorted(grouped.values(), key=lambda item: item[0].key):
        current: list[Check] = []
        for check in sorted(checks, key=lambda item: item.id):
            proposed = (*current, check)
            proposed_questions = {item.id: encode(item.question()) for item in proposed}
            if current and not planner.budget.fits(evidence.encoded, proposed_questions):
                questions = {item.id: encode(item.question()) for item in current}
                requests.append(Request(evidence, tuple(current), planner.budget.body(evidence.encoded, questions)))
                current = []
            current.append(check)
        if current:
            questions = {item.id: encode(item.question()) for item in current}
            requests.append(Request(evidence, tuple(current), planner.budget.body(evidence.encoded, questions)))
    return requests


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
) -> dict[str, Any]:
    """Plan candidate questions with production parsing/context/budgeting only."""
    if phase not in {"development", "heldout"}:
        raise ValueError("phase must be development or heldout")
    manifest = load_manifest(manifest_path)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    freeze = _assert_frozen(freeze_path, manifest_sha256) if phase == "heldout" else None
    entries = _entries(manifest, phase)
    planned: list[dict[str, Any]] = []
    all_items: list[tuple[Check, Evidence]] = []
    planners: dict[Path, Planner] = {}
    parsed_files: dict[Path, Any] = {}

    for entry in entries:
        source = _entry_source(manifest_path, entry, phase)
        parsed = parsed_files.get(source)
        if parsed is None:
            parsed, _ = _parse_source(source)
            parsed_files[source] = parsed
            config = load_config([source.parent], explicit=config_path, cwd=source.parent).config
            planners[source] = Planner(ContextBuilder(parsed), config)
        planner = planners[source]
        expected_target = entry["target"]
        for candidate, rule_id in _candidate_rules_for(str(entry["rule"])):
            matches = [
                check
                for check in planner.checks
                if check.rule_id == rule_id and _target_matches(check.target, expected_target)
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"{entry['id']}: expected one planned {rule_id} target, found "
                    f"{len(matches)} ({[check.target.metadata() for check in matches]})"
                )
            check = matches[0]
            evidence = planner.requested_evidence(check)
            all_items.append((check, evidence))
            source_bytes = source.read_bytes()
            planned.append({
                "case_id": entry["id"],
                "candidate": candidate,
                "rule_id": rule_id,
                "group": entry["group"] if phase == "heldout" else entry["source_group"],
                "label": entry["label"],
                "source_commit": entry.get("source_commit"),
                "source": str(source.relative_to(PROJECT_ROOT)),
                "target": check.target.metadata(),
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "target_sha256": hashlib.sha256(
                    source_bytes[check.target.start_byte : check.target.end_byte]
                ).hexdigest(),
                "expected_score": entry.get("expected_score"),
                "additional_sources": _additional_source_records(entry),
                "question_sha256": f"sha256:{_json_hash(check.question())}",
                "evidence_sha256": f"sha256:{_json_hash(evidence.state)}",
                "validation_pair": entry.get("validation_pair"),
                "actual_provider_input_tokens": None,
            })

    requests = _pack_requests(next(iter(planners.values())), all_items)
    reserved_tokens = 0
    batch_documents = []
    for index, request in enumerate(requests):
        estimate = next(iter(planners.values())).budget.budget(
            request.evidence.encoded, {check.id: encode(check.question()) for check in request.checks}
        )
        reserved_tokens += estimate.total_tokens
        batch_documents.append({
            "request_index": index,
            "evidence_sha256": f"sha256:{hashlib.sha256(request.evidence.encoded).hexdigest()}",
            "question_ids": [check.id for check in request.checks],
            "question_count": len(request.checks),
            "body_sha256": f"sha256:{hashlib.sha256(request.body).hexdigest()}",
            "reserved_input_tokens": estimate.total_tokens,
            "actual_provider_input_tokens": None,
            "reported_input_tokens": None,
        })
    result = {
        "schema_version": 1,
        "phase": phase,
        "manifest_sha256": manifest_sha256,
        "model": MODEL,
        "input_price_per_million": INPUT_PRICE_PER_MILLION,
        "localization_overhead": LOCALIZATION_OVERHEAD,
        "cumulative_cap": CUMULATIVE_CAP,
        "reserved_input_tokens": reserved_tokens,
        "reserved_cost": reserved_tokens * INPUT_PRICE_PER_MILLION / 1_000_000,
        "actual_provider_input_tokens": None,
        "reported_input_tokens": None,
        "frozen_candidates": freeze["candidates"] if freeze else None,
        "cases": planned,
        "batches": batch_documents,
    }
    if result["reserved_cost"] * (1 + LOCALIZATION_OVERHEAD) > CUMULATIVE_CAP:
        raise ValueError("offline reserved plan exceeds the experiment cap after localization overhead")
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


def _metric_records(cases: Iterable[CalibrationCase], rule_ids: tuple[str, ...]) -> list[Any]:
    accepted = set().union(*(set(_METRIC_ALIASES.get(rule_id, {rule_id})) for rule_id in rule_ids))
    return [replay_case(case) for case in cases if case.rule_id in accepted]


def _metrics_for_records(records: list[Any], signal_by_case: Mapping[str, bool] | None = None) -> dict[str, Any]:
    def signal(record: Any) -> bool:
        if signal_by_case is None:
            return _review_signal(record)
        return signal_by_case[record.case.case_id]

    positives = [record for record in records if record.case.label == "Agree"]
    negatives = [record for record in records if record.case.label == "Disagree"]
    partial = [record for record in records if record.case.label == "Partial"]
    hits = [record for record in positives if signal(record)]
    false_reviews = [record for record in negatives if signal(record)]
    groups: dict[str, list[Any]] = defaultdict(list)
    for record in records:
        groups[str(record.case.provenance.get("source_group", record.case.case_id))].append(record)
    positive_groups = [group for group in groups.values() if any(item.case.label == "Agree" for item in group)]
    group_hits = [group for group in positive_groups if any(signal(item) for item in group)]
    within_group = [
        sum(signal(item) for item in group if item.case.label == "Agree")
        / sum(item.case.label == "Agree" for item in group)
        for group in positive_groups
    ]
    attribution = []
    confirmed_severity = defaultdict(int)
    tentative_severity = defaultdict(int)
    for record in records:
        if signal(record):
            finding = record.assessment.finding or record.assessment.tentative_finding
            attribution.append(finding is None or finding.target == record.case.target)
        if record.assessment.finding is not None:
            confirmed_severity[str(record.assessment.finding.severity)] += 1
        if record.assessment.tentative_finding is not None:
            tentative_severity[str(record.assessment.tentative_finding.severity)] += 1
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


def _composed_metrics(child_records: Mapping[str, list[Any]]) -> dict[str, Any]:
    """Compose aligned child replay signals without changing production assessment."""
    by_case: dict[str, dict[str, Any]] = defaultdict(dict)
    for rule_id, records in child_records.items():
        for record in records:
            by_case[record.case.case_id][rule_id] = record
    common = {case_id: children for case_id, children in by_case.items() if len(children) == len(child_records)}
    if not common:
        return {"aligned_cases": 0, "metrics": None}
    primary_rule = next(iter(child_records))
    primary = [children[primary_rule] for children in common.values()]
    labels = {
        record.case.case_id: {child.case.label for child in children.values()}
        for record, children in zip(primary, common.values(), strict=True)
    }
    if any(len(values) != 1 for values in labels.values()):
        raise ValueError("composed candidate children disagree on a case label")
    targets = {
        record.case.case_id: {child.case.target for child in children.values()}
        for record, children in zip(primary, common.values(), strict=True)
    }
    if any(len(values) != 1 for values in targets.values()):
        raise ValueError("composed candidate children disagree on a target identity")
    signals = {case_id: all(record.signal for record in children.values()) for case_id, children in common.items()}
    return {"aligned_cases": len(primary), "metrics": _metrics_for_records(primary, signals)}


def _usage_metrics(cases: Iterable[CalibrationCase]) -> dict[str, Any]:
    cases = list(cases)

    def token_values(key: str) -> list[int]:
        values = []
        for case in cases:
            value = case.provenance.get(key)
            if value is None:
                continue
            if type(value) is not int or value < 0:
                raise ValueError(f"{key} must be a nonnegative integer when present")
            values.append(value)
        return values

    reported = token_values("actual_provider_input_tokens")
    reserved = token_values("reserved_input_tokens")
    return {
        "reported_input_tokens": sum(reported) if reported else None,
        "reserved_input_tokens": sum(reserved) if reserved else None,
        "reported_cost": sum(reported) * INPUT_PRICE_PER_MILLION / 1_000_000 if reported else None,
        "reserved_cost": sum(reserved) * INPUT_PRICE_PER_MILLION / 1_000_000 if reserved else None,
        "actual_usage_available": bool(reported),
    }


def replay_metrics(cases_path: Path) -> dict[str, Any]:
    """Compute declared metrics through production replay, without provider calls."""
    cases = load_cases(cases_path)
    result: dict[str, Any] = {"usage": _usage_metrics(cases)}
    for candidate, rule_ids in _CANDIDATE_RULES.items():
        if len(rule_ids) == 1:
            result[candidate] = _metrics_for_records(_metric_records(cases, rule_ids))
            continue
        child_records = {rule_id: _metric_records(cases, (rule_id,)) for rule_id in rule_ids}
        if candidate == "jev04-focused-decomposed":
            pair_bound = all(child_records.values()) and all(
                isinstance(record.case.provenance.get("validation_pair"), Mapping)
                for records in child_records.values()
                for record in records
            )
            result[candidate] = {
                "composition": "AND of guarantee-equivalence and preservation review signals; probabilities are not multiplied",
                "diagnostic_only": not pair_bound,
                "pair_binding": "one locally identified earlier/later pair with intervening locations is required",
                "children": {rule_id: _metrics_for_records(records) for rule_id, records in child_records.items()},
                "composed": _composed_metrics(child_records) if pair_bound else None,
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
    plan.add_argument("--output", type=Path, required=True)
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
        result = plan_manifest(args.manifest, phase=args.phase, config_path=args.config, freeze_path=args.freeze)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
