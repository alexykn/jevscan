"""Interpret raw answers once; keep severity separate from reasons for uncertainty."""

from dataclasses import dataclass
from typing import Literal

from jevscan.core.models import Finding, Severity
from jevscan.core.protocol import Answer, Check, ChoiceAnswer, NoulAnswer, ScoreAnswer
from jevscan.core.rules import ReportPolicy, ReportThreshold

Status = Literal["ok", "warning", "error", "unknown", "not_applicable"]


@dataclass(frozen=True, slots=True)
class Assessment:
    status: Status
    reason: str = ""
    finding: Finding | None = None
    tentative_finding: Finding | None = None


@dataclass(frozen=True, slots=True)
class Reading:
    value: str | float
    probability: float | None
    confidence: float | None
    eligible: bool = True


def _noul_reading(report: ReportPolicy, answer: NoulAnswer, _level: ReportThreshold | None) -> Reading:
    probability = answer.noul if report.expected else 1 - answer.noul
    return Reading(answer.noul, probability, None)


def _choice_reading(report: ReportPolicy, answer: ChoiceAnswer, _level: ReportThreshold | None) -> Reading:
    assert report.choices is not None
    return Reading(
        answer.choice,
        answer.probabilities[answer.choice],
        answer.confidence,
        answer.choice in report.choices,
    )


def _score_reading(_report: ReportPolicy, answer: ScoreAnswer, level: ReportThreshold | None) -> Reading:
    probability = None
    if level is not None and level.score_levels is not None:
        probability = sum(answer.probabilities[str(score)] for score in level.score_levels)
    return Reading(answer.score, probability, answer.confidence)


_READING_BUILDERS = {
    NoulAnswer: _noul_reading,
    ChoiceAnswer: _choice_reading,
    ScoreAnswer: _score_reading,
}


def _reading(report: ReportPolicy, answer: Answer, level: ReportThreshold | None = None) -> Reading:
    return _READING_BUILDERS[type(answer)](report, answer, level)


def _probability_signal(level: ReportThreshold, reading: Reading) -> bool:
    assert level.min_probability is not None and reading.probability is not None
    return reading.probability >= level.min_probability


def _score_signal(level: ReportThreshold, reading: Reading) -> bool:
    assert isinstance(reading.value, float)
    if level.min_score is not None:
        return reading.value >= level.min_score
    assert level.max_score is not None
    return reading.value <= level.max_score


def _matches_signal(level: ReportThreshold, reading: Reading) -> bool:
    """Severity measures the indicated problem, not confidence in the indication."""
    if level.score_levels is not None or level.min_probability is not None:
        return _probability_signal(level, reading)
    return _score_signal(level, reading)

def _choice_uncertainty(report: ReportPolicy, reading: Reading) -> str:
    minimum = report.levels.warning.min_probability
    assert minimum is not None and reading.probability is not None
    if reading.probability < minimum:
        return "low_choice_probability"
    return "weak_defect_signal" if reading.eligible else ""


def _uncertainty(report: ReportPolicy, reading: Reading, answer: Answer) -> str:
    minimum = report.levels.warning.min_confidence
    if minimum is not None and reading.confidence is not None and reading.confidence < minimum:
        return "low_confidence"
    return _choice_uncertainty(report, reading) if isinstance(answer, ChoiceAnswer) else ""


def _probability_ambiguous(report: ReportPolicy, answer: Answer) -> bool:
    if not isinstance(answer, NoulAnswer) or report.uncertain_range is None:
        return False
    lower, upper = report.uncertain_range
    return lower <= answer.noul < upper


def _at_level(check: Check, reading: Reading, severity: Severity, reason: str) -> Assessment:
    finding = Finding(
        check.rule_id,
        severity,
        check.rule.report.message,
        check.target,
        reading.value,
        reading.probability,
        reading.confidence,
    )
    if reason:
        return Assessment("unknown", reason, tentative_finding=finding)
    return Assessment("error" if severity == Severity.ERROR else "warning", finding=finding)


def _level_reason(level: ReportThreshold, reading: Reading, ambiguity: str) -> str:
    minimum = level.min_confidence
    low_confidence = minimum is not None and (reading.confidence is None or reading.confidence < minimum)
    return "low_confidence" if low_confidence else ambiguity


def _indicated_assessment(check: Check, answer: Answer, ambiguity: str) -> Assessment | None:
    report = check.rule.report
    levels = ((Severity.ERROR, report.levels.error), (Severity.WARNING, report.levels.warning))
    for severity, level in levels:
        reading = _reading(report, answer, level)
        if reading.eligible and _matches_signal(level, reading):
            # The strongest indicated level wins. Low confidence does not turn an error into a warning.
            return _at_level(check, reading, severity, _level_reason(level, reading, ambiguity))
    return None


def _nonfinding_status(
    report: ReportPolicy,
    answer: Answer,
    reason: str,
    context_complete: bool,
) -> Assessment:
    if reason:
        return Assessment("unknown", reason)
    if not context_complete:
        return Assessment("unknown", "reduced_context")
    not_applicable = isinstance(answer, ChoiceAnswer) and answer.choice in report.not_applicable_choices
    return Assessment("not_applicable", "rule_not_applicable") if not_applicable else Assessment("ok")


def _nonfinding_assessment(
    check: Check,
    answer: Answer,
    ambiguity: str,
    context_complete: bool,
) -> Assessment:
    report = check.rule.report
    reading = _reading(report, answer, report.levels.warning)
    reason = ambiguity or _uncertainty(report, reading, answer)
    return _nonfinding_status(report, answer, reason, context_complete)


def assess(check: Check, answer: Answer, context_complete: bool = True) -> Assessment:
    report = check.rule.report
    if isinstance(answer, ChoiceAnswer) and answer.choice in report.uncertain_choices:
        return Assessment("unknown", "missing_evidence")

    ambiguity = "probability_ambiguous" if _probability_ambiguous(report, answer) else ""
    indicated = _indicated_assessment(check, answer, ambiguity)
    if indicated is not None:
        return indicated
    return _nonfinding_assessment(check, answer, ambiguity, context_complete)
