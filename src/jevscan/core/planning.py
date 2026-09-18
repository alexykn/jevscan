"""Bind checks to targets, choose evidence, and pack bounded shared-state requests."""

import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

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

    @property
    def questions(self) -> dict[str, Question]:
        return {check.id: check.rule.question for check in self.checks}


@dataclass(frozen=True, slots=True)
class Omission:
    check: Check
    reason: str


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
            and context <= self.limits.max_context_tokens
            and total <= self.limits.max_total_tokens
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

    def request(self, evidence: Evidence, checks: tuple[Check, ...]) -> Request:
        body = self.budget.body(evidence.encoded, self._encoded_questions(checks))
        return Request(evidence, checks, body)

    def _select(self, check: Check) -> Evidence | None:
        variants = self.context.variants(check)
        if self.limits.oversized_context == "skip":
            variants = islice(variants, 1)
        return next((evidence for evidence in variants if self.fits(evidence, (check,))), None)

    def plan(self) -> Iterator[Request | Omission]:
        groups: dict[tuple[int, int], list[Check]] = defaultdict(list)
        for check in self.checks:
            evidence = self._select(check)
            if evidence is None:
                variants = tuple(self.context.variants(check))
                candidate = variants[0] if self.limits.oversized_context == "skip" else variants[-1]
                context_tokens, _, body_bytes = self.estimate(candidate, (check,))
                yield Omission(
                    check,
                    f"complete target plus question needs approximately {context_tokens:,} tokens "
                    f"(context budget {self.limits.max_context_tokens:,}) and {body_bytes:,} request bytes "
                    f"(byte budget {self.limits.max_request_bytes:,}); target was not truncated",
                )
            else:
                groups[evidence.key].append(check)
        for key, checks in groups.items():
            evidence = self.context.envelope(*key)
            pending: tuple[Check, ...] = ()
            for check in checks:
                candidate = (*pending, check)
                if pending and not self.fits(evidence, candidate):
                    yield self.request(evidence, pending)
                    pending = ()
                pending = (*pending, check)
            if pending:
                yield self.request(evidence, pending)

    def recover(self, request: Request) -> tuple[Request | Omission, ...]:
        """Every recovery reduces questions or source extent. Never truncate a target."""
        if len(request.checks) > 1:
            midpoint = len(request.checks) // 2
            return (
                self.request(request.evidence, request.checks[:midpoint]),
                self.request(request.evidence, request.checks[midpoint:]),
            )
        check = request.checks[0]
        if self.limits.oversized_context == "reduce":
            after_current = False
            for evidence in self.context.variants(check):
                if after_current and self.fits(evidence, (check,)):
                    return (self.request(evidence, (check,)),)
                after_current |= evidence.key == request.evidence.key
        return (Omission(check, "provider context limit rejects this complete target; no smaller evidence fits"),)
