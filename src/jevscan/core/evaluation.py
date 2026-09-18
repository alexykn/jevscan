"""Execute file-local plans, reclassify cached answers, and retain target attribution."""

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from jevscan.core.cache import AnswerCache, cache_key
from jevscan.core.client import JevClient
from jevscan.core.models import Diagnostic, EventSink, Finding, Severity, Summary, Target, emit_diagnostic
from jevscan.core.planning import Omission, Planner, Request
from jevscan.core.protocol import Answer, Check, ChoiceAnswer, ContextLimitError, NoulAnswer, validate_response
from jevscan.core.rules import ReportThreshold, ScoreQuestion


def _matches(level: ReportThreshold, value: float | str, probability: float | None, confidence: float | None) -> bool:
    if level.min_confidence is not None and (confidence is None or confidence < level.min_confidence):
        return False
    if level.min_probability is not None:
        assert probability is not None
        return probability >= level.min_probability
    assert isinstance(value, float)
    if level.min_score is not None:
        return value >= level.min_score
    assert level.max_score is not None
    return value <= level.max_score


def assess(check: Check, answer: Answer, context_complete: bool = True) -> tuple[str, Finding | None]:
    report = check.rule.report
    probability, confidence = None, None
    eligible = True
    if isinstance(answer, NoulAnswer):
        value: str | float = answer.noul
        probability = answer.noul if report.expected else 1 - answer.noul
    elif isinstance(answer, ChoiceAnswer):
        value, probability, confidence = answer.choice, answer.probabilities[answer.choice], answer.confidence
        if answer.choice in report.uncertain_choices:
            return "unknown", None
        assert report.choices is not None
        eligible = answer.choice in report.choices
    else:
        value, confidence = answer.score, answer.confidence
    for severity, level in ((Severity.ERROR, report.levels.error), (Severity.WARNING, report.levels.warning)):
        if eligible and _matches(level, value, probability, confidence):
            return str(severity), Finding(
                check.rule_id, severity, report.message, check.target, value, probability, confidence
            )
    minimum = report.levels.warning.min_confidence
    uncertain = minimum is not None and confidence is not None and confidence < minimum
    if isinstance(answer, ChoiceAnswer):
        assert report.levels.warning.min_probability is not None and probability is not None
        uncertain |= eligible or probability < report.levels.warning.min_probability
    return ("unknown" if uncertain or not context_complete else "ok"), None


@dataclass(frozen=True, slots=True)
class Judgment:
    check: Check
    answer: Answer
    evidence: dict[str, Any]
    model: str
    cached: bool


@dataclass(slots=True)
class TargetResults:
    target: Target
    judgments: dict[str, Judgment] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def event(self) -> dict[str, Any]:
        statuses, findings = {}, []
        for name, result in sorted(self.judgments.items()):
            status, finding = assess(result.check, result.answer, result.evidence["context_complete"])
            statuses[name] = status
            if finding:
                findings.append(asdict(finding))
        return {
            "event": "evaluation",
            "target": self.target.metadata(),
            "answers": {name: item.answer.model_dump(mode="json") for name, item in sorted(self.judgments.items())},
            "statuses": statuses,
            "findings": findings,
            "evidence": {name: item.evidence for name, item in self.judgments.items()},
            "models": {name: item.model for name, item in self.judgments.items()},
            "cached_rules": [name for name, item in self.judgments.items() if item.cached],
            "cached": bool(self.judgments)
            and not self.skipped
            and all(item.cached for item in self.judgments.values()),
            "skipped_rules": self.skipped,
            "scales": {
                name: len(item.check.rule.question.criteria) - 1
                for name, item in self.judgments.items()
                if isinstance(item.check.rule.question, ScoreQuestion)
            },
        }

    def diagnostics(self, sink: EventSink, summary: Summary, aborted: bool) -> None:
        reduced = [name for name, result in self.judgments.items() if not result.evidence["context_complete"]]
        summary.context_reduced += len(reduced)
        if reduced:
            emit_diagnostic(
                sink,
                summary,
                Diagnostic(
                    self.target.path,
                    "context-reduced",
                    f"{self.target.qualified_name}: reduced surrounding evidence for {', '.join(reduced)}; target is complete",
                    line=self.target.start_line,
                ),
            )
        if self.skipped:
            summary.incomplete = True
            if not aborted:
                detail = "; ".join(dict.fromkeys(self.skipped.values()))
                emit_diagnostic(
                    sink,
                    summary,
                    Diagnostic(
                        self.target.path,
                        "evaluation-size-limit",
                        f"{self.target.qualified_name}: {detail} ({', '.join(self.skipped)})",
                        line=self.target.start_line,
                    ),
                )

    def emit(self, sink: EventSink, summary: Summary, aborted: bool) -> None:
        event = self.event()
        summary.checks_evaluated += len(self.judgments)
        summary.checks_skipped += len(self.skipped)
        summary.uncertain += sum(status == "unknown" for status in event["statuses"].values())
        if self.target.scope == "unit":
            summary.units_evaluated += bool(self.judgments)
            summary.units_cached += event["cached"]
            summary.units_skipped += not self.judgments and not aborted
            summary.units_failed += bool(self.skipped) and aborted
        else:
            summary.file_targets_evaluated += bool(self.judgments)
            summary.file_targets_skipped += not self.judgments
        for finding in event["findings"]:
            summary.findings[finding["severity"]] += 1
        self.diagnostics(sink, summary, aborted)
        sink.emit(event)


class FileResults:
    """Own the answers for one file; serialize only when emitting a target report."""

    def __init__(self, planner: Planner) -> None:
        self.planner = planner
        self.records: dict[str, TargetResults] = {}
        for check in planner.checks:
            if check.target.id not in self.records:
                self.records[check.target.id] = TargetResults(check.target)

    def omit(self, omission: Omission) -> None:
        check = omission.check
        self.records[check.target.id].skipped[check.rule_id] = omission.reason

    def accept(self, request: Request, answers: dict[str, Answer], model: str, cached: bool) -> None:
        for check in request.checks:
            record = self.records[check.target.id]
            assert check.rule_id not in record.judgments
            evidence = self.planner.context.describe(check, request.evidence)
            record.judgments[check.rule_id] = Judgment(check, answers[check.id], evidence, model, cached)

    def finish(self, sink: EventSink, summary: Summary, aborted: bool) -> None:
        for check in self.planner.checks:
            record = self.records[check.target.id]
            if check.rule_id not in record.judgments and check.rule_id not in record.skipped:
                assert aborted, "every planned check must have an answer or explicit omission"
                record.skipped[check.rule_id] = "scan aborted before an answer was received"
        for record in self.records.values():
            record.emit(sink, summary, aborted)


async def _execute(
    request: Request, client: JevClient, cache: AnswerCache | None, summary: Summary, results: FileResults
) -> None:
    key = cache_key(client.base_url, request.body)
    raw = await cache.get(key) if cache else None
    if raw is not None:
        response = validate_response(raw, request.questions)
        summary.cache_hits += 1
    else:
        response = await client.evaluate(request.body, request.questions)
        summary.input_tokens += response.usage.input_tokens or 0
        summary.output_tokens += response.usage.output_tokens or 0
        if cache:
            await cache.put(key, response.model_dump_json().encode())
    results.accept(request, response.answers, response.model, raw is not None)


async def evaluate_file(
    planner: Planner, client: JevClient, cache: AnswerCache | None, sink: EventSink, summary: Summary
) -> None:
    results = FileResults(planner)
    finished = False
    try:
        for planned in planner.plan():
            pending = deque([planned])
            while pending:
                item = pending.popleft()
                if isinstance(item, Omission):
                    results.omit(item)
                    continue
                try:
                    await _execute(item, client, cache, summary, results)
                except ContextLimitError:
                    pending.extendleft(reversed(planner.recover(item)))
        finished = True
    finally:
        results.finish(sink, summary, aborted=not finished)
