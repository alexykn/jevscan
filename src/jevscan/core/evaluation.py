"""Execute file-local plans, reclassify cached answers, and retain target attribution."""

import hashlib
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from itertools import islice
from typing import Any

from jevscan.core.assessment import Assessment, assess
from jevscan.core.cache import AnswerCache
from jevscan.core.client import JevClient
from jevscan.core.compaction import Compactor
from jevscan.core.context import Evidence
from jevscan.core.enrichment import REVIEW_PRIORITY, Enricher, review_trigger
from jevscan.core.inference import Inference
from jevscan.core.models import Diagnostic, EventSink, Summary, Target, emit_diagnostic
from jevscan.core.planning import Omission, Planner, Preparation, Request
from jevscan.core.protocol import Answer, Check, ContextLimitError
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
    review: dict[str, Any] = field(default_factory=dict)

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
    preparation: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _cached(self, name: str, result: Judgment) -> bool:
        return result.fully_cached and all(
            trace.get("reason") != "provider_rejection"
            and not trace.get("adapted_after_rejection", False)
            and all(prediction.get("cached", False) for prediction in trace.get("predictions", []))
            for trace in self.preparation.get(name, [])
        )

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
                name: {"title": item.check.rule.title, "ruleset": item.check.rule.ruleset}
                for name, item in self.judgments.items()
            },
            "statuses": statuses,
            "uncertainty_reasons": reasons,
            "preparation": self.preparation,
            "reviews": {name: item.review for name, item in self.judgments.items() if item.review},
            "findings": findings,
            "tentative_findings": tentative,
            "evidence": {name: item.evidence for name, item in self.judgments.items()},
            "models": {name: item.model for name, item in self.judgments.items()},
            "cached_rules": [name for name, item in self.judgments.items() if self._cached(name, item)],
            "cached": bool(self.judgments)
            and not self.skipped
            and all(self._cached(name, item) for name, item in self.judgments.items()),
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
        summary.not_applicable += sum(status == "not_applicable" for status in event["statuses"].values())
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
        for finding in event["tentative_findings"]:
            summary.tentative_findings[finding["severity"]] += 1
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
            record.judgments[check.rule_id] = Judgment(
                check, answers[check.id], evidence, model, cached, request.evidence
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

    async def enrich(self, enricher: Enricher) -> None:
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
                    initial.review,
                )
            final = record.judgments[name].assessment()
            initial.review["final_status"] = final.status
            enricher.inference.summary.enrichment_resolved += final.status != "unknown"

    def finish(self, sink: EventSink, summary: Summary, aborted: bool) -> None:
        for check in self.planner.checks:
            record = self.records[check.target.id]
            if check.rule_id not in record.judgments and check.rule_id not in record.skipped:
                assert aborted, "every planned check must have an answer or explicit omission"
                record.skipped[check.rule_id] = "scan aborted before an answer was received"
        self.emit_coverage(sink)
        for record in self.records.values():
            record.emit(sink, summary, aborted)

    def emit_coverage(self, sink: EventSink) -> None:
        affected = [
            record
            for record in self.records.values()
            if record.skipped or any(not result.evidence["context_complete"] for result in record.judgments.values())
        ]
        if affected:
            sink.emit({
                "event": "coverage",
                "path": self.planner.context.parsed.path,
                "reduced_targets": sum(
                    any(not j.evidence["context_complete"] for j in record.judgments.values()) for record in affected
                ),
                "reduced_checks": sum(
                    not j.evidence["context_complete"] for record in affected for j in record.judgments.values()
                ),
                "skipped_targets": sum(bool(record.skipped) for record in affected),
                "skipped_checks": sum(len(record.skipped) for record in affected),
            })


class FileExecutor:
    """Run full requests, then pack prepared evidence in bounded batches; own retries here."""

    def __init__(self, planner: Planner, inference: Inference, results: FileResults) -> None:
        self.planner, self.inference, self.results = planner, inference, results
        self.compactor = Compactor(planner, inference)
        self.rejected_states: set[str] = set()
        self.pending: list[Preparation] = []

    def _rejected(self, request: Request, exc: ContextLimitError) -> None:
        if len(request.checks) == 1 and request.compaction_round == 0:
            self.rejected_states.add(request.evidence.key)
        for check in request.checks:
            self.results.records[check.target.id].preparation.setdefault(check.rule_id, []).append({
                "reason": "provider_rejection",
                "request_sha256": hashlib.sha256(request.body).hexdigest(),
                **exc.metadata(),
                "question_count": len(request.checks),
            })

    async def _execute(self, items: Iterable[Request | Preparation | Omission]) -> None:
        for planned in items:
            recovery = deque([planned])
            while recovery:
                item = recovery.popleft()
                if isinstance(item, Omission):
                    self.results.omit(item)
                    continue
                if isinstance(item, Preparation):
                    self.pending.append(item)
                    continue
                if (
                    item.compaction_round == 0
                    and item.evidence.key in self.rejected_states
                    and all(check.target.scope == "unit" for check in item.checks)
                    and self.planner.limits.oversized_context == "reduce"
                ):
                    recovery.extendleft(reversed(self.planner.adapt_rejected_state(item)))
                    continue
                try:
                    prediction = await self.inference.predict(item.body, item.questions)
                    response = prediction.response
                    self.results.accept(item, response.answers, response.model, prediction.cached)
                except ContextLimitError as exc:
                    self._rejected(item, exc)
                    recovery.extendleft(reversed(self.planner.recover(item)))

    async def _prepare(self, batch: list[Preparation]) -> list[Request | Omission]:
        groups: dict[tuple[str, int], tuple[Evidence, list[Check]]] = {}
        result: list[Request | Omission] = []
        for task in batch:
            trace: dict[str, Any] = {}
            self.results.records[task.check.target.id].preparation.setdefault(task.check.rule_id, []).append(trace)
            item = await self.compactor.prepare(task, trace)
            if isinstance(item, Omission):
                result.append(item)
                continue
            key = item.evidence.key, item.compaction_round
            if key not in groups:
                groups[key] = item.evidence, []
            groups[key][1].extend(item.checks)
        for (_, level), (evidence, checks) in groups.items():
            result.extend(self.planner.pack(evidence, checks, level))
        return result

    async def run(self) -> None:
        await self._execute(self.planner.plan())
        while self.pending:
            tasks, self.pending = iter(self.pending), []
            # At most 64 prepared request bodies per active file; no repository-sized collection.
            while batch := list(islice(tasks, 64)):
                await self._execute(await self._prepare(batch))


async def evaluate_file(
    planner: Planner,
    client: JevClient,
    cache: AnswerCache | None,
    sink: EventSink,
    summary: Summary,
    index: SourceIndex | None = None,
) -> None:
    results = FileResults(planner)
    inference = Inference(client, cache, summary)
    finished = False
    try:
        await FileExecutor(planner, inference, results).run()
        if index is not None:
            await results.enrich(Enricher(planner.context, planner.budget, index.limits, index, inference))
        finished = True
    finally:
        results.finish(sink, summary, aborted=not finished)
