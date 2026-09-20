"""Compare JEV02 expected-score reporting with probability-mass reporting.

This is an offline, reporting-only ledger. It never changes stored labels or
answers. A separate adjudications file may refine the original Partial rows;
those metrics are emitted under a separate view.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jevscan.core.rules import ReportPolicy
from jevscan.core.semantic_calibration import (
    CalibrationCase,
    CalibrationLabel,
    ReplayRecord,
    load_cases,
    replay_cases,
)

RULE_ID = "JEV02"


def _fraction(count: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "count": count,
        "denominator": denominator,
        "fraction": count / denominator if denominator else None,
    }


def _metrics(records: list[ReplayRecord], labels: Mapping[str, CalibrationLabel] | None = None) -> dict[str, Any]:
    def label(record: ReplayRecord) -> str:
        return labels.get(record.case.case_id, record.case.label) if labels else record.case.label

    confirmed = [record for record in records if record.confirmed]
    tentative = [record for record in records if record.assessment.tentative_finding is not None]
    signal = [record for record in records if record.signal]
    expected_positive = [record for record in records if label(record) == "Agree"]

    return {
        "cases": len(records),
        "label_counts": dict(Counter(label(record) for record in records)),
        "status_counts": dict(Counter(record.assessment.status for record in records)),
        "confirmed": len(confirmed),
        "tentative": len(tentative),
        "signal": len(signal),
        "confirmed_precision": _fraction(sum(label(record) == "Agree" for record in confirmed), len(confirmed)),
        "tentative_label_mix": dict(Counter(label(record) for record in tentative)),
        "confirmed_exact_positive_recall": _fraction(
            sum(record in confirmed for record in expected_positive),
            len(expected_positive),
        ),
        "review_exact_positive_recall": _fraction(
            sum(record in signal for record in expected_positive),
            len(expected_positive),
        ),
    }


def _mass_policy(case: CalibrationCase, threshold: float) -> ReportPolicy:
    policy = case.rule.report.model_dump(mode="python")
    warning = policy["levels"]["warning"]
    error = policy["levels"]["error"]
    warning.update({"score_levels": [2, 3], "min_probability": threshold, "min_score": None, "max_score": None})
    error.update({"score_levels": [3], "min_probability": threshold, "min_score": None, "max_score": None})
    return ReportPolicy.model_validate(policy)


def _label_map(labels_path: Path, adjudications_path: Path) -> tuple[dict[str, CalibrationLabel], dict[str, Any]]:
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    adjudications = json.loads(adjudications_path.read_text(encoding="utf-8"))
    by_target = {
        (row["review_target"]["path"], row["review_target"]["symbol"], row["rule_id"]): row["display_id"]
        for row in labels
    }
    result: dict[str, CalibrationLabel] = {}
    selected: dict[str, Any] = {}
    for rule_id in ("JEV01", "JEV02"):
        for item in adjudications.get(rule_id, []):
            key = (item["path"], item["symbol"], rule_id)
            display_id = by_target.get(key)
            if display_id is None:
                raise ValueError(f"adjudication does not match a displayed target: {key}")
            case_id = f"baseline-{display_id:03d}"
            if rule_id == "JEV01":
                label = "Agree" if item["exact_claim"] else "Partial" if item["warning_worthy"] else "Disagree"
            else:
                level = item["level"]
                label = "Agree" if level >= 2 else "Disagree"
            result[case_id] = label
            selected[case_id] = {
                "rule_id": rule_id,
                "path": item["path"],
                "symbol": item["symbol"],
                "label": label,
                "source_fields": (["exact_claim", "warning_worthy"] if rule_id == "JEV01" else ["level"]),
            }
    return result, {
        "source_commit": adjudications.get("source_commit"),
        "original_label": adjudications.get("original_label"),
        "notes": adjudications.get("notes", []),
        "label_mapping": {
            "JEV01": "Agree when exact_claim and warning_worthy; Partial when only warning_worthy; otherwise Disagree.",
            "JEV02": "Agree at level 2 or higher; Disagree at levels 0 and 1.",
        },
        "cases": selected,
    }


def compare(cases_path: Path, labels_path: Path, adjudications_path: Path, thresholds: list[float]) -> dict[str, Any]:
    cases = load_cases(cases_path)
    jev02 = [case for case in cases if case.rule_id == RULE_ID]
    if not jev02:
        raise ValueError("cases contain no JEV02 records")
    current = replay_cases(jev02)
    current_records = list(current.records)
    policies: list[dict[str, Any]] = [
        {
            "name": "current_expected_score",
            "description": "Existing JEV02 min_score/min_confidence policy; no rule semantics changed.",
            "metrics": _metrics(current_records),
        }
    ]
    for threshold in thresholds:
        override = _mass_policy(jev02[0], threshold)
        report = replay_cases(jev02, {RULE_ID: override})
        policies.append({
            "name": "probability_mass",
            "threshold": threshold,
            "description": "warning score_levels=[2,3], error score_levels=[3], probability-mass threshold.",
            "warning": override.levels.warning.model_dump(mode="json"),
            "error": override.levels.error.model_dump(mode="json"),
            "metrics": _metrics(list(report.records)),
        })

    adjudicated_labels, adjudication_meta = _label_map(labels_path, adjudications_path)
    adjudicated_policies: list[dict[str, Any]] = []
    exact_labels: dict[str, CalibrationLabel] = {record.case.case_id: record.case.label for record in current.records}
    exact_labels.update(adjudicated_labels)
    for policy in policies:
        if policy["name"] == "current_expected_score":
            report = current
        else:
            report = replay_cases(
                jev02,
                {RULE_ID: _mass_policy(jev02[0], policy["threshold"])},
            )
        selected_records = [record for record in report.records if record.case.case_id in adjudicated_labels]
        adjudicated_policies.append({
            **{key: value for key, value in policy.items() if key != "metrics"},
            "metrics": _metrics(selected_records, adjudicated_labels),
        })

    exact_policies: list[dict[str, Any]] = []
    for policy in policies:
        if policy["name"] == "current_expected_score":
            report = current
        else:
            report = replay_cases(
                jev02,
                {RULE_ID: _mass_policy(jev02[0], policy["threshold"])},
            )
        exact_policies.append({
            **{key: value for key, value in policy.items() if key != "metrics"},
            "metrics": _metrics(list(report.records), exact_labels),
        })

    return {
        "version": 1,
        "mode": "reporting-only",
        "rule_id": RULE_ID,
        "cases_total": len(cases),
        "cases_evaluated": len(jev02),
        "original_label_view": policies,
        "adjudicated_partial_refinement_view": {
            "description": "Separate exact view over the 48 parent-adjudicated original Partial rows.",
            **adjudication_meta,
            "policies": adjudicated_policies,
        },
        "exact_adjudicated_label_view": {
            "description": "Complete JEV02 view: parent exact labels replace only the 48 original Partial rows.",
            "label_counts": dict(Counter(exact_labels[record.case.case_id] for record in current.records)),
            "policies": exact_policies,
        },
    }


def _parse_thresholds(value: str) -> list[float]:
    try:
        result = [float(item) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("--thresholds must be comma-separated numbers") from exc
    if not result or any(not 0 <= value <= 1 for value in result):
        raise ValueError("--thresholds must contain values in [0, 1]")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare JEV02 reporting policies offline.")
    parser.add_argument("cases", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("adjudications", type=Path)
    parser.add_argument("--thresholds", default="0.50,0.60,0.70,0.80,0.90")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = compare(args.cases, args.labels, args.adjudications, _parse_thresholds(args.thresholds))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"compare_jev02: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
