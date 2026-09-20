"""Run reporting-only threshold sweeps over strict offline calibration cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jevscan.core.rules import ReportPolicy
from jevscan.core.semantic_calibration import load_cases, replay_cases


def _thresholds(value: str) -> list[float]:
    try:
        values = [float(item) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("--thresholds must be comma-separated numbers") from exc
    if not values or any(not 0 <= item <= 1 for item in values):
        raise ValueError("--thresholds must contain values in [0, 1]")
    if len(set(values)) != len(values):
        raise ValueError("--thresholds must not contain duplicates")
    return values


def _overrides(cases: list[Any], field: str, threshold: float) -> tuple[dict[str, Any], list[str]]:
    overrides: dict[str, Any] = {}
    changed: list[str] = []
    for case in cases:
        rule_id = case.rule_id
        policy = case.rule.report.model_dump(mode="python")
        level_name, field_name = field.split(".", 1)
        level = policy["levels"][level_name]
        if field_name not in level or level[field_name] is None:
            continue
        level[field_name] = threshold
        candidate = ReportPolicy.model_validate(policy)
        existing = overrides.get(rule_id)
        serialized = candidate.model_dump(mode="json")
        if existing is not None and existing != serialized:
            raise ValueError(f"rule {rule_id} has conflicting report policies across cases")
        overrides[rule_id] = serialized
        if rule_id not in changed:
            changed.append(rule_id)
    return overrides, sorted(changed)


def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Keep aggregate threshold outcomes without repeating case material."""
    return {
        "version": report["version"],
        "cases": report["cases"],
        "non_comparable": report["non_comparable"],
        "rules": report["rules"],
    }


def sweep(cases_path: Path, thresholds: list[float], field: str) -> dict[str, Any]:
    cases = load_cases(cases_path)
    level, name = field.split(".", 1)
    if level not in {"warning", "error"} or name not in {"min_probability", "min_confidence"}:
        raise ValueError(
            "--field must be warning.min_probability, warning.min_confidence, "
            "error.min_probability, or error.min_confidence"
        )
    results = []
    for threshold in thresholds:
        overrides, changed = _overrides(cases, field, threshold)
        full_report = replay_cases(cases, overrides).as_dict()
        report = _compact_report(full_report)
        records = full_report["records"]
        results.append({
            "threshold": threshold,
            "field": field,
            "changed_rule_ids": changed,
            "confirmed": sum(record["status"] in {"warning", "error"} for record in records),
            "tentative": sum(record["tentative_finding"] is not None for record in records),
            "report": report,
        })
    return {"version": 1, "cases": len(cases), "mode": "reporting-only", "sweeps": results}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay strict cases through reporting-only threshold overrides.")
    parser.add_argument("cases", type=Path, help="strict version-1 calibration JSONL")
    parser.add_argument("--field", default="warning.min_probability")
    parser.add_argument("--thresholds", default="0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        document = sweep(args.cases, _thresholds(args.thresholds), args.field)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"sweep_thresholds: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
