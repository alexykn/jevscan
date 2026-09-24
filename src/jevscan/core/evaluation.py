"""Execute file-local plans, reclassify cached answers, and retain target attribution."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from jevscan.core.assessment import Assessment, assess
from jevscan.core.cache import AnswerCache
from jevscan.core.capture import FinalJudgmentSink
from jevscan.core.client import JevClient
from jevscan.core.context import Evidence
from jevscan.core.enrichment import REVIEW_PRIORITY, Enricher, review_trigger
from jevscan.core.execution import FileExecutor
from jevscan.core.inference import Inference
from jevscan.core.models import Diagnostic, EventSink, Severity, Summary, Target, emit_diagnostic
from jevscan.core.planning import Omission, Planner, Request
from jevscan.core.protocol import Answer, Check, RequestRejectedError
from jevscan.core.retrieval import SourceIndex
from jevscan.core.rules import ScoreQuestion


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
        if not self.review:
            return self.cached
        return (
            self.cached
            and self.review["initial_cached"]
            and all("cached" in item and item["cached"] for item in self.review["predictions"])
        )

    def assessment(self) -> Assessment:
        if self.review.get("outcome") == "not_applicable":
            return Assessment("not_applicable", "model_routed_not_applicable")
        return assess(self.check, self.answer, self.evidence["context_complete"])


@dataclass(slots=True)
class TargetResults:
    target: Target
    judgments: dict[str, Judgment] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    applicability: dict[str, str] = field(default_factory=dict)
    context_selection: dict[str, dict[str, Any]] = field(default_factory=dict)

    def event(self) -> dict[str, Any]:
        statuses, reasons, findings, tentative = {}, {}, [], []
        for name, result in sorted(self.judgments.items()):
            decision = result.assessment()
            statuses[name], reasons[name] = decision.status, decision.reason
            if decision.finding:
                findings.append(asdict(decision.finding))
            if decision.tentative_finding:
                tentative.append(asdict(decision.tentative_finding))
        return {
            "event": "evaluation",
            "target": self.target.metadata(),
            "answers": {name: item.answer.model_dump(mode="json") for name, item in sorted(self.judgments.items())},
            "rule_metadata": {
                name: {
                    "title": item.check.rule.title,
                    "ruleset": item.check.rule.ruleset,
                    "blocks_exit": item.check.rule.report.blocks_exit,
                }
                for name, item in self.judgments.items()
            },
            "statuses": statuses,
            "uncertainty_reasons": reasons,
            "reviews": {name: item.review for name, item in self.judgments.items() if item.review},
            "findings": findings,
            "tentative_findings": tentative,
            "evidence": {name: item.evidence for name, item in self.judgments.items()},
            "models": {name: item.model for name, item in self.judgments.items()},
            "inference": {name: item.inference for name, item in self.judgments.items()},
            "cached_rules": [name for name, item in self.judgments.items() if item.fully_cached],
            "cached": bool(self.judgments)
            and not self.skipped
            and all(item.fully_cached for item in self.judgments.values()),
            "skipped_rules": self.skipped,
            "applicability_skips": self.applicability,
            "context_selection": self.context_selection,
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
            file_limit = all(reason.startswith("full-file context is ") for reason in self.skipped.values())
            if not aborted and not file_limit:
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
        summary.applicability_skips += len(self.applicability)
        summary.uncertain += sum(status == "unknown" for status in event["statuses"].values())
        summary.not_applicable += len(self.applicability) + sum(
            status == "not_applicable" for status in event["statuses"].values()
        )
        if self.target.scope == "unit":
            summary.units_evaluated += bool(self.judgments)
            summary.units_cached += event["cached"]
            summary.units_skipped += bool(self.skipped) and not self.judgments and not aborted
            summary.units_failed += bool(self.skipped) and aborted
        else:
            summary.file_targets_evaluated += bool(self.judgments)
            summary.file_targets_skipped += bool(self.skipped) and not self.judgments
        for finding in event["findings"]:
            summary.findings[finding["severity"]] += 1
            if not self.judgments[finding["rule"]].check.rule.report.blocks_exit:
                summary.advisory_findings[finding["severity"]] += 1
        for finding in event["tentative_findings"]:
            summary.tentative_findings[finding["severity"]] += 1
        self.diagnostics(sink, summary, aborted)
        sink.emit(event)


class FileResults:
    """Own the answers for one file; serialize only when emitting a target report."""

    def __init__(self, planner: Planner) -> None:
        self.planner = planner
        self.records: dict[str, TargetResults] = {}
        self._capture_reviews: dict[tuple[str, str], dict[str, Any]] = {}
        self.request_failures: dict[tuple[object, ...], dict[str, Any]] = {}
        self.request_rejected_checks = 0
        all_checks = [*planner.checks, *(omission.check for omission in planner.omissions)]
        active_ids = {check.target.id for check in all_checks} | set(planner.applicability_skips)
        for target in planner.targets:
            if target.id in active_ids:
                self.records[target.id] = TargetResults(target)
        for omission in planner.omissions:
            self.omit(omission)
        for target_id, skipped in planner.applicability_skips.items():
            for rule_id, reason in skipped.items():
                self.records[target_id].applicability[rule_id] = reason

    def recovery(self, check: Check) -> dict[str, Any]:
        return self.records[check.target.id].context_selection.setdefault(
            check.rule_id,
            {
                "rejections": [],
                "compactions": [],
                "outcome": "pending",
            },
        )

    def omit(self, omission: Omission) -> None:
        check = omission.check
        self.records[check.target.id].skipped[check.rule_id] = omission.reason

    def reject(self, request: Request, error: RequestRejectedError) -> None:
        metadata = error.metadata()
        entry = self.request_failures.setdefault(error.signature, {**metadata, "requests": 0, "checks": 0})
        entry["requests"] += 1
        entry["checks"] += len(request.checks)
        self.request_rejected_checks += len(request.checks)
        for check in request.checks:
            trace = self.recovery(check)
            trace["request_rejections"] = [
                *trace.get("request_rejections", []),
                {
                    **metadata,
                    "request_bytes": len(request.body),
                    "state_sha256": hashlib.sha256(request.state).hexdigest(),
                    "questions": len(request.checks),
                },
            ]
            trace["outcome"] = "provider_request_rejected"
            self.omit(Omission(check, f"provider request rejected (HTTP {error.status})"))

    def accept_answer(
        self,
        check: Check,
        evidence_state: Evidence,
        answer: Answer,
        model: str,
        cached: bool,
        inference: dict[str, Any] | None = None,
        *,
        shared_request: bool = False,
        wire_state: bytes = b"",
        question_wire: dict[str, Any] | None = None,
    ) -> None:
        record = self.records[check.target.id]
        assert check.rule_id not in record.judgments
        evidence = self.planner.context.describe(check, evidence_state)
        trace = record.context_selection.get(check.rule_id, {})
        fully_cached = cached and all(
            prediction.get("cached", False)
            for step in trace.get("compactions", [])
            for prediction in step["predictions"]
        )
        request_metrics = dict(inference or {})
        phase = request_metrics.pop("phase", "initial")
        metrics: dict[str, Any] = {
            "phase": phase,
            "question_id": check.id,
            "question_bytes": len(self.planner.question_wires[check.id]),
            "shared_request": shared_request,
            "cached": cached,
        }
        if request_metrics:
            metrics["request"] = request_metrics
        record.judgments[check.rule_id] = Judgment(
            check,
            answer,
            evidence,
            model,
            fully_cached,
            evidence_state,
            metrics,
            {},
            wire_state,
            question_wire or check.question(),
        )

    def accept(
        self,
        request: Request,
        answers: dict[str, Answer],
        model: str,
        cached: bool,
        inference: dict[str, Any] | None = None,
    ) -> None:
        for check in request.checks:
            self.accept_answer(
                check,
                request.evidence,
                answers[check.id],
                model,
                cached,
                inference,
                shared_request=len(request.checks) > 1,
                wire_state=request.state,
                question_wire=json.loads(request.question_wires[check.id]),
            )

    def _review_queue(self) -> list[tuple[int, str, Judgment]]:
        pending = []
        for record in self.records.values():
            for initial in record.judgments.values():
                trigger = review_trigger(initial.check, initial.assessment(), initial.evidence["context_complete"])
                if trigger is not None:
                    pending.append((REVIEW_PRIORITY[trigger], trigger, initial))
        # Schedule across the whole file before consuming either budget. Emission stays in source order.
        return sorted(pending, key=lambda item: (item[0], item[2].check.target.start_byte, item[2].check.rule_id))

    async def enrich(self, enricher: Enricher, capture: FinalJudgmentSink | None = None) -> None:
        for _, trigger, initial in self._review_queue():
            record = self.records[initial.check.target.id]
            name = initial.check.rule_id
            initial.review.update({
                "trigger": trigger,
                "initial_reason": initial.assessment().reason,
                "initial_model": initial.model,
                "initial_cached": initial.cached,
                "initial_evidence": initial.evidence,
            })
            if capture is not None:
                self._capture_reviews[(initial.check.target.id, name)] = {
                    "initial_evidence_state": json.loads(initial.wire_state)
                    if initial.wire_state
                    else initial.context.state,
                    "initial_question_wire": initial.question_wire or initial.check.question(),
                }
            result = await enricher.refine(initial.check, initial.answer, initial.context, initial.review)
            if result is not None:
                response = result.prediction.response
                record.judgments[name] = Judgment(
                    initial.check,
                    response.answers[initial.check.id],
                    self.planner.context.describe(initial.check, result.evidence),
                    response.model,
                    result.prediction.cached,
                    result.evidence,
                    {
                        "phase": "reassess",
                        "question_id": initial.check.id,
                        "question_bytes": len(self.planner.question_wires[initial.check.id]),
                        "shared_request": False,
                        "cached": result.prediction.cached,
                        "request": result.prediction.metrics,
                    },
                    initial.review,
                    result.wire_state,
                    result.question_wire,
                )
            final = record.judgments[name].assessment()
            initial.review["final_status"] = final.status
            enricher.inference.summary.enrichment_resolved += final.status != "unknown"

    def capture_final(self, sink: FinalJudgmentSink, source_documents: dict[str, str]) -> None:
        for record in self.records.values():
            for rule_id, reason in record.applicability.items():
                sink.record_skip(record.target, rule_id, reason, "applicability")
            for rule_id, reason in record.skipped.items():
                sink.record_skip(record.target, rule_id, reason, "omitted")
            for judgment in record.judgments.values():
                review = {
                    **self._capture_reviews.get((record.target.id, judgment.check.rule_id), {}),
                    **judgment.review,
                }
                sink.record(judgment, judgment.assessment(), source_documents, review)

    def _finalize_missing(self, aborted: bool) -> None:
        for check in [*self.planner.checks, *(item.check for item in self.planner.omissions)]:
            record = self.records[check.target.id]
            if check.rule_id in record.judgments or check.rule_id in record.skipped:
                continue
            assert aborted, "every planned check must have an answer or explicit omission"
            record.skipped[check.rule_id] = "scan aborted before an answer was received"

    def _emit_request_failures(self, sink: EventSink, summary: Summary) -> None:
        for failure in self.request_failures.values():
            fields = (
                ", ".join(f"{key}={','.join(values)}" for key, values in failure["machine_fields"].items())
                or "no machine fields"
            )
            emit_diagnostic(
                sink,
                summary,
                Diagnostic(
                    self.planner.context.parsed.path,
                    "provider-request-rejected",
                    f"Jev HTTP {failure['status']} rejected {failure['checks']} checks "
                    f"across {failure['requests']} request(s); {fields}; request ID: "
                    f"{failure['request_id'] or 'unavailable'}",
                    severity=Severity.ERROR,
                ),
            )

    def _emit_coverage(self, sink: EventSink, aborted: bool) -> None:
        reduced_targets = sum(
            any(not item.evidence["context_complete"] for item in record.judgments.values())
            for record in self.records.values()
        )
        skipped = sum(len(record.skipped) for record in self.records.values())
        if not reduced_targets and not skipped:
            return
        file_skipped = sum(len(record.skipped) for record in self.records.values() if record.target.scope == "file")
        sink.emit({
            "event": "coverage",
            "path": self.planner.context.parsed.path,
            "context_reduced_targets": reduced_targets,
            "skipped_checks": skipped,
            "skipped_file_checks": file_skipped,
            "request_rejected_checks": self.request_rejected_checks,
            "aborted": aborted,
        })

    def finish(self, sink: EventSink, summary: Summary, aborted: bool) -> None:
        self._finalize_missing(aborted)
        self._emit_request_failures(sink, summary)
        self._emit_coverage(sink, aborted)
        for record in self.records.values():
            if aborted:
                for trace in record.context_selection.values():
                    if trace["outcome"] == "pending":
                        trace["outcome"] = "aborted"
            record.emit(sink, summary, aborted)


async def evaluate_file(
    planner: Planner,
    client: JevClient,
    cache: AnswerCache | None,
    sink: EventSink,
    summary: Summary,
    index: SourceIndex | None = None,
    capture: FinalJudgmentSink | None = None,
) -> None:
    results = FileResults(planner)
    inference = Inference(client, cache, summary, planner.budget.calibration)
    finished = False
    enricher: Enricher | None = None
    try:
        await FileExecutor(planner, inference, results).run()
        if index is not None:
            enricher = Enricher(planner.context, planner.budget, index.limits, index, inference, planner.rubric)
            await results.enrich(enricher, capture)
        if capture is not None:
            source_documents = (
                enricher.source_documents()
                if enricher is not None
                else {planner.context.parsed.path: planner.context.parsed.source.decode("utf-8")}
            )
            results.capture_final(capture, source_documents)
        finished = True
    finally:
        results.finish(sink, summary, aborted=not finished)
