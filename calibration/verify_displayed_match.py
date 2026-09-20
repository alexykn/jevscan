"""Verify cached cases reproduce the original displayed answer/status summary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jevscan.core.protocol import NoulAnswer, ScoreAnswer
from jevscan.core.semantic_calibration import load_cases, replay_case


def _displayed_value(value: str) -> tuple[str, float]:
    kind, rest = value.split("=", 1)
    number = float(rest.split("/", 1)[0].split(" ", 1)[0])
    return kind, number


def verify(cases_path: Path) -> dict[str, Any]:
    cases = load_cases(cases_path)
    mismatches: list[dict[str, Any]] = []
    for case in cases:
        displayed = case.provenance.get("displayed_answer")
        displayed_status = case.provenance.get("displayed_status")
        if not isinstance(displayed, str) or not isinstance(displayed_status, str):
            mismatches.append({"case_id": case.case_id, "reason": "missing displayed provenance"})
            continue
        kind, expected_value = _displayed_value(displayed)
        if kind == "noul":
            if not isinstance(case.answer, NoulAnswer):
                raise ValueError(f"{case.case_id}: displayed noul answer has a different stored answer type")
            actual_value = case.answer.noul
        elif kind == "score":
            if not isinstance(case.answer, ScoreAnswer):
                raise ValueError(f"{case.case_id}: displayed score answer has a different stored answer type")
            actual_value = case.answer.score
        else:
            raise ValueError(f"{case.case_id}: unsupported displayed answer type {kind!r}")
        if abs(actual_value - expected_value) > 0.0006:
            mismatches.append({
                "case_id": case.case_id,
                "reason": "answer",
                "displayed": displayed,
                "actual": case.answer.model_dump(mode="json"),
            })
            continue
        assessment = replay_case(case).assessment
        actual_status = (
            "confirmed"
            if assessment.status in {"warning", "error"}
            else ("tentative" if assessment.tentative_finding is not None else assessment.status)
        )
        if actual_status != displayed_status:
            mismatches.append({
                "case_id": case.case_id,
                "reason": "status",
                "displayed": displayed_status,
                "actual": actual_status,
            })
    return {
        "version": 1,
        "cases": len(cases),
        "evidence_note": (
            "The persisted displayed transcripts expose target identity and answer/status summaries, "
            "not the complete final encoded evidence state. Case validation proves every stored evidence "
            "slice against the frozen source; it does not claim final enrichment-state identity."
        ),
        "answer_matches": len(cases) - sum(item["reason"] == "answer" for item in mismatches),
        "status_matches": len(cases) - sum(item["reason"] == "status" for item in mismatches),
        "mismatches": mismatches,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify selected cache cases against original displayed summaries.")
    parser.add_argument("cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = verify(args.cases)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"verify_displayed_match: {exc}")
        return 2
    return 0 if not result["mismatches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
