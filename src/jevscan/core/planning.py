"""Bind checks to targets, choose evidence, and pack bounded shared-state requests."""

import math
from collections.abc import Iterator
from dataclasses import dataclass

from jevscan.core.config import Config, EvaluationConfig
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.model_limits import TokenCalibration, limits_for_model
from jevscan.core.models import CALLABLE_KINDS, Target
from jevscan.core.protocol import Check, PromptRegistry, encode
from jevscan.core.rules import Question, Rule


@dataclass(frozen=True, slots=True)
class Request:
    evidence: Evidence
    checks: tuple[Check, ...]
    body: bytes
    state: bytes
    question_wires: dict[str, bytes]
    registry: PromptRegistry

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
        self.targets = (self.context.file, *(Target.from_unit(unit) for unit in self.context.parsed.units))
        self.applicability_skips: dict[str, dict[str, str]] = {}
        checks = self._checks(config)
        self.omissions: tuple[Omission, ...] = ()
        line_limit = config.scan.max_full_file_lines
        self.full_file_limited = line_limit is not None and self.context.file.end_line > line_limit
        if self.full_file_limited:
            kept: list[Check] = []
            omitted: list[Omission] = []
            for check in checks:
                # Explicit file judgments and rules that explicitly require file context
                # are not approximated. Top-level owner-context unit checks may fall
                # back to their complete target and are marked context-reduced.
                if check.target.scope == "file" or check.rule.context == "file":
                    omitted.append(
                        Omission(
                            check,
                            f"full-file context is {self.context.file.end_line} lines; configured limit is {line_limit}",
                        )
                    )
                else:
                    kept.append(check)
            checks = tuple(kept)
            self.omissions = tuple(omitted)
        self.checks = checks
        self.question_wires = {check.id: encode(check.question()) for check in self.checks}
        self._groups = self._evidence_groups()
        self._registry_by_check: dict[str, PromptRegistry] = {}
        for _, group_checks in self._groups:
            registry = PromptRegistry.from_questions(check.rule.question for check in group_checks)
            self._registry_by_check.update({check.id: registry for check in group_checks})

    def _facts(self, target: Target) -> frozenset[str]:
        if target.scope == "file":
            return frozenset(fact for unit in self.context.parsed.units for fact in unit.syntax_facts)
        return frozenset(self.context.units[target.id].syntax_facts)

    def _applicability_reason(self, target: Target, rule: Rule) -> str | None:
        policy = rule.applicability
        if policy is None:
            return None
        facts = self._facts(target)
        missing_all = [fact for fact in policy.requires_all if fact not in facts]
        any_present = not policy.requires_any or bool(facts & set(policy.requires_any))
        if not missing_all and any_present:
            return None
        missing = [*missing_all, *(policy.requires_any if not any_present else [])]
        declared = ", ".join(missing)
        return f"applicability: declared syntax prerequisite absent ({declared})"

    def _checks(self, config: Config) -> tuple[Check, ...]:
        checks = []
        rules = sorted(config.selected_rules().items())
        owners_with_members = {
            unit.parent_id
            for unit in self.context.parsed.units
            if unit.kind in CALLABLE_KINDS and unit.has_implementation
        }
        for target in self.targets:
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
                if (reason := self._applicability_reason(target, rule)) is not None:
                    self.applicability_skips.setdefault(target.id, {})[name] = reason
                    continue
                checks.append(Check(f"q{len(checks):05d}", target, name, rule))
        return tuple(checks)

    def requested_target(self, check: Check) -> Target:
        target = self.context.requested_target(check)
        if (
            self.full_file_limited
            and check.target.scope == "unit"
            and check.rule.context == "owner"
            and target.scope == "file"
        ):
            return check.target
        return target

    def requested_evidence(self, check: Check) -> Evidence:
        target = self.requested_target(check)
        if target == check.target and self.context.requested_target(check).scope == "file":
            return self.context.envelope(check.target.start_byte, check.target.end_byte)
        return self.context.requested(check)

    def registry_for(self, checks: tuple[Check, ...]) -> PromptRegistry:
        """Return the stable registry for the shared evidence/target-scope groups."""
        if not checks:
            raise ValueError("a request requires at least one check")
        registry = self._registry_by_check[checks[0].id]
        if any(self._registry_by_check[check.id] is not registry for check in checks[1:]):
            raise ValueError("request checks belong to different evidence groups")
        return registry

    def state(self, evidence: Evidence, checks: tuple[Check, ...], registry: PromptRegistry | None = None) -> bytes:
        active_registry = registry or self.registry_for(checks)
        active_registry.validate_questions(check.rule.question for check in checks)
        return active_registry.state_bytes(evidence.state)

    def _encoded_questions(self, checks: tuple[Check, ...]) -> dict[str, bytes]:
        return {check.id: self.question_wires[check.id] for check in checks}

    def estimate(
        self, evidence: Evidence, checks: tuple[Check, ...], registry: PromptRegistry | None = None
    ) -> tuple[int, int, int]:
        return self.budget.estimate(self.state(evidence, checks, registry), self._encoded_questions(checks))

    def fits(self, evidence: Evidence, checks: tuple[Check, ...], registry: PromptRegistry | None = None) -> bool:
        return self.budget.fits(self.state(evidence, checks, registry), self._encoded_questions(checks))

    def violations(
        self, evidence: Evidence, checks: tuple[Check, ...], registry: PromptRegistry | None = None
    ) -> frozenset[str]:
        return self.budget.violations(self.state(evidence, checks, registry), self._encoded_questions(checks))

    def request(self, evidence: Evidence, checks: tuple[Check, ...], registry: PromptRegistry | None = None) -> Request:
        registry = registry or self.registry_for(checks)
        state = self.state(evidence, checks, registry)
        question_wires = self._encoded_questions(checks)
        body = self.budget.body(state, question_wires)
        return Request(evidence, checks, body, state, question_wires, registry)

    def _evidence_groups(self) -> list[tuple[Evidence, list[Check]]]:
        groups: dict[tuple[str, str], tuple[Evidence, list[Check]]] = {}
        for check in self.checks:
            evidence = self.requested_evidence(check)
            # File-wide and unit judgments can have identical source evidence but
            # answer different questions. Keep their rubric registries independent.
            group_key = evidence.key, check.target.scope
            groups.setdefault(group_key, (evidence, []))[1].append(check)
        return list(groups.values())

    def _pack_group(self, evidence: Evidence, checks: list[Check], registry: PromptRegistry) -> Iterator[Request]:
        # Evidence that cannot fit even one question must reach recovery intact.
        # Fragmenting it here would prevent recovery from regrouping sibling checks
        # that converge on the same compacted evidence.
        singleton_violations = self.violations(evidence, (checks[0],), registry)
        if singleton_violations & {"context", "bytes"}:
            step = self.limits.max_questions
            for start in range(0, len(checks), step):
                yield self.request(evidence, tuple(checks[start : start + step]), registry)
            return

        pending: tuple[Check, ...] = ()
        for check in checks:
            candidate = (*pending, check)
            if pending and not self.fits(evidence, candidate, registry):
                yield self.request(evidence, pending, registry)
                pending = ()
            pending = (*pending, check)
        if pending:
            yield self.request(evidence, pending, registry)

    def plan(self) -> Iterator[Request]:
        # Exact evidence and target scope define a shared group. Different targets
        # within that scope can still share one state and be split into wire batches.
        for evidence, checks in self._groups:
            yield from self._pack_group(evidence, checks, self.registry_for(tuple(checks)))
