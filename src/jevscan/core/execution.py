"""Bounded request recovery, with intact targets and shared compacted evidence.

A singleton rejection establishes failure only for that exact request. A per-file
state memo is a scheduling hint, not a tokenizer proof. Final complete-target
attempts ignore the hint; exact rejected bodies are never sent twice.
"""

from __future__ import annotations

import hashlib
from collections import deque
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

    async def _send(self, attempt: Attempt) -> Literal["accepted", "size_rejected", "request_rejected"]:
        request = attempt.request
        digest = hashlib.sha256(request.body).hexdigest()
        if digest in self.rejected_requests:
            return "size_rejected"
        try:
            prediction = await self.inference.predict(request.body, request.questions)
        except ContextLimitError as exc:
            self.rejected_requests.add(digest)
            for check in request.checks:
                self.results.recovery(check)["rejections"].append({
                    **exc.metadata(),
                    "request_bytes": len(request.body),
                    "state_sha256": request.evidence.key,
                    "questions": len(request.checks),
                })
            if len(request.checks) == 1:
                length = len(self.planner.questions[request.checks[0].id])
                old = self.rejected.get(request.evidence.key)
                if old is None or length < old[0]:
                    self.rejected[request.evidence.key] = length, exc.metadata()
            return "size_rejected"
        except RequestRejectedError as exc:
            self.results.reject(request, exc)
            self.inference.summary.request_rejections += 1
            return "request_rejected"
        for check in request.checks:
            trace = self.results.records[check.target.id].context_selection.get(check.rule_id)
            if trace:
                trace["outcome"] = (
                    "complete_target_fallback" if attempt.final else "compacted" if attempt.round else "questions_split"
                )
        response = prediction.response
        self.results.accept(request, response.answers, response.model, prediction.cached)
        return "accepted"

    def _split(self, attempt: Attempt) -> list[Attempt]:
        request = attempt.request
        # Probe a short singleton before retrying sibling batches with the same large state.
        probe = min(request.checks, key=lambda check: len(self.planner.questions[check.id]))
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
        known = self.rejected.get(request.evidence.key)
        blocked = (
            not attempt.final
            and known is not None
            and all(len(self.planner.questions[check.id]) >= known[0] for check in request.checks)
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

    async def run(self) -> None:
        for planned in self.planner.plan():
            pending = deque([Attempt(planned)])
            while pending:
                attempt = pending.popleft()
                request = attempt.request
                recovery = await self._preflight(attempt)
                if recovery is not None:
                    pending.extendleft(reversed(recovery))
                    continue
                outcome = await self._send(attempt)
                if outcome == "size_rejected":
                    recovery = (
                        self._split(attempt)
                        if len(request.checks) > 1
                        else await self._recover(attempt, "provider_context_limit")
                    )
                    pending.extendleft(reversed(recovery))
