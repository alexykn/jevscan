"""Prepare target-complete AST evidence only after local limits or provider rejection.

Source selection is not program slicing: lexical references are hints. Every omitted
range remains explicit. Whole-file targets are never assessed from a partial file.
"""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from jevscan.core.context import ContextBuilder, Evidence, Span
from jevscan.core.inference import Inference, Prediction
from jevscan.core.models import Kind, Target
from jevscan.core.planning import Omission, Planner, Preparation, Request, RequestBudget
from jevscan.core.protocol import Check, ContextLimitError, encode
from jevscan.core.rules import Question
from jevscan.core.selection import rank_candidates


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    context: ContextBuilder
    span: Span
    relation: str
    priority: int
    name: str

    @property
    def id(self) -> str:
        return f"block_{self.span[0]}_{self.span[1]}"

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.context.parsed.path,
            "start_byte": self.span[0],
            "end_byte": self.span[1],
            "relation": self.relation,
            "name": self.name,
            "resolution": "lexical_source_not_resolved",
        }

    def preview(self) -> dict[str, Any]:
        text = self.context.parsed.source[self.span[0] : self.span[1]].decode("utf-8")
        return {**self.metadata(), "preview": text[:1200], "preview_complete": len(text) <= 1200}


def _definition_candidates(
    context: ContextBuilder, target: Target, references: set[str], class_owners: set[str]
) -> Iterator[ContextCandidate]:
    wanted = set(references)
    for depth in range(2):
        added_names: set[str] = set()
        for other in context.parsed.units:
            if other.id == target.id or other.start_byte <= target.start_byte < other.end_byte:
                continue
            if other.name in wanted:
                yield ContextCandidate(
                    context, (other.start_byte, other.end_byte), "referenced_definition", 1 + depth, other.name
                )
                added_names.update(context.names_in(other.start_byte, other.end_byte))
            elif other.parent_id in class_owners:
                priority = 1 if other.name in {"constructor", "__init__", "new"} else 3
                yield ContextCandidate(
                    context, (other.start_byte, other.end_byte), "owner_member", priority, other.name
                )
        wanted = added_names - wanted


def _declaration_candidates(
    context: ContextBuilder, target: Target, references: set[str]
) -> Iterator[ContextCandidate]:
    for block in context.parsed.blocks:
        visible_scope = block.scope_start <= target.start_byte and target.end_byte <= block.scope_end
        if "import" in block.syntax_type or block.syntax_type in {"use_declaration", "use_statement"}:
            if not visible_scope:
                continue
            relation, priority = "import", 0
        elif set(block.names) & references:
            relation, priority = "referenced_declaration", 1
        elif visible_scope:
            relation, priority = "lexical_setup", 2
        else:
            continue
        yield ContextCandidate(context, (block.start_byte, block.end_byte), relation, priority, ", ".join(block.names))


def local_candidates(context: ContextBuilder, check: Check) -> list[ContextCandidate]:
    """Discover exact declarations, captured state, owner structure and referenced units."""
    target = check.target
    references = context.names_in(target.start_byte, target.end_byte)
    ancestors = []
    unit = context.units[target.id]
    while unit.parent_id:
        unit = context.units[unit.parent_id]
        ancestors.append(unit)
    available = []
    for owner in ancestors:
        if owner.body_start_byte is not None:
            available.append(
                ContextCandidate(context, (owner.start_byte, owner.body_start_byte), "owner_signature", 0, owner.name)
            )
            references.update(context.names_in(owner.start_byte, owner.body_start_byte))
    available.extend(
        _definition_candidates(
            context, target, references, {owner.id for owner in ancestors if owner.kind == Kind.CLASS}
        )
    )
    available.extend(_declaration_candidates(context, target, references))
    candidates: dict[Span, ContextCandidate] = {}
    for candidate in available:
        start, end = candidate.span
        if start == end or (target.start_byte <= start and end <= target.end_byte):
            continue
        if start <= target.start_byte and target.end_byte <= end:
            continue  # A giant enclosing body is not a compact source block.
        previous = candidates.get(candidate.span)
        if previous is None or candidate.priority < previous.priority:
            candidates[candidate.span] = candidate
    return sorted(candidates.values(), key=lambda item: (item.priority, item.span[1] - item.span[0], item.span))


class CompactionBudgetError(Exception):
    """Auxiliary calls are exhausted; deterministic source selection still proceeds."""


class Compactor:
    def __init__(self, planner: Planner, inference: Inference) -> None:
        self.planner, self.inference = planner, inference
        self.context = planner.context
        self.calls = 0

    def _budget(self, task: Preparation) -> RequestBudget:
        limits = self.planner.limits
        tokens = min(limits.recovery_context_tokens, limits.max_context_tokens or limits.recovery_context_tokens)
        byte_limit = limits.max_request_bytes
        if task.previous_bytes is not None:
            assert task.previous_tokens is not None
            estimated = task.previous_tokens
            tokens = min(tokens, max(1, int(estimated * 0.6)))
            byte_limit = min(byte_limit, task.previous_bytes - 1)
        # Lower than public configuration minima is intentional after provider rejections.
        return RequestBudget(
            limits.model_copy(update={"max_context_tokens": tokens, "max_request_bytes": byte_limit}),
            self.inference.client.config.model,
        )

    async def _rank(
        self,
        task: Preparation,
        base: Evidence,
        candidates: list[ContextCandidate],
        budget: RequestBudget,
        trace: dict[str, Any],
    ) -> dict[str, float]:
        async def call(state: bytes, questions: dict[str, Question], wire: dict[str, bytes]) -> Prediction:
            if self.calls >= self.planner.limits.compaction_calls_per_file:
                raise CompactionBudgetError
            self.calls += 1
            body = budget.body(state, wire)
            item: dict[str, Any] = {"phase": "compaction_selection", "request_sha256": hashlib.sha256(body).hexdigest()}
            trace["predictions"].append(item)
            try:
                prediction = await self.inference.predict(body, questions, compaction=True)
            except ContextLimitError as exc:
                item["rejection"] = exc.metadata()
                raise
            item.update({
                "cached": prediction.cached,
                "model": prediction.response.model,
                "answers": {key: value.model_dump(mode="json") for key, value in prediction.response.answers.items()},
            })
            return prediction

        try:
            return await rank_candidates(task.check, base.state, candidates, budget, call, trace)
        except CompactionBudgetError:
            trace["selection_stop"] = "compaction_call_budget"
            return {item["id"]: item["relevance"] for item in trace["candidates"]}

    def _retain(
        self,
        check: Check,
        evidence: Evidence,
        candidates: list[ContextCandidate],
        budget: RequestBudget,
        trace: dict[str, Any],
    ) -> Evidence:
        spans = [(doc["start_byte"], doc["end_byte"]) for doc in evidence.state["documents"]]
        wire = {check.id: encode(check.question())}
        for candidate in candidates:
            proposed = self.context.assemble([*spans, candidate.span])
            if budget.fits(proposed.encoded, wire):
                spans.append(candidate.span)
                evidence = proposed
                trace["selected"].append(candidate.metadata())
            else:
                trace["omitted_candidates"].append({**candidate.metadata(), "reason": "context_budget"})
        return evidence

    def _outline(self, check: Check, evidence: Evidence, budget: RequestBudget, trace: dict[str, Any]) -> Evidence:
        omitted = [
            unit
            for unit in self.context.parsed.units
            if not self.context.contains(evidence, unit.start_byte, unit.end_byte)
        ]
        # Signatures are an explicitly partial index, not substitute implementations.
        entries = [
            {
                "id": unit.id,
                "name": unit.qualified_name,
                "start_byte": unit.start_byte,
                "end_byte": unit.end_byte,
                "signature_preview": unit.signature[:256],
            }
            for unit in omitted[:16]
        ]
        wire = {check.id: encode(check.question())}
        while entries:
            state = {
                **evidence.state,
                "source_outline": {
                    "entries": entries,
                    "omitted_entries": len(omitted) - len(entries),
                    "implementations_supplied": False,
                },
            }
            encoded = encode(state)
            if budget.fits(encoded, wire):
                trace["outline_entries"] = len(entries)
                return replace(evidence, state=state, encoded=encoded)
            entries.pop()
        trace["outline_entries"] = 0
        return evidence

    def _minimum_request(self, task: Preparation, trace: dict[str, Any]) -> Request | Omission:
        minimum = self.context.minimum(task.check)
        request = self.planner.request(minimum, (task.check,))
        if self.planner.fits(minimum, (task.check,)) and (
            task.previous_bytes is None or len(request.body) < task.previous_bytes
        ):
            trace["method"] = "complete_target_only"
            return replace(request, compaction_round=4)
        return Omission(task.check, "complete target cannot fit the available context; source was not truncated")

    async def prepare(self, task: Preparation, trace: dict[str, Any]) -> Request | Omission:
        check = task.check
        trace.update({
            "reason": task.reason,
            "adapted_after_rejection": task.previous_bytes is not None,
            "predictions": [],
            "candidates": [],
            "omitted_candidates": [],
            "selected": [],
            "method": "ast",
            "source_sha256": hashlib.sha256(self.context.parsed.source).hexdigest(),
        })
        minimum = self.context.minimum(check)
        assert check.target.scope == "unit", "file targets cannot be compacted"
        if task.compaction_round >= 3:
            return self._minimum_request(task, trace)
        budget = self._budget(task)
        wire = {check.id: encode(check.question())}
        if not budget.fits(minimum.encoded, wire):
            return self._minimum_request(task, trace)
        available = local_candidates(self.context, check)
        candidates = available[: self.planner.limits.compaction_candidates]
        trace["candidate_limit_omissions"] = len(available) - len(candidates)
        # Lexical owner headers and imports are selected without asking the model to overrule syntax.
        evidence = self._retain(check, minimum, [item for item in candidates if item.priority == 0], budget, trace)
        residual = [item for item in candidates if item.priority > 0]
        spans = [(doc["start_byte"], doc["end_byte"]) for doc in evidence.state["documents"]]
        combined = self.context.assemble([*spans, *(candidate.span for candidate in residual)])
        scores: dict[str, float] = {}
        if residual and not budget.fits(combined.encoded, wire) and self.planner.limits.compaction_calls_per_file:
            scores = await self._rank(task, evidence, residual, budget, trace)
            trace["method"] = "ast_with_relevance" if scores else "ast"
        # Direct references precede transitive/setup candidates; model scores rank within those groups.
        residual.sort(key=lambda item: (item.priority, -scores.get(item.id, 0.5), item.span))
        evidence = self._retain(check, evidence, residual, budget, trace)
        evidence = self._outline(check, evidence, budget, trace)
        result = self.planner.request(evidence, (check,))
        return replace(result, compaction_round=1 + task.compaction_round)
