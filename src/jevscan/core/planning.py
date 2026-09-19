"""Bind checks to targets, choose evidence, and pack bounded shared-state requests."""

import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from jevscan.core.config import Config, EvaluationConfig
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.model_limits import TokenCalibration, limits_for_model
from jevscan.core.models import CALLABLE_KINDS, Target
from jevscan.core.protocol import Check, encode
from jevscan.core.rules import Question


@dataclass(frozen=True, slots=True)
class Request:
    evidence: Evidence
    checks: tuple[Check, ...]
    body: bytes

    @property
    def questions(self) -> dict[str, Question]:
        return {check.id: check.rule.question for check in self.checks}


@dataclass(frozen=True, slots=True)
class BudgetEstimate:
    context_tokens: int
    total_tokens: int
    body_bytes: int
    questions: int


@dataclass(frozen=True, slots=True)
class Omission:
    check: Check
    reason: str


class RequestBudget:
    """Use identical context, aggregate, question-count and byte limits in every phase."""

    def __init__(self, limits: EvaluationConfig, model: str, calibration: TokenCalibration | None = None) -> None:
        self.limits = limits
        self.model_name = model
        self.model = encode(model)
        self.calibration = calibration or TokenCalibration()
        published = limits_for_model(model)
        self.max_context_tokens = self._minimum(
            limits.max_context_tokens, published.context_tokens if published else None
        )
        self.max_total_tokens = self._minimum(limits.max_total_tokens, published.total_tokens if published else None)

    @staticmethod
    def _minimum(first: int | None, second: int | None) -> int | None:
        values = [value for value in (first, second) if value is not None]
        return min(values) if values else None

    @staticmethod
    def _parts(questions: dict[str, bytes]) -> list[bytes]:
        return [encode(key) + b":" + value for key, value in questions.items()]

    def estimate(self, state: bytes, questions: dict[str, bytes]) -> tuple[int, int, int]:
        assert questions, "a prediction requires at least one question"
        parts = self._parts(questions)
        bytes_per_token = self.calibration.effective(self.limits.bytes_per_token)
        sizes = [math.ceil(len(part) / bytes_per_token) for part in parts]
        fixed = len(b'{"model":,"questions":{},"state":}') + len(self.model)
        state_tokens = math.ceil((len(state) + fixed) / bytes_per_token) + self.limits.token_reserve
        body_bytes = fixed + len(state) + sum(map(len, parts)) + len(parts) - 1
        return state_tokens + max(sizes), state_tokens + sum(sizes), body_bytes

    def budget(self, state: bytes, questions: dict[str, bytes]) -> BudgetEstimate:
        context, total, size = self.estimate(state, questions)
        return BudgetEstimate(context, total, size, len(questions))

    def violations(self, state: bytes, questions: dict[str, bytes]) -> frozenset[str]:
        estimate = self.budget(state, questions)
        problems = set()
        if estimate.questions > self.limits.max_questions:
            problems.add("questions")
        if self.max_context_tokens is not None and estimate.context_tokens > self.max_context_tokens:
            problems.add("context")
        if self.max_total_tokens is not None and estimate.total_tokens > self.max_total_tokens:
            problems.add("total")
        if estimate.body_bytes > self.limits.max_request_bytes:
            problems.add("bytes")
        return frozenset(problems)

    def fits(self, state: bytes, questions: dict[str, bytes]) -> bool:
        return not self.violations(state, questions)

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
    def __init__(self, context: ContextBuilder, config: Config, calibration: TokenCalibration | None = None) -> None:
        self.context = context
        self.limits = config.evaluation
        self.compaction = config.compaction
        self.budget = RequestBudget(config.evaluation, config.jev.model, calibration)
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

    def violations(self, evidence: Evidence, checks: tuple[Check, ...]) -> frozenset[str]:
        return self.budget.violations(evidence.encoded, self._encoded_questions(checks))

    def request(self, evidence: Evidence, checks: tuple[Check, ...]) -> Request:
        body = self.budget.body(evidence.encoded, self._encoded_questions(checks))
        return Request(evidence, checks, body)

    def plan(self) -> Iterator[Request]:
        # Group by requested lexical spans, not retained copies of every large owner.
        groups: dict[tuple[int, int], list[Check]] = defaultdict(list)
        for check in self.checks:
            target = self.context.requested_target(check)
            groups[(target.start_byte, target.end_byte)].append(check)
        for checks in groups.values():
            evidence = self.context.requested(checks[0])
            pending: tuple[Check, ...] = ()
            for check in checks:
                candidate = (*pending, check)
                if pending and not self.fits(evidence, candidate):
                    yield self.request(evidence, pending)
                    pending = ()
                pending = (*pending, check)
            if pending:
                yield self.request(evidence, pending)
