"""Bind checks to targets, choose evidence, and pack bounded shared-state requests."""

import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

from jevscan.core.config import Config
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.models import Target
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


class Planner:
    def __init__(self, context: ContextBuilder, config: Config) -> None:
        self.context = context
        self.limits = config.evaluation
        self.model = encode(config.jev.model)
        self.checks = self._checks(config)
        self.questions = {check.id: encode(check.question()) for check in self.checks}

    def _checks(self, config: Config) -> tuple[Check, ...]:
        targets = [self.context.file, *(Target.from_unit(unit) for unit in self.context.parsed.units)]
        checks = []
        for target in targets:
            for name, rule in sorted(config.rules.items()):
                if not rule.enabled or rule.target != target.scope or target.language not in rule.languages:
                    continue
                if target.scope == "unit":
                    unit = self.context.units[target.id]
                    if unit.kind not in rule.applies_to or (rule.require_body and not unit.has_body):
                        continue
                checks.append(Check(f"q{len(checks):05d}", target, name, rule))
        return tuple(checks)

    def _tokens(self, value: bytes) -> int:
        return math.ceil(len(value) / self.limits.bytes_per_token)

    def _parts(self, checks: tuple[Check, ...]) -> list[bytes]:
        return [encode(check.id) + b":" + self.questions[check.id] for check in checks]

    def estimate(self, evidence: Evidence, checks: tuple[Check, ...]) -> tuple[int, int, int]:
        parts = self._parts(checks)
        sizes = [self._tokens(part) for part in parts]
        fixed = len(b'{"model":,"questions":{},"state":}') + len(self.model)
        state_tokens = math.ceil((len(evidence.encoded) + fixed) / self.limits.bytes_per_token)
        state_tokens += self.limits.token_reserve
        body_bytes = fixed + len(evidence.encoded) + sum(map(len, parts)) + len(parts) - 1
        return state_tokens + max(sizes), state_tokens + sum(sizes), body_bytes

    def fits(self, evidence: Evidence, checks: tuple[Check, ...]) -> bool:
        context_tokens, total_tokens, body_bytes = self.estimate(evidence, checks)
        limits = self.limits
        return (
            len(checks) <= limits.max_questions
            and context_tokens <= limits.max_context_tokens
            and total_tokens <= limits.max_total_tokens
            and body_bytes <= limits.max_request_bytes
        )

    def _body(self, evidence: Evidence, checks: tuple[Check, ...]) -> bytes:
        return (
            b'{"model":'
            + self.model
            + b',"questions":{'
            + b",".join(self._parts(checks))
            + b'},"state":'
            + evidence.encoded
            + b"}"
        )

    def request(self, evidence: Evidence, checks: tuple[Check, ...]) -> Request:
        return Request(evidence, checks, self._body(evidence, checks))

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
