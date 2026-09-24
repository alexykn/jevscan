"""Generic all-builtins synthetic calibration capture and import.

This module is intentionally separate from the JEV01/JEV02 experiment
selector.  It prepares questions through the production ``Planner`` and
``ContextBuilder``, records provider answers without labels, and imports only
independently finalized adjudications into the normal version-1 calibration
case format.

The public synthetic manifest's generated labels are never used as ground
truth.  Scenario groups remain intact in every output so paired translations
cannot inflate independent calibration support.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, cast

import yaml
from pydantic import TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from jevscan.core.client import JevClient, ReservationUsage
from jevscan.core.config import BudgetConfig, JevConfig, load_config
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.model_limits import TokenCalibration
from jevscan.core.models import FileJob, ParsedFile, Target
from jevscan.core.parser import parse_source
from jevscan.core.planning import Planner, RequestBudget
from jevscan.core.protocol import (
    PROMPT_VERSION,
    QUESTION_POLICY,
    Answer,
    Check,
    JevResponse,
    PromptRegistry,
    encode,
    validate_answer,
)
from jevscan.core.rules import ChoiceQuestion, NoulQuestion, Rule, ScoreQuestion
from jevscan.core.semantic_calibration import CalibrationCase

MODEL = "jev-1.13.0"
INPUT_PRICE_PER_MILLION = 0.042
TOKEN_RESERVE = 128
BYTES_PER_TOKEN = 3.0
ANSWER_ADAPTER = TypeAdapter(Answer)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _source_snapshot_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for source in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = source.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + source.read_bytes())
    return digest.hexdigest()


def _json_hash(value: Any) -> str:
    return _sha256_bytes(encode(value))


def _dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _dump_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _dump_yaml(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _relative(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"{field} must be a safe relative path")
    return value.replace("\\", "/")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _rules_config(config_path: Path | None = None) -> tuple[Any, dict[str, Rule]]:
    targets = [config_path.parent] if config_path is not None else [PROJECT_ROOT]
    loaded = load_config(targets, explicit=config_path, cwd=PROJECT_ROOT)
    config = loaded.config.model_copy(
        update={
            "jev": loaded.config.jev.model_copy(update={"model": MODEL}),
            "lint": loaded.config.lint.model_copy(update={"select": ["ALL"], "ignore": []}),
        }
    )
    return config, config.rules


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    group: str
    split: str
    source_path: str
    language: str
    grammar: str
    source: bytes
    source_sha256: str
    parsed: ParsedFile
    target: Target
    rule_id: str
    rule: Rule
    applicability: dict[str, Any]
    provisional: dict[str, Any]
    context_requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedScenario:
    scenario: Scenario
    check: Check
    evidence: Evidence
    context_complete: bool
    target_complete: bool
    applicability_skip: str | None = None
    rubric: PromptRegistry | None = None


@dataclass(frozen=True, slots=True)
class RequestBatch:
    evidence: Evidence
    state: bytes
    scenarios: tuple[PreparedScenario, ...]
    questions: dict[str, Any]
    wires: dict[str, bytes]


def _language_grammar(language: str) -> tuple[str, str]:
    normalized = language.lower()
    if normalized == "javascript":
        return "javascript", "javascript"
    if normalized == "typescript":
        return "typescript", "typescript"
    if normalized in {"python", "rust", "perl"}:
        return normalized, normalized
    raise ValueError(f"unsupported synthetic language {language!r}")


def _find_target(parsed: ParsedFile, target_spec: Mapping[str, Any], source_path: str) -> Target:
    scope = target_spec.get("scope")
    name = _string(target_spec.get("name"), f"{source_path}.target.name")
    if scope == "file":
        target = Target.from_file(parsed)
        if name != Path(source_path).name:
            raise ValueError(f"{source_path}: file target name must be its filename")
        return target
    if scope != "unit":
        raise ValueError(f"{source_path}: target.scope must be unit or file")
    matches = [unit for unit in parsed.units if name in {unit.qualified_name, unit.display_name, unit.name}]
    expected_kind = target_spec.get("kind")
    if expected_kind is not None:
        matches = [unit for unit in matches if str(unit.kind) == expected_kind]
    if len(matches) != 1:
        raise ValueError(f"{source_path}: target {name!r} matched {len(matches)} units")
    return Target.from_unit(matches[0])


def _load_manifest(
    path: Path,
    config_path: Path | None = None,
    expected_snapshot_sha256: str | None = None,
) -> tuple[dict[str, Any], list[Scenario], Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from exc
    if isinstance(document, dict) and document.get("status") == "pending_source_validation_and_fresh_inference":
        scenarios = []
        for target in document.get("targets", []):
            for rule_id, expected in target.get("review_judgments", {}).items():
                scenarios.append({
                    "id": f"{target['repo']}|{target['path']}|{target['qualified_name']}|{rule_id}",
                    "scenario_group": target["owner_group"],
                    "source_root": target["root"],
                    "source": target["path"],
                    "language": "python",
                    "rule": rule_id,
                    "target": {"scope": "unit", "name": target["qualified_name"]},
                    "split": target["split"],
                    "target_sha256": target["target_sha256"],
                    "target_hash_method": "line_slice",
                    "review_start_line": target["review_start_line"],
                    "review_end_line": target["review_end_line"],
                    "applicability": {"status": "applicable", "required_facts": [], "observed_facts": []},
                    "provisional_outcome": expected,
                    "provisional_answer": expected,
                    "context_requirements": [],
                })
        document = {
            "schema_version": 1,
            "corpus": "lookup-pending",
            "provenance": document.get("note", "source-only lookup review"),
            "source_root": ".",
            "scenarios": scenarios,
        }
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("synthetic manifest schema_version 1 is required")
    declared_snapshot_sha256 = document.get("snapshot_manifest_sha256") or document.get("snapshot_sha256")
    if expected_snapshot_sha256 is not None:
        if declared_snapshot_sha256 is None:
            declared_snapshot_sha256 = expected_snapshot_sha256
        elif declared_snapshot_sha256 != expected_snapshot_sha256:
            raise ValueError("manifest snapshot hash does not match the requested reviewed snapshot")
    scenarios_data = document.get("scenarios")
    if not isinstance(scenarios_data, list) or not scenarios_data:
        raise ValueError("manifest.scenarios must be nonempty")
    config, rules = _rules_config(config_path)
    source_root_value = _string(document.get("source_root"), "source_root")
    source_root_path = Path(source_root_value).expanduser()
    if source_root_value == "project":
        # A portable manifest can name the checkout root without using an
        # unsafe ``..`` traversal from its own directory.
        source_root = source_root_value
        root = PROJECT_ROOT
    else:
        source_root = (
            source_root_value if source_root_path.is_absolute() else _relative(source_root_value, "source_root")
        )
        root = source_root_path.resolve() if source_root_path.is_absolute() else (path.parent / source_root).resolve()
    first_source = next(
        (item.get("source") for item in scenarios_data if isinstance(item, dict)),
        None,
    )
    if isinstance(first_source, str) and (first_source == source_root or first_source.startswith(source_root + "/")):
        root = path.parent.resolve()
    parsed_cache: dict[str, ParsedFile] = {}
    source_cache: dict[str, bytes] = {}
    result: list[Scenario] = []
    seen_ids: set[str] = set()
    for raw in scenarios_data:
        if not isinstance(raw, dict):
            raise TypeError("manifest scenario must be an object")
        scenario_id = _string(raw.get("id"), "scenario.id")
        if scenario_id in seen_ids:
            raise ValueError(f"duplicate scenario id {scenario_id!r}")
        seen_ids.add(scenario_id)
        source_path = _relative(raw.get("source"), f"{scenario_id}.source")
        raw_root = raw.get("source_root")
        if raw_root is None:
            scenario_root = root
        else:
            scenario_root_path = Path(_string(raw_root, f"{scenario_id}.source_root")).expanduser()
            scenario_root = (
                scenario_root_path.resolve()
                if scenario_root_path.is_absolute()
                else (path.parent / _relative(raw_root, f"{scenario_id}.source_root")).resolve()
            )
        source_file = scenario_root / source_path
        if not source_file.is_file():
            raise ValueError(f"{scenario_id}: source file does not exist: {source_file}")
        language = _string(raw.get("language"), f"{scenario_id}.language").lower()
        grammar, language = _language_grammar(language)
        source_key = str(source_file.resolve())
        if source_key not in parsed_cache:
            source = source_file.read_bytes()
            parsed_cache[source_key] = parse_source(
                source,
                FileJob(str(source_file), source_path, grammar, language),
            )
            source_cache[source_key] = source
        parsed = parsed_cache[source_key]
        if parsed.failed or parsed.diagnostics:
            details = ", ".join(f"{item.code}@{item.line}" for item in parsed.diagnostics)
            raise ValueError(f"{scenario_id}: parser failed ({details or 'unknown error'})")
        target_spec = raw.get("target", {})
        if not isinstance(target_spec, Mapping):
            raise TypeError(f"{scenario_id}.target must be an object")
        target = _find_target(parsed, target_spec, source_path)
        declared_source_sha256 = raw.get("source_sha256")
        if declared_source_sha256 is not None and declared_source_sha256 != _sha256_bytes(source_cache[source_key]):
            raise ValueError(f"{scenario_id}: source snapshot hash mismatch")
        declared_target_sha256 = raw.get("target_sha256")
        if declared_target_sha256 is not None:
            if raw.get("target_hash_method") == "line_slice":
                source_lines = source_cache[source_key].splitlines(keepends=True)
                start_line = raw.get("review_start_line", target.start_line)
                end_line = raw.get("review_end_line", target.end_line)
                if type(start_line) is not int or type(end_line) is not int:
                    raise TypeError(f"{scenario_id}: review line range must be integers")
                target_bytes = b"".join(source_lines[start_line - 1 : end_line])
            else:
                target_bytes = source_cache[source_key][target.start_byte : target.end_byte]
            actual_target_sha256 = _sha256_bytes(target_bytes)
            if declared_target_sha256 != actual_target_sha256:
                raise ValueError(f"{scenario_id}: target snapshot hash mismatch")
        rule_id = _string(raw.get("rule"), f"{scenario_id}.rule")
        if rule_id not in rules:
            raise ValueError(f"{scenario_id}: unknown packaged rule {rule_id!r}")
        applicability = raw.get("applicability", {})
        if not isinstance(applicability, dict):
            raise TypeError(f"{scenario_id}.applicability must be an object")
        provisional = {
            key: raw[key] for key in ("provisional_outcome", "provisional_answer", "expected_report") if key in raw
        }
        result.append(
            Scenario(
                scenario_id,
                _string(raw.get("scenario_group"), f"{scenario_id}.scenario_group"),
                _string(raw.get("split", raw.get("partition", "development")), f"{scenario_id}.split"),
                source_path,
                language,
                grammar,
                source_cache[source_key],
                _sha256_bytes(source_cache[source_key]),
                parsed,
                target,
                rule_id,
                rules[rule_id],
                applicability,
                provisional,
                tuple(str(item) for item in raw.get("context_requirements", [])),
            )
        )
    _validate_groups(result)
    metadata = {
        "manifest_path": str(path),
        "manifest_sha256": _sha256_bytes(path.read_bytes()),
        "source_root": source_root if source_root == "project" else str(root),
        "corpus": document.get("corpus"),
        "provenance": document.get("provenance"),
        "scenario_count": len(result),
        "rule_ids": sorted({scenario.rule_id for scenario in result}),
        "scenario_groups": sorted({scenario.group for scenario in result}),
        "config_source": str(config_path) if config_path is not None else "packaged default",
        "snapshot_manifest_sha256": declared_snapshot_sha256,
    }
    return metadata, result, config


def _validate_groups(scenarios: list[Scenario]) -> None:
    splits: dict[str, set[str]] = defaultdict(set)
    for scenario in scenarios:
        splits[scenario.group].add(scenario.split)
    leaked = {group: sorted(values) for group, values in splits.items() if len(values) > 1}
    if leaked:
        raise ValueError(f"scenario groups must not cross splits: {leaked}")


def _prepare(scenarios: list[Scenario], config: Any) -> list[PreparedScenario]:
    by_source: dict[int, Planner] = {}
    prepared: list[PreparedScenario] = []
    for scenario in scenarios:
        source_identity = id(scenario.parsed)
        planner = by_source.get(source_identity)
        if planner is None:
            planner = Planner(ContextBuilder(scenario.parsed), config)
            by_source[source_identity] = planner
        target_checks = [
            check for check in planner.checks if check.target == scenario.target and check.rule_id == scenario.rule_id
        ]
        if len(target_checks) > 1:
            raise ValueError(f"{scenario.scenario_id}: production planner produced duplicate checks")
        if not target_checks:
            reason = planner.applicability_skips.get(scenario.target.id, {}).get(scenario.rule_id)
            if reason is None:
                raise ValueError(f"{scenario.scenario_id}: target/rule was not planned")
            prepared.append(
                PreparedScenario(
                    scenario,
                    Check(scenario.scenario_id, scenario.target, scenario.rule_id, scenario.rule),
                    Evidence({"documents": [], "coverage": {}}, b"{}"),
                    False,
                    False,
                    reason,
                )
            )
            continue
        planned_check = target_checks[0]
        check = Check(scenario.scenario_id, planned_check.target, scenario.rule_id, scenario.rule)
        evidence = planner.requested_evidence(planned_check)
        description = planner.context.describe(planned_check, evidence)
        prepared.append(
            PreparedScenario(
                scenario,
                check,
                evidence,
                bool(description["context_complete"]),
                bool(description["target_complete"]),
                rubric=planner.registry_for((planned_check,)),
            )
        )
    return prepared


def _request_budget(config: Any) -> RequestBudget:
    return RequestBudget(config.evaluation, MODEL, TokenCalibration())


def _batches(prepared: list[PreparedScenario], config: Any) -> list[RequestBatch]:
    budget = _request_budget(config)
    groups: dict[tuple[str, str, bytes], tuple[Evidence, PromptRegistry, list[PreparedScenario]]] = {}
    for item in prepared:
        if item.applicability_skip is None:
            rubric = item.rubric or PromptRegistry.for_question(item.scenario.rule.question)
            key = item.evidence.key, item.check.target.scope, encode(rubric.material)
            groups.setdefault(key, (item.evidence, rubric, []))[2].append(item)
    batches: list[RequestBatch] = []
    for _, (evidence, rubric, group) in sorted(groups.items(), key=lambda item: item[0]):
        group.sort(key=lambda item: item.scenario.scenario_id)
        rubric.validate_questions(item.scenario.rule.question for item in group)
        state = rubric.state_bytes(evidence.state)
        current: list[PreparedScenario] = []
        questions: dict[str, Any] = {}
        wires: dict[str, bytes] = {}
        for item in group:
            question_id = item.scenario.scenario_id
            question = item.scenario.rule.question
            question_wire = encode(item.check.question())
            proposed_questions = {**wires, question_id: question_wire}
            violations = budget.violations(state, {question_id: question_wire})
            if violations:
                details = ", ".join(sorted(violations))
                raise ValueError(
                    f"{item.scenario.scenario_id}: single question/evidence request exceeds request budget ({details})"
                )
            if current and not budget.fits(state, proposed_questions):
                batches.append(RequestBatch(evidence, state, tuple(current), questions, wires))
                current, questions, wires = [], {}, {}
            current.append(item)
            questions[question_id] = question
            wires[question_id] = question_wire
        if current:
            batches.append(RequestBatch(evidence, state, tuple(current), questions, wires))
    return batches


def _plan(prepared: list[PreparedScenario], config: Any) -> dict[str, Any]:
    budget = _request_budget(config)
    batches = _batches(prepared, config)
    records = []
    for index, batch in enumerate(batches):
        body = budget.body(batch.state, batch.wires)
        records.append({
            "request_index": index,
            "source_evidence_sha256": batch.evidence.key,
            "evidence_sha256": _sha256_bytes(batch.state),
            "question_ids": sorted(batch.questions),
            "question_count": len(batch.questions),
            "body_sha256": _sha256_bytes(body),
            "estimated_reserved_input_tokens": math.ceil(len(body) / BYTES_PER_TOKEN) + TOKEN_RESERVE,
        })
    by_rule = defaultdict(list)
    for item in prepared:
        by_rule[item.scenario.rule_id].append(item)
    by_group = defaultdict(set)
    for item in prepared:
        if item.applicability_skip is None:
            by_group[item.scenario.rule_id].add(item.scenario.group)
    return {
        "model": MODEL,
        "requests": len(records),
        "estimated_reserved_input_tokens": sum(cast(int, item["estimated_reserved_input_tokens"]) for item in records),
        "estimated_reserved_cost": sum(cast(int, item["estimated_reserved_input_tokens"]) for item in records)
        * INPUT_PRICE_PER_MILLION
        / 1_000_000,
        "applicability_skips": [
            {
                "scenario_id": item.scenario.scenario_id,
                "rule_id": item.scenario.rule_id,
                "reason": item.applicability_skip,
            }
            for item in prepared
            if item.applicability_skip is not None
        ],
        "rules": {
            rule_id: {
                "scenarios": len(items),
                "groups": len(by_group[rule_id]),
                "leave_one_group_out_supported": len(by_group[rule_id]) >= 2,
            }
            for rule_id, items in sorted(by_rule.items())
        },
        "batches": records,
    }


def _hashes(
    check: Check,
    wire_state: dict[str, Any],
    source_sha256: str,
    rule: Rule,
    endpoint: str,
    requested_model: str,
    returned_model: str,
) -> dict[str, Any]:
    prompt = {"version": PROMPT_VERSION, "policy": QUESTION_POLICY}
    return {
        "question": f"sha256:{_json_hash(check.question())}",
        "evidence": f"sha256:{_json_hash(wire_state)}",
        "source_documents": {check.target.path: f"sha256:{source_sha256}"},
        "rule": f"sha256:{_json_hash(rule.model_dump(mode='json'))}",
        "report": f"sha256:{_json_hash(rule.report.model_dump(mode='json'))}",
        "prompt": f"sha256:{_json_hash(prompt)}",
        "endpoint": f"sha256:{_sha256_text(endpoint)}",
        "requested_model": f"sha256:{_sha256_text(requested_model)}",
        "returned_model": f"sha256:{_sha256_text(returned_model)}",
    }


def _answer_label(rule: Rule, expected: Any) -> tuple[str, bool | None]:
    if isinstance(rule.question, NoulQuestion):
        if expected == "ambiguous":
            return "Partial", None
        if type(expected) is not bool:
            raise ValueError(f"{rule.title}: expected noul adjudication must be boolean or ambiguous")
        return ("Agree" if expected else "Disagree"), expected
    if isinstance(rule.question, ScoreQuestion):
        if expected == "ambiguous":
            return "Partial", None
        if type(expected) is not int or not 0 <= expected <= len(rule.question.criteria) - 1:
            raise ValueError(f"{rule.title}: expected score adjudication is outside the rubric")
        return ("Agree" if expected >= 2 else "Disagree"), expected >= 2
    assert isinstance(rule.question, ChoiceQuestion)
    if expected == "insufficient_context":
        return "Partial", None
    if expected in (rule.report.choices or []):
        return "Agree", True
    if expected in rule.question.criteria:
        return "Disagree", False
    raise ValueError(f"{rule.title}: adjudicated choice {expected!r} is not in the rubric")


def _adjudication_index(path: Path) -> dict[Any, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read adjudications {path}: {exc}") from exc
    cases = document if isinstance(document, list) else document.get("cases")
    if not isinstance(cases, list):
        raise TypeError("adjudications.cases must be a list")
    result: dict[Any, dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict):
            raise TypeError("adjudication case must be an object")
        stable_id = case.get("scenario_id") or case.get("case_id")
        if stable_id is not None:
            key: Any = _string(stable_id, "adjudication.scenario_id")
        else:
            key = (
                _string(case.get("path"), "adjudication.path"),
                _string(case.get("symbol") or case.get("path"), "adjudication.symbol"),
                _string(case.get("rule_id"), "adjudication.rule_id"),
            )
        if key in result:
            raise ValueError(f"duplicate adjudication {key}")
        result[key] = case
    return result


def _make_case(
    item: PreparedScenario,
    answer: Answer,
    adjudication: Mapping[str, Any],
    endpoint: str,
    returned_model: str,
    manifest_sha256: str,
    adjudications_sha256: str,
) -> CalibrationCase:
    try:
        validate_answer(answer, item.scenario.rule.question, item.scenario.scenario_id)
    except Exception as exc:
        raise ValueError(f"{item.scenario.scenario_id}: provider answer is invalid: {exc}") from exc
    expected = adjudication.get("expected_answer")
    label, warning_worthy = _answer_label(item.scenario.rule, expected)
    check = item.check
    rubric = item.rubric or PromptRegistry.for_question(item.scenario.rule.question)
    rubric.validate_questions((check.rule.question,))
    wire_state = rubric.bind_state(item.evidence.state)
    hashes = _hashes(
        check,
        wire_state,
        item.scenario.source_sha256,
        item.scenario.rule,
        endpoint,
        MODEL,
        returned_model,
    )
    prompt = {"version": PROMPT_VERSION, "policy": QUESTION_POLICY}
    provenance = {
        "source": "blind-synthetic-adjudication",
        "scenario_group": item.scenario.group,
        "source_group": adjudication.get("source_group", item.scenario.group),
        "manifest_sha256": manifest_sha256,
        "adjudications_sha256": adjudications_sha256,
        "source_sha256": item.scenario.source_sha256,
        "expected_answer": expected,
        "warning_worthy": warning_worthy,
        "reason": adjudication.get("reason", ""),
        "applicability": item.scenario.applicability,
        "provisional": item.scenario.provisional,
        "adjudication": {
            key: adjudication[key]
            for key in ("expected_answer", "reason", "source_group", "scenario_group")
            if key in adjudication
        },
    }
    return CalibrationCase.model_validate({
        "version": 1,
        "case_id": item.scenario.scenario_id,
        "rule_id": item.scenario.rule_id,
        "rule": item.scenario.rule.model_dump(mode="json"),
        "target": item.scenario.target.metadata(),
        "answer": answer.model_dump(mode="json"),
        "context_complete": item.context_complete,
        "target_complete": item.target_complete,
        "split": item.scenario.split,
        "label": label,
        "explanation": _string(adjudication.get("reason"), f"{item.scenario.scenario_id}.reason"),
        "provenance": provenance,
        "evidence": {
            "state": wire_state,
            "source_documents": {item.scenario.source_path: item.scenario.source.decode("utf-8")},
        },
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


def _group_support(items: list[PreparedScenario]) -> dict[str, Any]:
    by_rule: dict[str, set[str]] = defaultdict(set)
    by_rule_count: dict[str, int] = defaultdict(int)
    for item in items:
        by_rule[item.scenario.rule_id].add(item.scenario.group)
        by_rule_count[item.scenario.rule_id] += 1
    return {
        rule_id: {
            "scenarios": by_rule_count[rule_id],
            "independent_scenario_groups": len(groups),
            "leave_one_group_out_supported": len(groups) >= 2,
        }
        for rule_id, groups in sorted(by_rule.items())
    }


def _load_answers(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        metadata = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read captured answers {path}: {exc}") from exc
    answers: dict[str, dict[str, Any]] = {}
    for line in lines:
        row = json.loads(line)
        if not isinstance(row, dict):
            raise TypeError("captured answer row must be an object")
        scenario_id = _string(row.get("scenario_id"), "answer.scenario_id")
        if scenario_id in answers:
            raise ValueError(f"duplicate captured answer {scenario_id}")
        answers[scenario_id] = row
    return metadata, answers


def _validate_captured_answers(
    prepared: list[PreparedScenario],
    answers: Mapping[str, Mapping[str, Any]],
) -> None:
    expected = {item.scenario.scenario_id: item for item in prepared if item.applicability_skip is None}
    unexpected = sorted(set(answers) - set(expected))
    if unexpected:
        raise ValueError(f"captured answers contain unknown or unexpected scenario IDs: {unexpected}")
    for scenario_id, captured in answers.items():
        item = expected[scenario_id]
        captured_rule_id = captured.get("rule_id")
        if captured_rule_id != item.scenario.rule_id:
            raise ValueError(
                f"{scenario_id}: captured rule_id {captured_rule_id!r} does not match "
                f"manifest rule_id {item.scenario.rule_id!r}"
            )
        captured_evidence = captured.get("evidence_sha256")
        rubric = item.rubric or PromptRegistry.for_question(item.scenario.rule.question)
        rubric.validate_questions((item.check.rule.question,))
        expected_evidence = _sha256_bytes(rubric.state_bytes(item.evidence.state))
        if captured_evidence != expected_evidence:
            raise ValueError(
                f"{scenario_id}: captured evidence_sha256 {captured_evidence!r} does not match "
                f"prepared request state {expected_evidence!r}"
            )


def _capture_jev_config(config: Any) -> JevConfig:
    return config.jev.model_copy(
        update={
            "model": MODEL,
            "concurrency": 1,
            "requests_per_minute": 600,
        }
    )


def _capture_budget(request_count: int, max_cost: float, retries: int = 0) -> BudgetConfig:
    return BudgetConfig(
        max_requests=request_count * (retries + 1),
        max_input_tokens=math.floor(max_cost * 1_000_000 / INPUT_PRICE_PER_MILLION),
        max_cost=max_cost,
        input_cost_per_million=INPUT_PRICE_PER_MILLION,
    )


async def _capture_live(
    batches: list[RequestBatch],
    config: Any,
    endpoint: str,
    api_key: str,
    max_cost: float,
    metadata: dict[str, Any],
    answers_path: Path,
    receipts_path: Path,
) -> None:
    jev = _capture_jev_config(config)
    budget = _capture_budget(len(batches), max_cost, config.jev.retries)
    request_budget = _request_budget(config)
    plans = []
    for index, batch in enumerate(batches):
        violations = request_budget.violations(batch.state, batch.wires)
        if violations:
            details = ", ".join(sorted(violations))
            raise ValueError(f"request batch {index} exceeds request budget ({details})")
        body = request_budget.body(batch.state, batch.wires)
        plans.append({
            "request_index": index,
            "source_evidence_sha256": batch.evidence.key,
            "evidence_sha256": _sha256_bytes(batch.state),
            "question_ids": sorted(batch.questions),
            "body_sha256": _sha256_bytes(body),
            "reserved_input_tokens": math.ceil(len(body) / BYTES_PER_TOKEN) + TOKEN_RESERVE,
        })
    estimated = sum(cast(int, item["reserved_input_tokens"]) for item in plans) * INPUT_PRICE_PER_MILLION / 1_000_000
    if estimated > max_cost:
        raise RuntimeError(f"preflight reservation ${estimated:.6f} exceeds ${max_cost:.6f}")
    answer_rows: list[dict[str, Any]] = []
    receipt_rows: list[dict[str, Any]] = []
    async with JevClient(
        jev, api_key, base_url=endpoint, budget=budget, bytes_per_token=BYTES_PER_TOKEN, token_reserve=TOKEN_RESERVE
    ) as client:
        for plan, batch in zip(plans, batches, strict=True):
            body = request_budget.body(batch.state, batch.wires)
            reservation = ReservationUsage()
            response: JevResponse = await client.evaluate(body, batch.questions, reservation=reservation)
            answer_rows.extend(
                {
                    "scenario_id": item.scenario.scenario_id,
                    "rule_id": item.scenario.rule_id,
                    "answer": response.answers[item.scenario.scenario_id].model_dump(mode="json"),
                    "returned_model": response.model,
                    "evidence_sha256": _sha256_bytes(batch.state),
                }
                for item in batch.scenarios
            )
            reported_input = response.usage.input_tokens
            receipt_rows.append({
                **plan,
                "returned_model": response.model,
                "reserved_input_tokens": reservation.input_tokens,
                "reported_input_tokens": reported_input,
                "reported_output_tokens": response.usage.output_tokens,
                "reserved_cost": reservation.input_tokens * INPUT_PRICE_PER_MILLION / 1_000_000,
                "reported_cost": reported_input * INPUT_PRICE_PER_MILLION / 1_000_000
                if reported_input is not None
                else None,
            })
    metadata = {
        **metadata,
        "captured_at": _now(),
        "model": MODEL,
        "endpoint": endpoint,
        "requests": len(receipt_rows),
        "reserved_cost": sum(row["reserved_cost"] for row in receipt_rows),
        "reported_cost": sum(row["reported_cost"] or 0 for row in receipt_rows),
    }
    _dump_jsonl(answers_path, answer_rows)
    _dump_jsonl(receipts_path, receipt_rows)
    _dump_json(answers_path.with_suffix(".meta.json"), metadata)


def cmd_plan(args: argparse.Namespace) -> int:
    metadata, scenarios, config = _load_manifest(args.manifest, args.config, args.snapshot_sha256)
    prepared = _prepare(scenarios, config)
    output = {
        **metadata,
        "plan": _plan(prepared, config),
        "support": _group_support(prepared),
        "paid_call": False,
    }
    if args.output:
        _dump_json(args.output, output)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    try:
        document = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read manifest {args.manifest}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("scenarios"), list):
        raise TypeError("expanded manifest must contain scenarios")
    source_root = (args.manifest.parent / _relative(document["source_root"], "source_root")).resolve()
    snapshot = document.get("source_snapshot", {})
    snapshot_hash = snapshot.get("digest") if isinstance(snapshot, dict) else None
    actual_snapshot_hash = _source_snapshot_hash(source_root)
    if args.snapshot_sha256 is not None and actual_snapshot_hash != args.snapshot_sha256:
        raise ValueError("expanded source snapshot hash does not match the requested reviewed snapshot")
    snapshot_hash = args.snapshot_sha256 or actual_snapshot_hash or snapshot_hash
    if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64:
        raise ValueError("expanded manifest source_snapshot.digest is required")
    common = {
        "schema_version": 1,
        "corpus": document.get("corpus", "expanded"),
        "corpus_revision": document.get("corpus_revision"),
        "provenance": document.get("provenance"),
        "source_root": str(source_root),
        "snapshot_manifest_sha256": snapshot_hash,
        "source_contract": document.get("source_contract", {}),
    }
    source_root_name = Path(str(document["source_root"])).name
    outputs: dict[str, list[dict[str, Any]]] = {"development": [], "heldout": []}
    prefix = args.group_prefix
    for raw in document["scenarios"]:
        if not isinstance(raw, dict):
            raise TypeError("expanded scenario must be an object")
        partition = raw.get("partition")
        if partition not in outputs:
            raise ValueError(f"unsupported scenario partition {partition!r}")
        scenario = {
            key: value
            for key, value in raw.items()
            if key not in {"partition", "provisional_label", "generator_provenance"}
        }
        scenario["id"] = f"{prefix}{raw['id']}"
        scenario["scenario_group"] = f"{prefix}{raw['scenario_group']}"
        source_name = str(raw["source"])
        source_name = source_name.removeprefix(source_root_name + "/")
        scenario["source"] = source_name
        scenario["split"] = partition
        scenario["provisional"] = {"label": raw.get("provisional_label")}
        outputs[partition].append(scenario)
    for partition, scenarios in outputs.items():
        output = args.dev_output if partition == "development" else args.heldout_output
        _dump_yaml(output, {**common, "scenarios": scenarios, "split": partition})
    print(
        json.dumps(
            {
                "snapshot_manifest_sha256": snapshot_hash,
                "development": len(outputs["development"]),
                "heldout": len(outputs["heldout"]),
                "group_prefix": prefix,
                "outputs": {"development": str(args.dev_output), "heldout": str(args.heldout_output)},
            },
            indent=2,
        )
    )
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    if args.live and args.max_cost is None:
        raise ValueError("--live requires an explicit --max-cost")
    metadata, scenarios, config = _load_manifest(args.manifest, args.config, args.snapshot_sha256)
    prepared = _prepare(scenarios, config)
    plan = _plan(prepared, config)
    if not args.live:
        output = {**metadata, "plan": plan, "support": _group_support(prepared), "paid_call": False}
        _dump_json(args.output.with_suffix(".plan.json"), output)
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    assert args.max_cost is not None
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY is unavailable; capture was not executed")
    asyncio.run(
        _capture_live(
            _batches(prepared, config),
            config,
            args.endpoint,
            api_key,
            args.max_cost,
            {**metadata, "support": _group_support(prepared)},
            args.output,
            args.receipts,
        )
    )
    print(json.dumps({"answers": str(args.output), "receipts": str(args.receipts)}, indent=2))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    metadata, scenarios, config = _load_manifest(args.manifest, args.config, args.snapshot_sha256)
    prepared = _prepare(scenarios, config)
    answer_metadata, answers = _load_answers(args.answers)
    if answer_metadata.get("manifest_sha256") != metadata["manifest_sha256"]:
        raise ValueError("captured answers were made from a different manifest")
    _validate_captured_answers(prepared, answers)
    adjudications = _adjudication_index(args.adjudications)
    adjudication_sha256 = _sha256_bytes(args.adjudications.read_bytes())
    cases: list[CalibrationCase] = []
    pending: list[str] = []
    skipped: list[dict[str, Any]] = []
    for item in prepared:
        if item.applicability_skip is not None:
            skipped.append({
                "scenario_id": item.scenario.scenario_id,
                "rule_id": item.scenario.rule_id,
                "reason": item.applicability_skip,
            })
            continue
        captured = answers.get(item.scenario.scenario_id)
        if captured is None:
            pending.append(item.scenario.scenario_id)
            continue
        symbol = item.scenario.target.qualified_name
        if item.scenario.target.scope == "file":
            symbol = Path(item.scenario.source_path).name
        path_candidates = {
            item.scenario.source_path,
            item.scenario.source_path.removeprefix("sources/"),
            Path(item.scenario.source_path).name,
        }
        adjudication = adjudications.get(item.scenario.scenario_id)
        for source_path in path_candidates:
            if adjudication is not None:
                break
            adjudication = adjudications.get((source_path, symbol, item.scenario.rule_id))
        if adjudication is None:
            # A missing finalized label is not filled from the provisional
            # manifest.  It remains pending for blind review.
            pending.append(item.scenario.scenario_id)
            continue
        returned_model = _string(captured.get("returned_model"), f"{item.scenario.scenario_id}.returned_model")
        answer = ANSWER_ADAPTER.validate_python(captured.get("answer"))
        cases.append(
            _make_case(
                item,
                answer,
                adjudication,
                answer_metadata.get("endpoint", "https://api.typesafe.ai"),
                returned_model,
                metadata["manifest_sha256"],
                adjudication_sha256,
            )
        )
    _dump_jsonl(args.output, [case.model_dump(mode="json") for case in cases])
    summary = {
        **metadata,
        "captured_metadata": answer_metadata,
        "adjudications_sha256": adjudication_sha256,
        "cases": len(cases),
        "pending_unadjudicated": pending,
        "applicability_skips": skipped,
        "support": _group_support([item for item in prepared if item.applicability_skip is None]),
        "provisional_labels_used_as_ground_truth": False,
    }
    _dump_json(args.summary, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="All-builtins synthetic calibration capture/import")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="validate production targets and estimate requests without API calls")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--config", type=Path, help="project YAML config; defaults to packaged rules")
    plan.add_argument("--snapshot-sha256", help="require the reviewed source snapshot identity")
    plan.add_argument("--output", type=Path)
    plan.set_defaults(function=cmd_plan)
    normalize = sub.add_parser("normalize", help="normalize partitioned expanded manifests without using labels")
    normalize.add_argument("--manifest", type=Path, required=True)
    normalize.add_argument("--dev-output", type=Path, required=True)
    normalize.add_argument("--heldout-output", type=Path, required=True)
    normalize.add_argument("--snapshot-sha256", help="require the reviewed source snapshot identity")
    normalize.add_argument("--group-prefix", default="expanded:")
    normalize.set_defaults(function=cmd_normalize)
    capture = sub.add_parser("capture", help="capture unlabeled production answers, or plan by default")
    capture.add_argument("--manifest", type=Path, required=True)
    capture.add_argument("--config", type=Path, help="project YAML config; defaults to packaged rules")
    capture.add_argument("--snapshot-sha256", help="require the reviewed source snapshot identity")
    capture.add_argument("--output", type=Path, default=Path(".jevscan-calibration/synthetic-answers.jsonl"))
    capture.add_argument("--receipts", type=Path, default=Path(".jevscan-calibration/synthetic-receipts.jsonl"))
    capture.add_argument("--live", action="store_true", help="allow live API calls; omitted means plan-only")
    capture.add_argument("--max-cost", type=float, help="required hard cost cap for --live")
    capture.add_argument("--endpoint", default=os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai"))
    capture.set_defaults(function=cmd_capture)
    import_parser = sub.add_parser("import", help="import captured answers using finalized blind adjudications")
    import_parser.add_argument("--manifest", type=Path, required=True)
    import_parser.add_argument("--config", type=Path, help="project YAML config; defaults to packaged rules")
    import_parser.add_argument("--snapshot-sha256", help="require the reviewed source snapshot identity")
    import_parser.add_argument("--answers", type=Path, required=True)
    import_parser.add_argument("--adjudications", type=Path, required=True)
    import_parser.add_argument("--output", type=Path, default=Path(".jevscan-calibration/synthetic-cases.jsonl"))
    import_parser.add_argument("--summary", type=Path, default=Path(".jevscan-calibration/synthetic-summary.json"))
    import_parser.set_defaults(function=cmd_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.function(args)
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"synthetic calibration: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
