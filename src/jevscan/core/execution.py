"""Bounded request recovery, with intact targets and shared compacted evidence.

A singleton rejection establishes failure only for that exact request. A per-file
state memo is a scheduling hint, not a tokenizer proof. Final complete-target
attempts ignore the hint; exact rejected bodies are never sent twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from jevscan.core.compaction import Compactor
from jevscan.core.context import Evidence
from jevscan.core.inference import Inference
from jevscan.core.planning import Omission, Planner, Request
from jevscan.core.protocol import Check, ContextLimitError, RequestRejectedError

if TYPE_CHECKING:
    from jevscan.core.evaluation import FileResults


@dataclass(frozen=True, slots=True)
class Attempt:
    request: Request
    round: int = 0
    final: bool = False


class FileExecutor:
    def __init__(self, planner: Planner, inference: Inference, results: FileResults) -> None:
        self.planner, self.inference, self.results = planner, inference, results
        self.compactor: Compactor | None = None
        self.rejected: dict[str, tuple[int, dict[str, Any]]] = {}
        self.rejected_requests: set[str] = set()

    async def _without_cached_judgments(self, request: Request) -> Request | None:
        cached = await self.inference.cached_judgments(
            request.state,
            request.question_wires,
            request.questions,
        )
        missing: list[Check] = []
        for check in request.checks:
            item = cached.get(check.id)
            if item is None:
                missing.append(check)
                continue
            answer, model = item
            self.results.accept_answer(
                check,
                request.evidence,
                answer,
                model,
                True,
                wire_state=request.state,
                question_wire=json.loads(request.question_wires[check.id]),
            )
            trace = self.results.records[check.target.id].context_selection.get(check.rule_id)
            if trace:
                trace["outcome"] = "judgment_cache"
        if not missing:
            return None
        return (
            request if len(missing) == len(request.checks) else self.planner.request(request.evidence, tuple(missing))
        )

    def _record_size_rejection(self, request: Request, error: ContextLimitError) -> None:
        for check in request.checks:
            self.results.recovery(check)["rejections"].append({
                **error.metadata(),
                "request_bytes": len(request.body),
                "state_sha256": hashlib.sha256(request.state).hexdigest(),
                "questions": len(request.checks),
            })
        if len(request.checks) != 1:
            return
        length = len(request.question_wires[request.checks[0].id])
        state_key = hashlib.sha256(request.state).hexdigest()
        old = self.rejected.get(state_key)
        if old is None or length < old[0]:
            self.rejected[state_key] = length, error.metadata()

    def _mark_accepted(self, attempt: Attempt, request: Request) -> None:
        for check in request.checks:
            trace = self.results.records[check.target.id].context_selection.get(check.rule_id)
            if trace:
                trace["outcome"] = (
                    "complete_target_fallback" if attempt.final else "compacted" if attempt.round else "questions_split"
                )

    async def _send(self, attempt: Attempt) -> Literal["accepted", "size_rejected", "request_rejected"]:
        request = attempt.request
        digest = hashlib.sha256(request.body).hexdigest()
        if digest in self.rejected_requests:
            return "size_rejected"
        try:
            prediction = await self.inference.predict(
                request.body,
                request.questions,
                state_bytes=len(request.state),
                question_bytes=sum(len(request.question_wires[check.id]) for check in request.checks),
                state=json.loads(request.state),
            )
        except ContextLimitError as exc:
            self.rejected_requests.add(digest)
            self._record_size_rejection(request, exc)
            return "size_rejected"
        except RequestRejectedError as exc:
            self.results.reject(request, exc)
            self.inference.summary.request_rejections += 1
            return "request_rejected"

        self._mark_accepted(attempt, request)
        response = prediction.response
        await self.inference.store_judgments(
            request.state,
            request.question_wires,
            request.questions,
            response,
        )
        self.results.accept(
            request,
            response.answers,
            response.model,
            prediction.cached,
            {**prediction.metrics, "phase": "initial"},
        )
        return "accepted"

    def _split(self, attempt: Attempt) -> list[Attempt]:
        request = attempt.request
        # Probe a short singleton before retrying sibling batches with the same large state.
        probe = min(request.checks, key=lambda check: len(request.question_wires[check.id]))
        remaining = tuple(check for check in request.checks if check.id != probe.id)
        midpoint = (len(remaining) + 1) // 2
        batches = [(probe,), remaining[:midpoint], remaining[midpoint:]]
        return [Attempt(self.planner.request(request.evidence, batch), attempt.round) for batch in batches if batch]

    def _split_preflight(self, attempt: Attempt) -> list[Attempt]:
        checks = attempt.request.checks
        midpoint = len(checks) // 2
        return [
            Attempt(self.planner.request(attempt.request.evidence, batch), attempt.round)
            for batch in (checks[:midpoint], checks[midpoint:])
            if batch
        ]

    def _pack(self, evidence: Evidence, checks: list[Check], round_number: int) -> list[Attempt]:
        """Preserve shared-state batching when multiple rules choose identical compacted evidence."""
        attempts = []
        pending: tuple[Check, ...] = ()
        for check in checks:
            if pending and not self.planner.fits(evidence, (*pending, check)):
                attempts.append(Attempt(self.planner.request(evidence, pending), round_number))
                pending = ()
            pending = (*pending, check)
        if pending:
            attempts.append(Attempt(self.planner.request(evidence, pending), round_number))
        return attempts

    async def _recover(self, attempt: Attempt, reason: str) -> list[Attempt]:
        request = attempt.request
        groups: dict[str, tuple[Evidence, list[Check]]] = {}
        final_attempts = []
        for check in request.checks:
            trace = self.results.recovery(check)
            trace.setdefault("trigger", reason)
            if attempt.final:
                trace["outcome"] = "omitted"
                self.results.omit(
                    Omission(check, f"{reason}: complete target/context cannot be evaluated within bounded recovery")
                )
                continue
            previous_bytes = len(self.planner.request(request.evidence, (check,)).body)
            trace.setdefault("initial_request_bytes", previous_bytes)
            trace.setdefault("initial_token_estimates", self.planner.estimate(request.evidence, (check,))[:2])
            evidence = None
            if attempt.round < self.planner.compaction.max_rounds:
                if self.compactor is None:
                    self.compactor = Compactor(self.planner, self.inference)
                evidence = await self.compactor.compact(check, previous_bytes, attempt.round, trace)
            if evidence is None:
                target = check.target
                bare = (
                    request.evidence
                    if self.planner.limits.oversized_context == "skip"
                    else self.planner.context.envelope(target.start_byte, target.end_byte)
                )
                final_attempts.append(Attempt(self.planner.request(bare, (check,)), attempt.round, final=True))
                continue
            assert len(self.planner.request(evidence, (check,)).body) < previous_bytes
            groups.setdefault(evidence.key, (evidence, []))[1].append(check)
        return [
            item for evidence, checks in groups.values() for item in self._pack(evidence, checks, attempt.round + 1)
        ] + final_attempts

    async def _preflight(self, attempt: Attempt) -> list[Attempt] | None:
        request = attempt.request
        state_key = hashlib.sha256(request.state).hexdigest()
        known = self.rejected.get(state_key)
        blocked = (
            not attempt.final
            and known is not None
            and all(len(request.question_wires[check.id]) >= known[0] for check in request.checks)
        )
        if blocked:
            assert known is not None
            for check in request.checks:
                self.results.recovery(check)["known_rejection"] = known[1]
            return await self._recover(attempt, "related_request_rejection")

        violations = self.planner.violations(request.evidence, request.checks)
        if not violations:
            return None
        if "context" not in violations and len(request.checks) > 1:
            return self._split_preflight(attempt)
        reason = "model_context_preflight" if "context" in violations else "request_limit_preflight"
        return await self._recover(attempt, reason)

    async def _process(self, queue: asyncio.Queue[Attempt], attempt: Attempt) -> None:
        request = await self._without_cached_judgments(attempt.request)
        if request is None:
            return
        attempt = Attempt(request, attempt.round, attempt.final)
        recovery = await self._preflight(attempt)
        if recovery is not None:
            for item in recovery:
                queue.put_nowait(item)
            return
        if await self._send(attempt) != "size_rejected":
            return
        request = attempt.request
        recovered = (
            self._split(attempt) if len(request.checks) > 1 else await self._recover(attempt, "provider_context_limit")
        )
        for item in recovered:
            queue.put_nowait(item)

    async def _worker(self, queue: asyncio.Queue[Attempt]) -> None:
        while True:
            attempt = await queue.get()
            try:
                await self._process(queue, attempt)
            finally:
                queue.task_done()

    async def run(self) -> None:
        initial = [Attempt(request) for request in self.planner.plan()]
        if not initial:
            return
        queue: asyncio.Queue[Attempt] = asyncio.Queue()
        for attempt in initial:
            queue.put_nowait(attempt)

        # Recovery can fan one oversized request back into many independent requests,
        # so worker capacity must not be capped by the number of initial batches.
        count = self.inference.client.config.concurrency
        tasks = [asyncio.create_task(self._worker(queue)) for _ in range(count)]
        joined = asyncio.create_task(queue.join())
        try:
            done, _ = await asyncio.wait([joined, *tasks], return_when=asyncio.FIRST_COMPLETED)
            if joined in done:
                await joined
                for task in tasks:
                    if task.done() and not task.cancelled() and (error := task.exception()) is not None:
                        raise error
                return
            failed = next(task for task in done if task is not joined)
            error = failed.exception()
            if error is None:
                raise RuntimeError("request worker stopped before its queue was drained")
            raise error
        finally:
            joined.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(joined, *tasks, return_exceptions=True)
