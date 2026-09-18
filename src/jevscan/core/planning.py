"""Bind checks to targets, choose evidence, and pack bounded shared-state requests."""

import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from jevscan.core.config import Config, EvaluationConfig
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.models import CALLABLE_KINDS, Target
from jevscan.core.protocol import Check, encode
from jevscan.core.rules import Question


@dataclass(frozen=True, slots=True)
class Request:
    evidence: Evidence
    checks: tuple[Check, ...]
    body: bytes
    compaction_round: int = 0

    @property
    def questions(self) -> dict[str, Question]:
        return {check.id: check.rule.question for check in self.checks}


@dataclass(frozen=True, slots=True)
class Omission:
    check: Check
    reason: str


@dataclass(frozen=True, slots=True)
class Preparation:
    check: Check
    reason: str
    previous_bytes: int | None = None
    previous_tokens: int | None = None
    compaction_round: int = 0


class RequestBudget:
    """Use identical context, aggregate, question-count and byte limits in every phase."""

    def __init__(self, limits: EvaluationConfig, model: str) -> None:
        self.limits = limits
        self.model = encode(model)

    @staticmethod
    def _parts(questions: dict[str, bytes]) -> list[bytes]:
        return [encode(key) + b":" + value for key, value in questions.items()]

    def estimate(self, state: bytes, questions: dict[str, bytes]) -> tuple[int, int, int]:
        assert questions, "a prediction requires at least one question"
        parts = self._parts(questions)
        sizes = [math.ceil(len(part) / self.limits.bytes_per_token) for part in parts]
        fixed = len(b'{"model":,"questions":{},"state":}') + len(self.model)
        state_tokens = math.ceil((len(state) + fixed) / self.limits.bytes_per_token) + self.limits.token_reserve
        body_bytes = fixed + len(state) + sum(map(len, parts)) + len(parts) - 1
        return state_tokens + max(sizes), state_tokens + sum(sizes), body_bytes

    def fits(self, state: bytes, questions: dict[str, bytes]) -> bool:
        context, total, size = self.estimate(state, questions)
        return (
            len(questions) <= self.limits.max_questions
            and (self.limits.max_context_tokens is None or context <= self.limits.max_context_tokens)
            and (self.limits.max_total_tokens is None or total <= self.limits.max_total_tokens)
            and size <= self.limits.max_request_bytes
        )

    def body(self, state: bytes, questions: dict[str, bytes]) -> bytes:
        return (
            b'{"model":'
            + self.model
            + b',"questions":{'
            + b",".join(self._parts(questions))
            + b'},"state":'
            + state
            + b"}"
        )


class Planner:
    def __init__(self, context: ContextBuilder, config: Config) -> None:
        self.context = context
        self.limits = config.evaluation
        self.budget = RequestBudget(config.evaluation, config.jev.model)
        self.checks = self._checks(config)
        self.questions = {check.id: encode(check.question()) for check in self.checks}

    def _checks(self, config: Config) -> tuple[Check, ...]:
        targets = [self.context.file, *(Target.from_unit(unit) for unit in self.context.parsed.units)]
        checks = []
        rules = sorted(config.selected_rules().items())
        owners_with_members = {
            unit.parent_id
            for unit in self.context.parsed.units
            if unit.kind in CALLABLE_KINDS and unit.has_implementation
        }
        for target in targets:
            for name, rule in rules:
                if rule.target != target.scope or target.language not in rule.languages:
                    continue
                if target.scope == "unit":
                    unit = self.context.units[target.id]
                    if unit.kind not in rule.applies_to or (
                        rule.require_body and (not unit.has_body or not unit.has_implementation)
                    ):
                        continue
                    if rule.require_members and target.id not in owners_with_members:
                        continue
                checks.append(Check(f"q{len(checks):05d}", target, name, rule))
        return tuple(checks)

    def _encoded_questions(self, checks: tuple[Check, ...]) -> dict[str, bytes]:
        return {check.id: self.questions[check.id] for check in checks}

    def estimate(self, evidence: Evidence, checks: tuple[Check, ...]) -> tuple[int, int, int]:
        return self.budget.estimate(evidence.encoded, self._encoded_questions(checks))

    def fits(self, evidence: Evidence, checks: tuple[Check, ...]) -> bool:
        return self.budget.fits(evidence.encoded, self._encoded_questions(checks))

    def request(self, evidence: Evidence, checks: tuple[Check, ...], compaction_round: int = 0) -> Request:
        body = self.budget.body(evidence.encoded, self._encoded_questions(checks))
        return Request(evidence, checks, body, compaction_round)

    def _oversized(self, check: Check, reason: str, previous: Request | None = None) -> Preparation | Omission:
        if check.target.scope == "file" or self.limits.oversized_context == "skip":
            return Omission(check, reason + "; complete target/context required, source was not truncated")
        if previous is None:
            return Preparation(check, reason)
        tokens, _, _ = self.estimate(previous.evidence, (check,))
        return Preparation(check, reason, len(previous.body), tokens, previous.compaction_round)

    def pack(self, evidence: Evidence, checks: list[Check], compaction_round: int = 0) -> Iterator[Request]:
        pending: tuple[Check, ...] = ()
        for check in checks:
            candidate = (*pending, check)
            if pending and not self.fits(evidence, candidate):
                yield self.request(evidence, pending, compaction_round)
                pending = ()
            pending = (*pending, check)
        if pending:
            yield self.request(evidence, pending, compaction_round)

    def plan(self) -> Iterator[Request | Omission | Preparation]:
        groups: dict[tuple[int, int], list[Check]] = defaultdict(list)
        for check in self.checks:
            evidence = self.context.requested(check)
            if not self.fits(evidence, (check,)):
                yield self._oversized(check, "requested evidence exceeds configured request limits")
            else:
                groups[(evidence.start, evidence.end)].append(check)
        for span, checks in groups.items():
            evidence = self.context.envelope(*span)
            yield from self.pack(evidence, checks)

    def adapt_rejected_state(self, request: Request) -> tuple[Preparation | Omission, ...]:
        return tuple(
            self._oversized(check, "same evidence rejected for another check", request) for check in request.checks
        )

    def recover(self, request: Request) -> tuple[Request | Omission | Preparation, ...]:
        """Reduce question count before preparing smaller, target-complete evidence."""
        if len(request.checks) > 1:
            midpoint = len(request.checks) // 2
            return (
                self.request(request.evidence, request.checks[:midpoint], request.compaction_round),
                self.request(request.evidence, request.checks[midpoint:], request.compaction_round),
            )
        return (self._oversized(request.checks[0], "provider rejected request size", request),)
