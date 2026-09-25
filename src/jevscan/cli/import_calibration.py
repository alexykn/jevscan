"""Import final scan judgments and external adjudication labels into strict cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from jevscan.cli.path_identity import paths_alias
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


def _reject_output_collisions(capture: Path, labels: Path, output: Path) -> None:
    destinations = (output, output.with_suffix(".skips.json"))
    protected = (capture, labels, capture.with_suffix(".meta.json"))
    for destination in destinations:
        for input_path in protected:
            if paths_alias(destination, input_path):
                raise ValueError(f"calibration output {destination} aliases protected input {input_path}")
    if paths_alias(*destinations):
        raise ValueError(f"calibration outputs {destinations[0]} and {destinations[1]} alias")


_LABEL_FIELDS = {
    "case_id",
    "label",
    "split",
    "explanation",
    "adjudicated_severity",
    "source_group",
    "scenario_group",
}


def _mapping_entries(entries: dict[Any, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for case_id, value in entries.items():
        if not isinstance(value, dict):
            raise TypeError("label mapping values must be objects")
        if "case_id" in value and value["case_id"] != case_id:
            raise ValueError(f"label key conflicts with embedded case_id {case_id!r}")
        normalized.append({"case_id": case_id, **value})
    return normalized


def _label_entries(document: Any) -> list[dict[str, Any]]:
    entries = document.get("cases") if isinstance(document, dict) else document
    if isinstance(entries, dict):
        entries = _mapping_entries(entries)
    if not isinstance(entries, list):
        raise TypeError("labels must be a list or a mapping with case IDs")
    return entries


def _validate_label_group(entry: dict[str, Any], case_id: str) -> None:
    for field in ("source_group", "scenario_group"):
        if field not in entry:
            continue
        value = entry[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{case_id}: {field} must be a nonempty string")


def _label_entry(entry: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(entry, dict) or not isinstance(entry.get("case_id"), str):
        raise TypeError("every label must contain case_id")
    case_id = entry["case_id"]
    unknown = set(entry) - _LABEL_FIELDS
    if unknown:
        raise ValueError(f"{case_id}: unsupported label fields {sorted(unknown)}")
    _validate_label_group(entry, case_id)
    return case_id, entry


def _labels(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    result: dict[str, dict[str, Any]] = {}
    for entry in _label_entries(document):
        case_id, normalized = _label_entry(entry)
        if case_id in result:
            raise ValueError(f"duplicate label {case_id!r}")
        result[case_id] = normalized
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


_REQUIRED_CAPTURE_FIELDS = (
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


def _require_case_fields(row: dict[str, Any]) -> None:
    missing = [key for key in _REQUIRED_CAPTURE_FIELDS if key not in row]
    if missing:
        raise ValueError(f"{row.get('case_id', '<unknown>')}: capture missing {', '.join(missing)}")


def _case_hashes(row: dict[str, Any]) -> dict[str, Any]:
    return {
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


def _capture_disposition(row: dict[str, Any]) -> dict[str, Any]:
    return row.get(
        "disposition",
        {
            "status": row.get("assessment", {}).get("status", "unknown"),
            "reason": row.get("assessment", {}).get("reason", ""),
        },
    )


def _final_capture(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": "final",
        "question_wire": row["question_wire"],
        "evidence": row["evidence"],
        "answer": row["answer"],
        "disposition": _capture_disposition(row),
        "context_complete": row["context_complete"],
        "target_complete": row["target_complete"],
        "returned_model": row["returned_model"],
        "assessment": row.get("assessment", {}),
        "initial": row.get("initial"),
        "review": row.get("review", {}),
    }


def _adjudication(row: dict[str, Any], label: dict[str, Any]) -> tuple[str, Any]:
    label_name = label.get("label")
    if label_name not in {"Agree", "Partial", "Disagree"}:
        raise ValueError(f"{row['case_id']}: label must be Agree, Partial, or Disagree")
    severity = label.get("adjudicated_severity")
    if label_name == "Disagree" and severity is not None:
        raise ValueError(f"{row['case_id']}: Disagree cannot carry severity")
    return label_name, severity


def _provenance(row: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
    groups = {key: label[key] for key in ("source_group", "scenario_group") if key in label}
    return {
        "source": "final-scan-adjudication",
        "capture_phase": "final",
        "assessment": row.get("assessment", {}),
        "disposition": row.get("disposition", {}),
        "review": row.get("review", {}),
        "inference": row.get("inference", {}),
        "cached": row.get("cached", False),
        "fully_cached": row.get("fully_cached", False),
        **groups,
    }


def _comparability(row: dict[str, Any], hashes: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": hashes["question"],
        "evidence": hashes["evidence"],
        "prompt": {"version": row["prompt"]["version"], "identity": hashes["prompt"]},
        "endpoint": row["endpoint"],
        "model": row["returned_model"],
    }


def _case_document(row: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
    _require_case_fields(row)
    hashes = _case_hashes(row)
    label_name, severity = _adjudication(row, label)
    return {
        "version": 1,
        "case_id": row["case_id"],
        "rule_id": row["rule_id"],
        "rule": row["rule"],
        "target": row["target"],
        "answer": row["answer"],
        "context_complete": row["context_complete"],
        "target_complete": row["target_complete"],
        "question_wire": row["question_wire"],
        "capture": _final_capture(row),
        "split": label.get("split", "final"),
        "label": label_name,
        "adjudicated_severity": severity,
        "explanation": label.get("explanation", "External final-judgment adjudication"),
        "provenance": _provenance(row, label),
        "evidence": row["evidence"],
        "prompt": row["prompt"],
        "endpoint": row["endpoint"],
        "requested_model": row["requested_model"],
        "returned_model": row["returned_model"],
        "hashes": hashes,
        "comparability": _comparability(row, hashes),
    }


def _case(row: dict[str, Any], label: dict[str, Any]) -> CalibrationCase:
    return CalibrationCase.model_validate(_case_document(row, label))

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


def _partition_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    skips = [row for row in rows if row.get("kind") in {"applicability", "omitted", "skip"}]
    judgments = [row for row in rows if row.get("kind", "judgment") == "judgment"]
    return judgments, skips


def _write_cases(path: Path, cases: list[CalibrationCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for case in sorted(cases, key=lambda item: item.case_id):
            stream.write(json.dumps(case.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n")


def _write_skips(path: Path, skips: list[dict[str, Any]]) -> None:
    if not skips:
        return
    path.with_suffix(".skips.json").write_text(
        json.dumps(skips, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _import_cases(args: argparse.Namespace) -> None:
    _reject_output_collisions(args.capture, args.labels, args.output)
    metadata = _capture_metadata(args.capture)
    _require_complete(metadata, args.allow_incomplete)
    rows = _read_jsonl(args.capture)
    _verify_capture_metadata(args.capture, metadata, rows)
    judgments, skips = _partition_rows(rows)
    labels = _labels(args.labels)
    _validate_label_ids({row.get("case_id") for row in judgments}, set(labels))
    cases = [_case(row, labels[row["case_id"]]) for row in judgments]
    _write_cases(args.output, cases)
    _write_skips(args.output, skips)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _import_cases(args)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"jevscan-calibration-import: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
