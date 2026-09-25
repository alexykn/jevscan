"""Projection and accounting for one target's completed judgments."""

from dataclasses import asdict, dataclass, field
from typing import Any

from jevscan.core.assessment import Assessment, assess
from jevscan.core.context import Evidence
from jevscan.core.models import Diagnostic, EventSink, Summary, Target, emit_diagnostic
from jevscan.core.protocol import Answer, Check
from jevscan.core.rules import ScoreQuestion


def _review_predictions_cached(review: dict[str, Any]) -> bool:
    return all(
        prediction.get("cached") is True
        for prediction in review.get("predictions", ())
    )


@dataclass(frozen=True, slots=True)
class Judgment:
    check: Check
    answer: Answer
    evidence: dict[str, Any]
    model: str
    cached: bool
    context: Evidence
    inference: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    wire_state: bytes = b""
    question_wire: dict[str, Any] = field(default_factory=dict)

    @property
    def fully_cached(self) -> bool:
        if not self.cached:
            return False
        if not self.review:
            return True
        return bool(self.review["initial_cached"] and _review_predictions_cached(self.review))

    def assessment(self) -> Assessment:
        if self.review.get("outcome") == "not_applicable":
            return Assessment("not_applicable", "model_routed_not_applicable")
        return assess(self.check, self.answer, self.evidence["context_complete"])


@dataclass(frozen=True, slots=True)
class AssessmentProjection:
    statuses: dict[str, str]
    reasons: dict[str, str]
    findings: list[dict[str, Any]]
    tentative: list[dict[str, Any]]

    @classmethod
    def from_judgments(cls, judgments: dict[str, Judgment]) -> "AssessmentProjection":
        statuses: dict[str, str] = {}
        reasons: dict[str, str] = {}
        findings: list[dict[str, Any]] = []
        tentative: list[dict[str, Any]] = []
        for name, result in sorted(judgments.items()):
            decision = result.assessment()
            statuses[name] = decision.status
            reasons[name] = decision.reason
            if decision.finding is not None:
                findings.append(asdict(decision.finding))
            if decision.tentative_finding is not None:
                tentative.append(asdict(decision.tentative_finding))
        return cls(statuses, reasons, findings, tentative)


def _rule_metadata(judgments: dict[str, Judgment]) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "title": item.check.rule.title,
            "ruleset": item.check.rule.ruleset,
            "blocks_exit": item.check.rule.report.blocks_exit,
        }
        for name, item in judgments.items()
    }


def _scales(judgments: dict[str, Judgment]) -> dict[str, int]:
    return {
        name: len(item.check.rule.question.criteria) - 1
        for name, item in judgments.items()
        if isinstance(item.check.rule.question, ScoreQuestion)
    }


def _target_cached(judgments: dict[str, Judgment], skipped: dict[str, str]) -> bool:
    return bool(judgments) and not skipped and all(item.fully_cached for item in judgments.values())


@dataclass(slots=True)
class TargetResults:
    target: Target
    judgments: dict[str, Judgment] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    applicability: dict[str, str] = field(default_factory=dict)
    context_selection: dict[str, dict[str, Any]] = field(default_factory=dict)

    def event(self) -> dict[str, Any]:
        projection = AssessmentProjection.from_judgments(self.judgments)
        return {
            "event": "evaluation",
            "target": self.target.metadata(),
            "answers": {name: item.answer.model_dump(mode="json") for name, item in sorted(self.judgments.items())},
            "rule_metadata": _rule_metadata(self.judgments),
            "statuses": projection.statuses,
            "uncertainty_reasons": projection.reasons,
            "reviews": {name: item.review for name, item in self.judgments.items() if item.review},
            "findings": projection.findings,
            "tentative_findings": projection.tentative,
            "evidence": {name: item.evidence for name, item in self.judgments.items()},
            "models": {name: item.model for name, item in self.judgments.items()},
            "inference": {name: item.inference for name, item in self.judgments.items()},
            "cached_rules": [name for name, item in self.judgments.items() if item.fully_cached],
            "cached": _target_cached(self.judgments, self.skipped),
            "skipped_rules": self.skipped,
            "applicability_skips": self.applicability,
            "context_selection": self.context_selection,
            "scales": _scales(self.judgments),
        }

    def diagnostics(self, sink: EventSink, summary: Summary, aborted: bool) -> None:
        reduced = _reduced_rules(self.judgments)
        summary.context_reduced += len(reduced)
        _emit_reduced_diagnostic(self.target, reduced, sink, summary)
        _emit_skipped_diagnostic(self, sink, summary, aborted)

    def emit(self, sink: EventSink, summary: Summary, aborted: bool) -> None:
        event = self.event()
        _account_checks(self, event, summary)
        _account_target(self, event, summary, aborted)
        _account_findings(self, event, summary)
        self.diagnostics(sink, summary, aborted)
        sink.emit(event)


def _reduced_rules(judgments: dict[str, Judgment]) -> list[str]:
    return [name for name, result in judgments.items() if not result.evidence["context_complete"]]


def _emit_reduced_diagnostic(
    target: Target,
    reduced: list[str],
    sink: EventSink,
    summary: Summary,
) -> None:
    if not reduced:
        return
    emit_diagnostic(
        sink,
        summary,
        Diagnostic(
            target.path,
            "context-reduced",
            f"{target.qualified_name}: reduced surrounding evidence for {', '.join(reduced)}; target is complete",
            line=target.start_line,
            incomplete=False,
        ),
    )


def _emit_skipped_diagnostic(
    results: TargetResults,
    sink: EventSink,
    summary: Summary,
    aborted: bool,
) -> None:
    if not results.skipped:
        return
    summary.incomplete = True
    file_limit = all(reason.startswith("full-file context is ") for reason in results.skipped.values())
    if aborted or file_limit:
        return
    detail = "; ".join(dict.fromkeys(results.skipped.values()))
    emit_diagnostic(
        sink,
        summary,
        Diagnostic(
            results.target.path,
            "evaluation-size-limit",
            f"{results.target.qualified_name}: {detail} ({', '.join(results.skipped)})",
            line=results.target.start_line,
        ),
    )


def _account_checks(results: TargetResults, event: dict[str, Any], summary: Summary) -> None:
    summary.checks_evaluated += len(results.judgments)
    summary.checks_skipped += len(results.skipped)
    summary.applicability_skips += len(results.applicability)
    summary.uncertain += sum(status == "unknown" for status in event["statuses"].values())
    summary.not_applicable += len(results.applicability) + sum(
        status == "not_applicable" for status in event["statuses"].values()
    )


def _account_target(
    results: TargetResults,
    event: dict[str, Any],
    summary: Summary,
    aborted: bool,
) -> None:
    has_judgments = bool(results.judgments)
    has_skips = bool(results.skipped)
    if results.target.scope == "file":
        summary.file_targets_evaluated += has_judgments
        summary.file_targets_skipped += has_skips and not has_judgments
        return
    summary.units_evaluated += has_judgments
    summary.units_cached += event["cached"]
    summary.units_skipped += has_skips and not has_judgments and not aborted
    summary.units_failed += has_skips and aborted


def _account_findings(results: TargetResults, event: dict[str, Any], summary: Summary) -> None:
    for finding in event["findings"]:
        severity = finding["severity"]
        summary.findings[severity] += 1
        if not results.judgments[finding["rule"]].check.rule.report.blocks_exit:
            summary.advisory_findings[severity] += 1
    for finding in event["tentative_findings"]:
        summary.tentative_findings[finding["severity"]] += 1
