"""Execute file-local plans, reclassify cached answers, and retain target attribution."""

import hashlib
import json
from typing import Any

from jevscan.core.cache import AnswerCache
from jevscan.core.capture import FinalJudgmentSink
from jevscan.core.client import JevClient
from jevscan.core.context import Evidence
from jevscan.core.enrichment import Enricher
from jevscan.core.enrichment_routing import REVIEW_PRIORITY, review_trigger
from jevscan.core.execution import FileExecutor
from jevscan.core.inference import Inference
from jevscan.core.models import Diagnostic, EventSink, Severity, Summary, emit_diagnostic
from jevscan.core.planning import Omission, Planner, Request
from jevscan.core.protocol import Answer, Check, RequestRejectedError
from jevscan.core.result_reporting import Judgment, TargetResults
from jevscan.core.retrieval import SourceIndex


def _active_target_ids(planner: Planner) -> set[str]:
    checks = [*planner.checks, *(omission.check for omission in planner.omissions)]
    return {check.target.id for check in checks} | set(planner.applicability_skips)


def _initial_records(planner: Planner) -> dict[str, TargetResults]:
    active = _active_target_ids(planner)
    return {target.id: TargetResults(target) for target in planner.targets if target.id in active}


def _judgment_cached(cached: bool, trace: dict[str, Any]) -> bool:
    predictions = (prediction for step in trace.get("compactions", ()) for prediction in step["predictions"])
    return cached and all(prediction.get("cached", False) for prediction in predictions)


def _inference_metrics(
    planner: Planner,
    check: Check,
    cached: bool,
    shared_request: bool,
    inference: dict[str, Any] | None,
) -> dict[str, Any]:
    request_metrics = dict(inference or {})
    phase = request_metrics.pop("phase", "initial")
    metrics: dict[str, Any] = {
        "phase": phase,
        "question_id": check.id,
        "question_bytes": len(planner.question_wires[check.id]),
        "shared_request": shared_request,
        "cached": cached,
    }
    if request_metrics:
        metrics["request"] = request_metrics
    return metrics


class FileResults:
    """Own the answers for one file; serialize only when emitting a target report."""

    def __init__(self, planner: Planner) -> None:
        self.planner = planner
        self.records = _initial_records(planner)
        self._capture_reviews: dict[tuple[str, str], dict[str, Any]] = {}
        self.request_failures: dict[tuple[object, ...], dict[str, Any]] = {}
        self.request_rejected_checks = 0
        self._seed_omissions(planner.omissions)
        self._seed_applicability(planner.applicability_skips)

    def _seed_omissions(self, omissions: tuple[Omission, ...]) -> None:
        for omission in omissions:
            self.omit(omission)

    def _seed_applicability(self, skipped: dict[str, dict[str, str]]) -> None:
        for target_id, rules in skipped.items():
            self.records[target_id].applicability.update(rules)

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
        record.judgments[check.rule_id] = Judgment(
            check,
            answer,
            evidence,
            model,
            _judgment_cached(cached, trace),
            evidence_state,
            _inference_metrics(self.planner, check, cached, shared_request, inference),
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
            enricher = Enricher(planner.context, planner.budget, index.limits, index, inference)
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
