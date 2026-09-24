"""Materialize strict v1 calibration cases from an offline runner capture.

The label manifest is deliberately authoritative and positional.  Every row
must name its original displayed ID, target signature, rule, semantic label,
and explanation.  The importer rejects duplicate IDs, ambiguous target
matches, missing IDs, and any attempt to infer a label from a raw answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from jevscan.core.protocol import (
    QUESTION_POLICY_V5,
    RUBRIC_STATE_KEY,
    encode,
    prompt_binder,
)
from jevscan.core.rules import Rule
from jevscan.core.semantic_calibration import CalibrationCase, TargetRecord

CASE_VERSION = 1
CAPTURE_FORMAT_VERSION = 1
CAPTURE_PROMPT_VERSION = 5


def _hash_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _hash_text(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


def _target_key(target: dict[str, Any]) -> tuple[Any, ...]:
    return (
        target["path"],
        target["start_byte"],
        target["end_byte"],
        target["qualified_name"],
        target["kind"],
    )


def _wire_target_key(wire: dict[str, Any]) -> tuple[Any, ...]:
    instructions = wire.get("instructions")
    if not isinstance(instructions, dict) or not isinstance(instructions.get("target"), dict):
        raise TypeError("captured question has no bound target metadata")
    target = instructions["target"]
    span = target.get("span")
    if span is None and {"start_byte", "end_byte"} <= target.keys():
        span = [target["start_byte"], target["end_byte"]]
    if span is not None and (not isinstance(span, list) or len(span) != 2):
        raise ValueError("captured question has an invalid target byte span")
    return (
        target.get("path"),
        target.get("start_line"),
        target.get("end_line"),
        target.get("name", target.get("qualified_name")),
        target.get("kind"),
        tuple(span) if span is not None else None,
    )


def _event_target_key(target: dict[str, Any]) -> tuple[Any, ...]:
    return (
        target["path"],
        target["start_line"],
        target["end_line"],
        target["qualified_name"],
        target["kind"],
        (target["start_byte"], target["end_byte"]),
    )


def _captured_material(
    indexed: dict[tuple[str, tuple[Any, ...]], dict[str, Any]],
    question_id: str,
    target: dict[str, Any],
) -> dict[str, Any] | None:
    locator = _event_target_key(target)
    material = indexed.get((question_id, locator))
    if material is not None:
        return material
    # Version-5 bound questions did not include byte spans. Their question IDs
    # are file-local and disambiguate legacy same-line target locators.
    return indexed.get((question_id, (*locator[:-1], None)))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def _git_commit(source: Path) -> str | None:
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


def _event_records(events_path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{events_path}:{line_number}: invalid JSON: {exc}") from exc
        if event.get("event") == "evaluation" and event.get("inference"):
            records.append(event)
    return records


def _indexed_material(capture: dict[str, Any]) -> dict[tuple[str, tuple[Any, ...]], dict[str, Any]]:
    indexed: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = {}
    for request in capture.get("requests", []):
        for question_id, wire in request.get("questions", {}).items():
            key = (question_id, _wire_target_key(wire))
            if key in indexed:
                previous = indexed[key]
                if previous["state_sha256"] != request["state_sha256"]:
                    raise ValueError(f"question {question_id} has conflicting captured evidence")
                continue
            cached = request.get("cached", {}).get(question_id)
            indexed[key] = {
                "state": request["state"],
                "state_sha256": request["state_sha256"],
                "wire": wire,
                "cached": cached,
            }
    return indexed


def _rule_documents(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for event in events:
        for rule_id, metadata in event.get("rule_metadata", {}).items():
            rules.setdefault(rule_id, {"title": metadata.get("title", ""), "ruleset": metadata.get("ruleset", "JEV")})
    return rules


def _capture_rules(capture: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    declared = capture.get("rules")
    if isinstance(declared, dict) and declared:
        return {name: rule for name, rule in declared.items() if isinstance(name, str) and isinstance(rule, dict)}
    return _rule_documents(events)


def _find_event(events: list[dict[str, Any]], display: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    target_signature = display.get("target")
    rule_id = display.get("rule_id")
    if not isinstance(target_signature, dict) or not isinstance(rule_id, str):
        raise TypeError("each label row requires target and rule_id")
    matches = []
    for event in events:
        if _event_target_key(event["target"]) != _event_target_key(target_signature):
            continue
        inference = event.get("inference", {}).get(rule_id)
        if isinstance(inference, dict) and isinstance(inference.get("question_id"), str):
            matches.append((event, inference["question_id"], event.get("evidence", {}).get(rule_id, {})))
    if len(matches) != 1:
        raise ValueError(
            f"displayed ID {display.get('display_id')!r} matched {len(matches)} "
            f"runner evaluations for {target_signature.get('path')}:{target_signature.get('start_line')} {rule_id}"
        )
    return matches[0]


def _source_documents(root: Path, state: dict[str, Any]) -> dict[str, str]:
    paths = {document.get("path") for document in state.get("documents", [])}
    if not paths or any(not isinstance(path, str) or not path for path in paths):
        raise ValueError("captured evidence must contain document paths")
    result = {}
    for path in paths:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"cannot safely read evidence source document {path!r}")
        candidate = root / relative
        if not candidate.is_file():
            raise ValueError(f"cannot safely read evidence source document {path!r}")
        result[path] = candidate.read_text(encoding="utf-8")
    return result


def _frozen_capture_prompt(rule: Rule, target: dict[str, Any], material: dict[str, Any]) -> dict[str, Any]:
    """Validate the capture-v1 request as the historical version-5 wire."""
    state = material.get("state")
    wire = material.get("wire")
    if not isinstance(state, dict) or RUBRIC_STATE_KEY in state or not isinstance(wire, dict):
        raise ValueError("offline runner capture v1 must contain frozen version-5 state and question material")
    target_record = TargetRecord.model_validate(target).to_target()
    expected = prompt_binder(CAPTURE_PROMPT_VERSION, QUESTION_POLICY_V5).bind(
        rule.question, target_record, QUESTION_POLICY_V5
    )
    if wire != expected:
        raise ValueError("offline runner capture v1 question does not match the frozen version-5 wire")
    return {"version": CAPTURE_PROMPT_VERSION, "policy": QUESTION_POLICY_V5}


def _case(
    display: dict[str, Any],
    event: dict[str, Any],
    evidence_meta: dict[str, Any],
    material: dict[str, Any],
    rules: dict[str, dict[str, Any]],
    source_root: Path,
    capture: dict[str, Any],
) -> dict[str, Any]:
    if capture.get("version") != CAPTURE_FORMAT_VERSION:
        raise ValueError("offline runner capture format version 1 is required (frozen prompt version 5)")
    cached = material.get("cached")
    if not isinstance(cached, dict):
        raise TypeError(f"displayed ID {display['display_id']} has no pre-existing cached answer")
    answer = cached["answer"]
    returned_model = cached["model"]
    if returned_model == capture.get("synthetic_model"):
        raise ValueError(f"displayed ID {display['display_id']} only has a synthetic capture answer")
    rule_id = display["rule_id"]
    rule = display.get("rule_definition") or rules.get(rule_id)
    if not isinstance(rule, dict) or "question" not in rule or "report" not in rule:
        raise ValueError(
            f"displayed ID {display['display_id']} lacks a complete rule_definition; "
            "raw runner metadata is not sufficient to reconstruct the rule contract"
        )
    rule = Rule.model_validate(rule).model_dump(mode="json")
    target = event["target"]
    state = material["state"]
    sources = _source_documents(source_root, state)
    prompt = _frozen_capture_prompt(Rule.model_validate(rule), target, material)
    endpoint = capture["endpoint"]
    requested_model = capture["requested_model"]
    hashes = {
        "question": _hash_bytes(encode(material["wire"])),
        "evidence": _hash_bytes(encode(state)),
        "source_documents": {path: _hash_text(text) for path, text in sorted(sources.items())},
        "rule": _hash_bytes(encode(rule)),
        "report": _hash_bytes(encode(rule["report"])),
        "prompt": _hash_bytes(encode(prompt)),
        "endpoint": _hash_text(endpoint),
        "requested_model": _hash_text(requested_model),
        "returned_model": _hash_text(returned_model),
    }
    label = display.get("label")
    if label not in {"Agree", "Partial", "Disagree"}:
        raise ValueError(f"displayed ID {display['display_id']} has no explicit semantic label")
    if not isinstance(display.get("explanation"), str) or not display["explanation"].strip():
        raise ValueError(f"displayed ID {display['display_id']} has no explanation")
    if label == "Disagree" and display.get("adjudicated_severity") is not None:
        raise ValueError("Disagree rows must omit adjudicated_severity")
    case = {
        "version": CASE_VERSION,
        "case_id": f"baseline-{int(display['display_id']):03d}",
        "rule_id": rule_id,
        "rule": rule,
        "target": target,
        "answer": answer,
        "context_complete": bool(evidence_meta.get("context_complete", False)),
        "target_complete": bool(evidence_meta.get("target_complete", False)),
        "split": display.get("split", "development"),
        "label": label,
        "explanation": display["explanation"],
        "provenance": {
            "source_commit": capture.get("source_commit"),
            "source_root": "jevscan-frozen-public-checkout",
            "source_group": f"jevscan@{capture['source_commit']}:{target['path']}",
            "capture_version": capture["version"],
            "display_id": display["display_id"],
            "baseline_target": target["id"],
            "source": display.get("provenance", "offline-baseline-cache"),
        },
        "evidence": {"state": state, "source_documents": sources},
        "prompt": prompt,
        "endpoint": endpoint,
        "requested_model": requested_model,
        "returned_model": returned_model,
        "hashes": hashes,
        "comparability": {
            "question": hashes["question"],
            "evidence": hashes["evidence"],
            "prompt": {"version": CAPTURE_PROMPT_VERSION, "identity": hashes["prompt"]},
            "endpoint": endpoint,
            "model": returned_model,
        },
    }
    for field in ("displayed_status", "displayed_answer"):
        if field in display:
            case["provenance"][field] = display[field]
    if display.get("adjudicated_severity") is not None:
        case["adjudicated_severity"] = display["adjudicated_severity"]
    CalibrationCase.model_validate(case)
    return case


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import exact displayed-order labels into strict calibration JSONL.")
    parser.add_argument("capture", type=Path, help="capture_cache.py JSON output")
    parser.add_argument("events", type=Path, help="capture_cache.py runner JSONL events")
    parser.add_argument("source_root", type=Path, help="same frozen source tree used by the capture")
    parser.add_argument("labels", type=Path, help="ordered JSON array of explicit label rows")
    parser.add_argument("--output", type=Path, required=True, help="strict version-1 calibration JSONL")
    parser.add_argument(
        "--allow-partial-selected",
        action="store_true",
        help="import only explicitly selected concrete cache answers from a partial exploratory capture",
    )
    return parser


def import_cases(
    capture_path: Path,
    events_path: Path,
    source_root: Path,
    labels_path: Path,
    output: Path,
    *,
    allow_partial_selected: bool = False,
) -> None:
    capture = _read_json(capture_path)
    labels = _read_json(labels_path)
    if capture.get("transport_blocked") is not True:
        raise ValueError("capture was not marked transport_blocked")
    partial = bool(capture.get("partial") or capture.get("cache_misses") or capture.get("synthetic_requests"))
    if partial and not allow_partial_selected:
        raise ValueError("capture is partial; durable baseline import requires a complete cache-only run")
    expected_commit = capture.get("source_commit")
    actual_commit = _git_commit(source_root.resolve())
    if not isinstance(expected_commit, str) or not expected_commit:
        raise ValueError("capture has no source commit")
    if actual_commit is not None and actual_commit != expected_commit:
        raise ValueError(f"source commit mismatch: capture={expected_commit} source={actual_commit}")
    if not isinstance(labels, list) or not labels:
        raise ValueError("labels must be a nonempty JSON array in displayed order")
    display_ids = [row.get("display_id") for row in labels if isinstance(row, dict)]
    if len(display_ids) != len(set(display_ids)) or sorted(display_ids) != list(range(1, len(labels) + 1)):
        raise ValueError("labels must contain each displayed ID exactly once, in contiguous order")

    events = _event_records(events_path)
    indexed = _indexed_material(capture)
    rules = _capture_rules(capture, events)
    cases = []
    used_keys: set[tuple[str, tuple[Any, ...]]] = set()
    for display in labels:
        event, question_id, evidence_meta = _find_event(events, display)
        material = _captured_material(indexed, question_id, event["target"])
        if material is None:
            raise ValueError(f"displayed ID {display['display_id']} has no captured question material")
        if material["cached"] is None:
            raise ValueError(f"displayed ID {display['display_id']} was not present in the baseline cache")
        stable = (display["rule_id"], _target_key(event["target"]))
        if stable in used_keys:
            raise ValueError(f"duplicate displayed target/rule {stable}")
        used_keys.add(stable)
        case = _case(display, event, evidence_meta, material, rules, source_root.resolve(), capture)
        if partial:
            case["provenance"]["capture_scope"] = "selected-initial-cache-records"
            case["provenance"]["capture_partial"] = True
            case["provenance"]["synthetic_requests_total"] = len(capture.get("synthetic_requests", []))
            case["provenance"]["cache_misses_total"] = len(capture.get("cache_misses", []))
        cases.append(case)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        for case in cases:
            stream.write(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def main() -> int:
    args = _parser().parse_args()
    try:
        import_cases(
            args.capture,
            args.events,
            args.source_root,
            args.labels,
            args.output,
            allow_partial_selected=args.allow_partial_selected,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"import_cases: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
