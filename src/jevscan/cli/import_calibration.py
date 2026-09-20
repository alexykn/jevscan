"""Import final scan judgments and external adjudication labels into strict cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from jevscan.core.protocol import encode
from jevscan.core.semantic_calibration import CalibrationCase


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256(value.encode("utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        rows = []
        seen: set[str] = set()
        for line_number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise TypeError(f"{path}:{line_number}: expected an object")
            case_id = value.get("case_id")
            if not isinstance(case_id, str) or case_id in seen:
                raise ValueError(f"{path}:{line_number}: duplicate or missing case_id")
            seen.add(case_id)
            rows.append(value)
    if not rows:
        raise ValueError(f"{path}: capture is empty")
    return rows


def _capture_metadata(path: Path) -> dict[str, Any]:
    metadata_path = path.with_suffix(".meta.json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read capture metadata {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise TypeError("capture metadata must be an object")
    return metadata


def _labels(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    entries = document.get("cases") if isinstance(document, dict) else document
    if isinstance(entries, dict):
        normalized = []
        for case_id, value in entries.items():
            if not isinstance(value, dict):
                raise TypeError("label mapping values must be objects")
            if "case_id" in value and value["case_id"] != case_id:
                raise ValueError(f"label key conflicts with embedded case_id {case_id!r}")
            normalized.append({"case_id": case_id, **value})
        entries = normalized
    if not isinstance(entries, list):
        raise TypeError("labels must be a list or a mapping with case IDs")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("case_id"), str):
            raise TypeError("every label must contain case_id")
        case_id = entry["case_id"]
        if case_id in result:
            raise ValueError(f"duplicate label {case_id!r}")
        unknown = set(entry) - {
            "case_id",
            "label",
            "split",
            "explanation",
            "adjudicated_severity",
            "source_group",
            "scenario_group",
        }
        if unknown:
            raise ValueError(f"{case_id}: unsupported label fields {sorted(unknown)}")
        for field in ("source_group", "scenario_group"):
            if field in entry and (not isinstance(entry[field], str) or not entry[field].strip()):
                raise ValueError(f"{case_id}: {field} must be a nonempty string")
        result[case_id] = entry
    return result


def _validate_label_ids(row_ids: set[str | None], label_ids: set[str]) -> None:
    if row_ids != label_ids:
        missing = sorted(item for item in row_ids - label_ids if item is not None)
        extra = sorted(label_ids - row_ids)
        raise ValueError(f"label case IDs do not match capture (missing={missing}, extra={extra})")


def _require_complete(metadata: dict[str, Any], allow_incomplete: bool) -> None:
    if type(metadata.get("complete")) is not bool:
        raise ValueError("capture metadata.complete must be a boolean")
    if type(metadata.get("records")) is not int:
        raise ValueError("capture metadata.records must be an integer")
    if not isinstance(metadata.get("capture_sha256"), str):
        raise TypeError("capture metadata.capture_sha256 is required")
    if not metadata["complete"] and not allow_incomplete:
        raise ValueError("capture is incomplete; pass --allow-incomplete to import it explicitly")


def _verify_capture_metadata(path: Path, metadata: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if metadata["records"] != len(rows):
        raise ValueError("capture metadata.records does not match JSONL rows")
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if metadata["capture_sha256"] != actual:
        raise ValueError("capture JSONL digest does not match its sidecar")


def _case(row: dict[str, Any], label: dict[str, Any]) -> CalibrationCase:
    required = (
        "case_id",
        "rule_id",
        "rule",
        "target",
        "answer",
        "context_complete",
        "target_complete",
        "question_wire",
        "evidence",
        "prompt",
        "endpoint",
        "requested_model",
        "returned_model",
    )
    missing = [key for key in required if key not in row]
    if missing:
        raise ValueError(f"{row.get('case_id', '<unknown>')}: capture missing {', '.join(missing)}")
    hashes = {
        "question": f"sha256:{_sha256(encode(row['question_wire']))}",
        "evidence": f"sha256:{_sha256(encode(row['evidence']['state']))}",
        "source_documents": {
            path: f"sha256:{_sha256_text(content)}"
            for path, content in sorted(row["evidence"]["source_documents"].items())
        },
        "rule": f"sha256:{_sha256(encode(row['rule']))}",
        "report": f"sha256:{_sha256(encode(row['rule']['report']))}",
        "prompt": f"sha256:{_sha256(encode(row['prompt']))}",
        "endpoint": f"sha256:{_sha256_text(row['endpoint'])}",
        "requested_model": f"sha256:{_sha256_text(row['requested_model'])}",
        "returned_model": f"sha256:{_sha256_text(row['returned_model'])}",
    }
    final_capture = {
        "phase": "final",
        "question_wire": row["question_wire"],
        "evidence": row["evidence"],
        "answer": row["answer"],
        "disposition": row.get(
            "disposition",
            {
                "status": row.get("assessment", {}).get("status", "unknown"),
                "reason": row.get("assessment", {}).get("reason", ""),
            },
        ),
        "context_complete": row["context_complete"],
        "target_complete": row["target_complete"],
        "returned_model": row["returned_model"],
        "assessment": row.get("assessment", {}),
        "initial": row.get("initial"),
        "review": row.get("review", {}),
    }
    label_name = label.get("label")
    if label_name not in {"Agree", "Partial", "Disagree"}:
        raise ValueError(f"{row['case_id']}: label must be Agree, Partial, or Disagree")
    severity = label.get("adjudicated_severity")
    if label_name == "Disagree" and severity is not None:
        raise ValueError(f"{row['case_id']}: Disagree cannot carry severity")
    return CalibrationCase.model_validate({
        "version": 1,
        "case_id": row["case_id"],
        "rule_id": row["rule_id"],
        "rule": row["rule"],
        "target": row["target"],
        "answer": row["answer"],
        "context_complete": row["context_complete"],
        "target_complete": row["target_complete"],
        "question_wire": row["question_wire"],
        "capture": final_capture,
        "split": label.get("split", "final"),
        "label": label_name,
        "adjudicated_severity": severity,
        "explanation": label.get("explanation", "External final-judgment adjudication"),
        "provenance": {
            "source": "final-scan-adjudication",
            "capture_phase": "final",
            "assessment": row.get("assessment", {}),
            "disposition": row.get("disposition", {}),
            "review": row.get("review", {}),
            "inference": row.get("inference", {}),
            "cached": row.get("cached", False),
            "fully_cached": row.get("fully_cached", False),
            **{key: label[key] for key in ("source_group", "scenario_group") if key in label},
        },
        "evidence": row["evidence"],
        "prompt": row["prompt"],
        "endpoint": row["endpoint"],
        "requested_model": row["requested_model"],
        "returned_model": row["returned_model"],
        "hashes": hashes,
        "comparability": {
            "question": hashes["question"],
            "evidence": hashes["evidence"],
            "prompt": {"version": row["prompt"]["version"], "identity": hashes["prompt"]},
            "endpoint": row["endpoint"],
            "model": row["returned_model"],
        },
    })


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jevscan-calibration-import",
        description="Import final jevscan judgment captures with external labels",
    )
    parser.add_argument("capture", type=Path, help="final-judgment JSONL from --calibration-output")
    parser.add_argument("--labels", type=Path, required=True, help="YAML/JSON labels keyed by case_id")
    parser.add_argument("--allow-incomplete", action="store_true", help="import a capture that did not complete")
    parser.add_argument("-o", "--output", type=Path, required=True, help="strict calibration JSONL output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        metadata = _capture_metadata(args.capture)
        _require_complete(metadata, args.allow_incomplete)
        rows = _read_jsonl(args.capture)
        _verify_capture_metadata(args.capture, metadata, rows)
        skips = [row for row in rows if row.get("kind") in {"applicability", "omitted", "skip"}]
        rows = [row for row in rows if row.get("kind", "judgment") == "judgment"]
        labels = _labels(args.labels)
        row_ids = {row.get("case_id") for row in rows}
        _validate_label_ids(row_ids, set(labels))
        cases = [_case(row, labels[row["case_id"]]) for row in rows]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as stream:
            for case in sorted(cases, key=lambda item: item.case_id):
                stream.write(json.dumps(case.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n")
        if skips:
            args.output.with_suffix(".skips.json").write_text(
                json.dumps(skips, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"jevscan-calibration-import: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
