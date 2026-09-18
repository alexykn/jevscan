"""Interpret raw answers once; keep severity separate from reasons for uncertainty."""

from dataclasses import dataclass
from typing import Literal

from jevscan.core.models import Finding, Severity
from jevscan.core.protocol import Answer, Check, ChoiceAnswer, NoulAnswer
from jevscan.core.rules import ReportPolicy, ReportThreshold

Status = Literal["ok", "warning", "error", "unknown", "not_applicable"]


@dataclass(frozen=True, slots=True)
class Assessment:
    status: Status
    reason: str = ""
    finding: Finding | None = None


@dataclass(frozen=True, slots=True)
class Reading:
    value: str | float
    probability: float | None
    confidence: float | None
    eligible: bool = True


def _reading(report: ReportPolicy, answer: Answer) -> Reading:
    if isinstance(answer, NoulAnswer):
        probability = answer.noul if report.expected else 1 - answer.noul
        return Reading(answer.noul, probability, None)
    if isinstance(answer, ChoiceAnswer):
        assert report.choices is not None
        return Reading(
            answer.choice, answer.probabilities[answer.choice], answer.confidence, answer.choice in report.choices
        )
    return Reading(answer.score, None, answer.confidence)


def _matches(level: ReportThreshold, reading: Reading) -> bool:
    if level.min_confidence is not None and (reading.confidence is None or reading.confidence < level.min_confidence):
        return False
    if level.min_probability is not None:
        assert reading.probability is not None
        return reading.probability >= level.min_probability
    assert isinstance(reading.value, float)
    if level.min_score is not None:
        return reading.value >= level.min_score
    assert level.max_score is not None
    return reading.value <= level.max_score


def _uncertainty(report: ReportPolicy, reading: Reading, answer: Answer) -> str:
    minimum = report.levels.warning.min_confidence
    if minimum is not None and reading.confidence is not None and reading.confidence < minimum:
        return "low_confidence"
    if isinstance(answer, ChoiceAnswer):
        assert report.levels.warning.min_probability is not None and reading.probability is not None
        if reading.probability < report.levels.warning.min_probability:
            return "low_choice_probability"
        if reading.eligible:
            return "weak_defect_signal"
    return ""


def assess(check: Check, answer: Answer, context_complete: bool = True) -> Assessment:
    report = check.rule.report
    reading = _reading(report, answer)
    if isinstance(answer, ChoiceAnswer) and answer.choice in report.uncertain_choices:
        return Assessment("unknown", "missing_evidence")
    if isinstance(answer, NoulAnswer) and report.uncertain_range is not None:
        lower, upper = report.uncertain_range
        if lower <= answer.noul < upper:
            return Assessment("unknown", "probability_ambiguous")
    for severity, level in ((Severity.ERROR, report.levels.error), (Severity.WARNING, report.levels.warning)):
        if reading.eligible and _matches(level, reading):
            finding = Finding(
                check.rule_id,
                severity,
                report.message,
                check.target,
                reading.value,
                reading.probability,
                reading.confidence,
            )
            return Assessment("error" if severity == Severity.ERROR else "warning", finding=finding)
    reason = _uncertainty(report, reading, answer)
    if reason or not context_complete:
        return Assessment("unknown", reason or "reduced_context")
    if isinstance(answer, ChoiceAnswer) and answer.choice in report.not_applicable_choices:
        return Assessment("not_applicable", "rule_not_applicable")
    return Assessment("ok")
