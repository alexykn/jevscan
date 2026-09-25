"""Replay validated calibration cases through production assessment and report metrics."""

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, TextIO

from jevscan.core.assessment import Assessment, assess
from jevscan.core.calibration_cases import (
    CALIBRATION_VERSION,
    CalibrationCase,
    CalibrationLabel,
    FinalCaptureMaterial,
    IdentityHashes,
    TargetRecord,
    load_cases,
    sha256_bytes,
)
from jevscan.core.models import Target
from jevscan.core.protocol import Check, encode
from jevscan.core.rules import ReportPolicy, Rule

__all__ = [
    "CalibrationCase",
    "CalibrationLabel",
    "CalibrationReport",
    "FinalCaptureMaterial",
    "ReplayRecord",
    "TargetRecord",
    "load_cases",
    "replay_case",
    "replay_cases",
    "write_report",
]


@dataclass(frozen=True, slots=True)
class Fraction:
    count: int
    denominator: int

    @property
    def value(self) -> float | None:
        return self.count / self.denominator if self.denominator else None

    def as_dict(self) -> dict[str, int | float | None]:
        return {"count": self.count, "denominator": self.denominator, "fraction": self.value}


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    record_index: int
    case: CalibrationCase
    assessment: Assessment
    effective_rule: Rule
    effective_hashes: IdentityHashes
    comparable: bool = True
    mismatches: tuple[dict[str, Any], ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.assessment.status in {"warning", "error"}

    @property
    def signal(self) -> bool:
        return self.confirmed or self.assessment.tentative_finding is not None

    @property
    def reason(self) -> str:
        return ", ".join(str(item["field"]) for item in self.mismatches)


@dataclass(frozen=True, slots=True)
class RuleSplitReport:
    rule_id: str
    split: str
    total: int
    label_counts: dict[str, int]
    strict_supported_warning: Fraction
    confirmed_recall: Fraction
    review_list_recall: Fraction
    disagree_confirmed: Fraction
    partial_confirmed: Fraction
    partial_any_signal: Fraction
    confirmed_severity_agreement: Fraction
    review_list_severity_agreement: Fraction
    severity_confusion: dict[str, dict[str, int]]
    severity_counts: dict[str, int]
    tentative_severity_counts: dict[str, int]
    non_comparable: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "split": self.split,
            "total": self.total,
            "label_counts": self.label_counts,
            "strict_supported_warning": self.strict_supported_warning.as_dict(),
            "confirmed_recall": self.confirmed_recall.as_dict(),
            "review_list_recall": self.review_list_recall.as_dict(),
            "disagree_confirmed": self.disagree_confirmed.as_dict(),
            "partial_confirmed": self.partial_confirmed.as_dict(),
            "partial_any_signal": self.partial_any_signal.as_dict(),
            "confirmed_severity_agreement": self.confirmed_severity_agreement.as_dict(),
            "review_list_severity_agreement": self.review_list_severity_agreement.as_dict(),
            "severity_confusion": self.severity_confusion,
            "severity_counts": self.severity_counts,
            "tentative_severity_counts": self.tentative_severity_counts,
            "non_comparable": self.non_comparable,
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    records: tuple[ReplayRecord, ...]
    reports: dict[str, dict[str, RuleSplitReport]]
    non_comparable: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        ordered_records = sorted(
            self.records,
            key=lambda record: (record.case.case_id, record.case.rule_id, record.case.split, record.record_index),
        )
        return {
            "version": CALIBRATION_VERSION,
            "cases": len(self.records),
            "records": [_record_as_dict(record) for record in ordered_records],
            "non_comparable": list(self.non_comparable),
            "rules": {
                rule_id: {split: report.as_dict() for split, report in splits.items()}
                for rule_id, splits in sorted(self.reports.items())
            },
        }


def _report_override(rule: Rule, override: ReportPolicy | Mapping[str, Any] | None) -> Rule:
    if override is None:
        return rule
    policy = override if isinstance(override, ReportPolicy) else ReportPolicy.model_validate(override)
    return Rule.model_validate({**rule.model_dump(mode="python"), "report": policy.model_dump(mode="python")})


def _effective_hashes(case: CalibrationCase, rule: Rule) -> IdentityHashes:
    values = case.hashes.model_dump(mode="python")
    values["rule"] = sha256_bytes(encode(rule.model_dump(mode="json")))
    values["report"] = sha256_bytes(encode(rule.report.model_dump(mode="json")))
    return IdentityHashes.model_validate(values)


def replay_case(
    case: CalibrationCase,
    report_override: ReportPolicy | Mapping[str, Any] | None = None,
    *,
    record_index: int = 0,
) -> ReplayRecord:
    """Replay one case through the production Check and assess paths."""
    rule = _report_override(case.rule, report_override)
    check = Check(case.case_id, case.target, case.rule_id, rule)
    assessment = assess(check, case.answer, case.context_complete)
    if (
        case.capture is not None
        and case.capture.disposition.status == "not_applicable"
        and case.capture.disposition.reason == "model_routed_not_applicable"
    ):
        assessment = Assessment("not_applicable", case.capture.disposition.reason)
    return ReplayRecord(
        record_index,
        case,
        assessment,
        rule,
        _effective_hashes(case, rule),
    )


def _comparability_value(record: ReplayRecord, field: str) -> Any:
    return record.case.comparability.model_dump(mode="json")[field]


def _value_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_COMPARABILITY_FIELDS = ("question", "evidence", "prompt", "endpoint", "model")


def _records_by_case(records: list[ReplayRecord]) -> dict[str, list[ReplayRecord]]:
    grouped: dict[str, list[ReplayRecord]] = defaultdict(list)
    for record in records:
        grouped[record.case.case_id].append(record)
    return grouped


def _field_conflict(group: list[ReplayRecord], field: str) -> dict[str, Any] | None:
    values = [_comparability_value(record, field) for record in group]
    if len({_value_key(value) for value in values}) <= 1:
        return None
    return {
        "field": field,
        "records": [
            {
                "record_index": record.record_index,
                "case_id": record.case.case_id,
                "rule_id": record.case.rule_id,
                "split": record.case.split,
                "value": _comparability_value(record, field),
            }
            for record in group
        ],
    }


def _case_conflicts(group: list[ReplayRecord]) -> tuple[dict[str, Any], ...]:
    return tuple(conflict for field in _COMPARABILITY_FIELDS if (conflict := _field_conflict(group, field)) is not None)


def _mark_case_group(
    records: list[ReplayRecord],
    case_id: str,
    conflicts: tuple[dict[str, Any], ...],
) -> list[ReplayRecord]:
    return [
        ReplayRecord(
            record.record_index,
            record.case,
            record.assessment,
            record.effective_rule,
            record.effective_hashes,
            record.comparable and record.case.case_id != case_id,
            conflicts if record.case.case_id == case_id else record.mismatches,
        )
        for record in records
    ]


def _conflict_notice(case_id: str, conflicts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "fields": [item["field"] for item in conflicts],
        "conflicts": list(conflicts),
    }


def _mark_non_comparable(
    records: list[ReplayRecord],
) -> tuple[list[ReplayRecord], tuple[dict[str, Any], ...]]:
    marked = list(records)
    notices: list[dict[str, Any]] = []
    for case_id, group in sorted(_records_by_case(records).items()):
        conflicts = _case_conflicts(group)
        if not conflicts:
            continue
        notices.append(_conflict_notice(case_id, conflicts))
        marked = _mark_case_group(marked, case_id, conflicts)
    return marked, tuple(notices)


def _count(records: Iterable[ReplayRecord], predicate: Callable[[ReplayRecord], bool]) -> int:
    return sum(predicate(record) for record in records)


def _fraction(
    records: Iterable[ReplayRecord],
    numerator: Callable[[ReplayRecord], bool],
    denominator: Callable[[ReplayRecord], bool],
) -> Fraction:
    values = list(records)
    return Fraction(_count(values, numerator), _count(values, denominator))


def _label_count(records: list[ReplayRecord], label: str) -> int:
    return _count(records, lambda record: record.case.label == label)


def _status_count(records: list[ReplayRecord], status: str) -> int:
    return _count(records, lambda record: record.assessment.status == status)


def _tentative_count(records: list[ReplayRecord], severity: str) -> int:
    return _count(
        records,
        lambda record: (
            record.assessment.tentative_finding is not None
            and str(record.assessment.tentative_finding.severity) == severity
        ),
    )


SEVERITY_OUTCOMES = (
    "none",
    "confirmed_warning",
    "confirmed_error",
    "tentative_warning",
    "tentative_error",
)


def _observed_severity(record: ReplayRecord, *, include_tentative: bool) -> str | None:
    if record.assessment.finding is not None:
        severity = str(record.assessment.finding.severity)
        return severity if severity in {"warning", "error"} else None
    if include_tentative and record.assessment.tentative_finding is not None:
        severity = str(record.assessment.tentative_finding.severity)
        return severity if severity in {"warning", "error"} else None
    return None


def _severity_agreement(record: ReplayRecord, *, include_tentative: bool) -> bool:
    expected = record.case.adjudicated_severity
    return expected is not None and _observed_severity(record, include_tentative=include_tentative) == expected


def _severity_outcome(record: ReplayRecord) -> str:
    if record.assessment.finding is not None:
        severity = str(record.assessment.finding.severity)
        if severity in {"warning", "error"}:
            return f"confirmed_{severity}"
    if record.assessment.tentative_finding is not None:
        severity = str(record.assessment.tentative_finding.severity)
        if severity in {"warning", "error"}:
            return f"tentative_{severity}"
    return "none"


def _severity_confusion(records: list[ReplayRecord]) -> dict[str, dict[str, int]]:
    return {
        adjudicated: {
            outcome: _count(
                records,
                lambda record, adjudicated=adjudicated, outcome=outcome: (
                    record.case.adjudicated_severity == adjudicated and _severity_outcome(record) == outcome
                ),
            )
            for outcome in SEVERITY_OUTCOMES
        }
        for adjudicated in ("warning", "error")
    }


def _label_fractions(records: list[ReplayRecord]) -> dict[str, Fraction]:
    return {
        "strict_supported_warning": _fraction(
            records,
            lambda record: record.case.label == "Agree" and record.confirmed,
            lambda record: record.confirmed,
        ),
        "confirmed_recall": _fraction(
            records,
            lambda record: record.case.label == "Agree" and record.confirmed,
            lambda record: record.case.label == "Agree",
        ),
        "review_list_recall": _fraction(
            records,
            lambda record: record.case.label == "Agree" and record.signal,
            lambda record: record.case.label == "Agree",
        ),
        "disagree_confirmed": _fraction(
            records,
            lambda record: record.case.label == "Disagree" and record.confirmed,
            lambda record: record.case.label == "Disagree",
        ),
        "partial_confirmed": _fraction(
            records,
            lambda record: record.case.label == "Partial" and record.confirmed,
            lambda record: record.case.label == "Partial",
        ),
        "partial_any_signal": _fraction(
            records,
            lambda record: record.case.label == "Partial" and record.signal,
            lambda record: record.case.label == "Partial",
        ),
    }


def _severity_fractions(records: list[ReplayRecord]) -> tuple[Fraction, Fraction]:
    has_adjudicated_severity = lambda record: record.case.adjudicated_severity is not None
    return (
        _fraction(
            records,
            lambda record: _severity_agreement(record, include_tentative=False),
            has_adjudicated_severity,
        ),
        _fraction(
            records,
            lambda record: _severity_agreement(record, include_tentative=True),
            has_adjudicated_severity,
        ),
    )


def _severity_counts(records: list[ReplayRecord]) -> tuple[dict[str, int], dict[str, int]]:
    confirmed = {
        status: _status_count(records, status) for status in ("ok", "warning", "error", "unknown", "not_applicable")
    }
    tentative = {severity: _tentative_count(records, severity) for severity in ("warning", "error")}
    return confirmed, tentative


def _make_report(rule_id: str, split: str, records: list[ReplayRecord]) -> RuleSplitReport:
    usable = [record for record in records if record.comparable]
    fractions = _label_fractions(usable)
    confirmed_agreement, review_agreement = _severity_fractions(usable)
    severity_counts, tentative_counts = _severity_counts(usable)
    return RuleSplitReport(
        rule_id=rule_id,
        split=split,
        total=len(usable),
        label_counts={label: _label_count(usable, label) for label in ("Agree", "Partial", "Disagree")},
        strict_supported_warning=fractions["strict_supported_warning"],
        confirmed_recall=fractions["confirmed_recall"],
        review_list_recall=fractions["review_list_recall"],
        disagree_confirmed=fractions["disagree_confirmed"],
        partial_confirmed=fractions["partial_confirmed"],
        partial_any_signal=fractions["partial_any_signal"],
        confirmed_severity_agreement=confirmed_agreement,
        review_list_severity_agreement=review_agreement,
        severity_confusion=_severity_confusion(usable),
        severity_counts=severity_counts,
        tentative_severity_counts=tentative_counts,
        non_comparable=len(records) - len(usable),
    )


def replay_cases(
    cases: Iterable[CalibrationCase],
    report_overrides: Mapping[str, ReportPolicy | Mapping[str, Any]] | None = None,
) -> CalibrationReport:
    """Replay cases and aggregate exact count/denominator metrics by rule and split."""
    records = [
        replay_case(
            case,
            report_overrides.get(case.rule_id) if report_overrides else None,
            record_index=index,
        )
        for index, case in enumerate(cases)
    ]
    marked, notices = _mark_non_comparable(records)
    grouped: dict[tuple[str, str], list[ReplayRecord]] = defaultdict(list)
    for record in marked:
        grouped[(record.case.rule_id, record.case.split)].append(record)
    reports: dict[str, dict[str, RuleSplitReport]] = defaultdict(dict)
    for (rule_id, split), group in sorted(grouped.items()):
        reports[rule_id][split] = _make_report(rule_id, split, group)
    return CalibrationReport(tuple(marked), dict(reports), notices)


def _target_as_dict(target: Target) -> dict[str, Any]:
    return {
        "id": target.id,
        "scope": target.scope,
        "path": target.path,
        "language": target.language,
        "qualified_name": target.qualified_name,
        "start_byte": target.start_byte,
        "end_byte": target.end_byte,
        "start_line": target.start_line,
        "end_line": target.end_line,
        "kind": str(target.kind) if target.kind is not None else None,
        "display_name": target.display_name,
    }


def _finding_as_dict(finding: Any) -> dict[str, Any] | None:
    if finding is None:
        return None
    return {
        "rule": finding.rule,
        "severity": str(finding.severity),
        "message": finding.message,
        "target": _target_as_dict(finding.target),
        "value": finding.value,
        "probability": finding.probability,
        "confidence": finding.confidence,
    }


def _record_as_dict(record: ReplayRecord) -> dict[str, Any]:
    case = record.case
    finding = _finding_as_dict(record.assessment.finding)
    tentative = _finding_as_dict(record.assessment.tentative_finding)
    outcome = {
        "status": record.assessment.status,
        "reason": record.assessment.reason,
        "finding": finding,
        "tentative_finding": tentative,
    }
    return {
        "record_index": record.record_index,
        "case_id": case.case_id,
        "rule_id": case.rule_id,
        "split": case.split,
        "label": case.label,
        "adjudicated_severity": case.adjudicated_severity,
        "explanation": case.explanation,
        "provenance": case.provenance,
        "rule": case.rule.model_dump(mode="json"),
        "effective_rule": record.effective_rule.model_dump(mode="json"),
        "target": _target_as_dict(case.target),
        "answer": case.answer.model_dump(mode="json"),
        "context_complete": case.context_complete,
        "target_complete": case.target_complete,
        "evidence": case.evidence.model_dump(mode="json"),
        "prompt": case.prompt.model_dump(mode="json"),
        "endpoint": case.endpoint,
        "requested_model": case.requested_model,
        "returned_model": case.returned_model,
        "hashes": case.hashes.model_dump(mode="json"),
        "comparability": case.comparability.model_dump(mode="json"),
        "effective_hashes": record.effective_hashes.model_dump(mode="json"),
        "comparable": record.comparable,
        "comparability_mismatches": list(record.mismatches),
        "status": record.assessment.status,
        "reason": record.assessment.reason,
        "finding": finding,
        "tentative_finding": tentative,
        "outcome": outcome,
    }


def write_report(report: CalibrationReport, destination: TextIO) -> None:
    json.dump(report.as_dict(), destination, ensure_ascii=False, sort_keys=True, indent=2)
    destination.write("\n")
