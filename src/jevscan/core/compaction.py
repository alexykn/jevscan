"""Rule-aware, exact-span context selection after an explicit size constraint.

The target is immutable. Syntax supplies candidates; optional Jev judgments order
residual evidence, never authorize truncating a target or claiming full coverage.
"""

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from jevscan.core.context import ContextBuilder, Evidence, Span
from jevscan.core.inference import Inference, Prediction
from jevscan.core.models import CALLABLE_KINDS, Target, Unit
from jevscan.core.planning import Planner, RequestBudget
from jevscan.core.protocol import Check, ContextLimitError, PromptRegistry, encode
from jevscan.core.retrieval import Candidate, Snapshot
from jevscan.core.rules import Question
from jevscan.core.selection import rank_candidates


@dataclass(frozen=True, slots=True)
class ContextRecipe:
    target: Span
    scaffold: tuple[Span, ...]
    candidates: tuple[Candidate, ...]
    candidate_count: int
    outline: tuple[dict[str, Any], ...]


class LocalContext:
    """One syntax index per active file; no cross-file reads or generated source."""

    def __init__(self, context: ContextBuilder) -> None:
        self.context = context
        self.snapshot = Snapshot.from_parsed(context.parsed)
        self.references = sorted(context.parsed.references, key=lambda reference: reference.start_byte)
        self.starts = [reference.start_byte for reference in self.references]
        self.definitions: dict[str, list[Candidate]] = defaultdict(list)
        self.declarations: list[Candidate] = []
        for unit in context.parsed.units:
            self.definitions[unit.name].append(Candidate(self.snapshot, Target.from_unit(unit), "local_definition"))
        for block in context.parsed.blocks:
            location = context.location(block.start_byte, block.end_byte)
            name = ", ".join(block.names) or block.kind
            target = Target(
                f"{context.parsed.path}:{block.start_byte}:declaration",
                "unit",
                context.parsed.path,
                context.parsed.language,
                name,
                location["start_byte"],
                location["end_byte"],
                location["start_line"],
                location["end_line"],
            )
            candidate = Candidate(self.snapshot, target, "local_" + block.kind)
            self.declarations.append(candidate)
            for name in block.names:
                self.definitions[name].append(candidate)

    def _names(self, start: int, end: int) -> set[str]:
        return {
            reference.name
            for reference in self.references[bisect_left(self.starts, start) : bisect_left(self.starts, end)]
        }

    def _parents(self, target: Target) -> list[Unit]:
        parents = []
        parent_id = self.context.units[target.id].parent_id
        while parent_id:
            parent = self.context.units[parent_id]
            parents.append(parent)
            parent_id = parent.parent_id
        return parents

    def _dependencies(self, target: Target, limit: int) -> dict[str, Candidate]:
        related: dict[str, Candidate] = {}
        frontier = self._names(target.start_byte, target.end_byte)
        visited: set[str] = set()
        for _ in range(2):
            visited.update(frontier)
            next_names = set()
            for name in sorted(frontier):
                for candidate in self.definitions.get(name, ()):
                    item = candidate.target
                    # Neither the entire rejected owner nor code already inside the target adds evidence.
                    if item.start_byte <= target.start_byte and item.end_byte >= target.end_byte:
                        continue
                    if target.start_byte <= item.start_byte and item.end_byte <= target.end_byte:
                        continue
                    related[candidate.id] = candidate
                    if len(related) <= limit:
                        next_names.update(self._names(item.start_byte, item.end_byte))
            frontier = next_names - visited
        return related

    def _owner_state(self, parents: list[Unit]) -> list[Candidate]:
        owners = [parent for parent in parents if parent.kind not in CALLABLE_KINDS]
        owner_ids = {owner.id for owner in owners}
        candidates = []
        for candidate in self.declarations:
            item = candidate.target
            if (
                candidate.relation
                in {
                    "local_import_statement",
                    "local_import_from_statement",
                    "local_use_declaration",
                    "local_use_statement",
                }
                or candidate.relation
                in {
                    "local_field_definition",
                    "local_public_field_definition",
                    "local_field_declaration",
                }
                and any(owner.start_byte < item.start_byte and item.end_byte < owner.end_byte for owner in owners)
            ):
                candidates.append(candidate)
        candidates.extend(
            Candidate(self.snapshot, Target.from_unit(unit), "owner_constructor")
            for unit in self.context.parsed.units
            if unit.parent_id in owner_ids and unit.name in {"constructor", "__init__", "new"}
        )
        return candidates

    def _outline(self, parents: list[Unit]) -> tuple[dict[str, Any], ...]:
        owner_ids = {parent.id for parent in parents if parent.kind not in CALLABLE_KINDS}
        units = [*reversed(parents), *(unit for unit in self.context.parsed.units if unit.parent_id in owner_ids)]
        return tuple(
            {
                "name": unit.display_name or unit.qualified_name,
                "signature": unit.signature[:160],
                "start_line": unit.start_line,
                "end_line": unit.end_line,
            }
            for unit in units[:24]
        )

    def recipe(self, check: Check, limit: int) -> ContextRecipe:
        target = check.target
        parents = self._parents(target)
        # These are exact AST-bound header fragments, not fabricated compilable stubs.
        scaffold = tuple(
            (parent.start_byte, parent.body_start_byte)
            for parent in parents
            if parent.body_start_byte is not None and parent.start_byte < parent.body_start_byte
        )
        related = self._dependencies(target, limit)
        for candidate in self._owner_state(parents):
            related.setdefault(candidate.id, candidate)
        candidates = [
            item
            for item in related.values()
            if not (target.start_byte <= item.target.start_byte and item.target.end_byte <= target.end_byte)
        ]
        # Imports follow full referenced source/state; source order provides deterministic ties.
        candidates.sort(
            key=lambda item: (
                item.relation.startswith(("local_import", "local_use")),
                item.target.start_byte,
                item.target.end_byte,
            )
        )
        return ContextRecipe(
            (target.start_byte, target.end_byte),
            scaffold,
            tuple(candidates[:limit]),
            len(candidates),
            self._outline(parents),
        )


class SelectionStoppedError(Exception):
    """An optional selection budget was reached; deterministic source selection remains available."""


@dataclass(slots=True)
class CompactionRound:
    recipe: ContextRecipe
    budget: RequestBudget
    wire: dict[str, bytes]
    scaffold: bool
    base: Evidence
    candidates: list[Candidate]
    trace: dict[str, Any]


class Compactor:
    def __init__(self, planner: Planner, inference: Inference) -> None:
        self.planner, self.inference = planner, inference
        self.limits = planner.compaction
        self.local = LocalContext(planner.context)
        self.calls = 0

    def _budget(self, previous_bytes: int, round_number: int) -> RequestBudget:
        limits = self.planner.limits
        context_tokens = max(1024, self.limits.context_tokens // (2**round_number))
        for ceiling in (limits.max_context_tokens, limits.max_total_tokens):
            if ceiling is not None:
                context_tokens = min(context_tokens, ceiling)
        # Serialized request size decreases even when the approximate tokenizer was wrong.
        byte_limit = min(limits.max_request_bytes, max(1, int(previous_bytes * 0.70)))
        reduced = limits.model_copy(update={"max_context_tokens": context_tokens, "max_request_bytes": byte_limit})
        return RequestBudget(reduced, self.inference.client.config.model, self.planner.budget.calibration)

    async def _predict(
        self, phase: str, state: bytes, questions: dict[str, Question], wire: dict[str, bytes], trace: dict[str, Any]
    ) -> Prediction:
        if self.calls >= self.limits.max_calls_per_file:
            raise SelectionStoppedError("selection_call_budget")
        self.calls += 1
        body = self.planner.budget.body(state, wire)
        entry: dict[str, Any] = {"phase": phase, "request_sha256": hashlib.sha256(body).hexdigest()}
        trace["predictions"].append(entry)
        result = await self.inference.predict(
            body,
            questions,
            compaction=True,
            state_bytes=len(state),
            question_bytes=sum(map(len, wire.values())),
            state=json.loads(state),
        )
        entry.update({
            "model": result.response.model,
            "cached": result.cached,
            "metrics": result.metrics,
            "answers": {key: value.model_dump(mode="json") for key, value in result.response.answers.items()},
        })
        return result

    def _compose(self, recipe: ContextRecipe, selected: list[Candidate], scaffold: bool) -> Evidence:
        spans = [recipe.target, *(recipe.scaffold if scaffold else ())]
        spans.extend((item.target.start_byte, item.target.end_byte) for item in selected)
        evidence = self.planner.context.compose(spans)
        if not scaffold or not recipe.outline:
            return evidence
        state = {
            **evidence.state,
            "outline": {
                "items": recipe.outline,
                "complete": False,
                "meaning": "Bounded lexical signatures only; omitted implementations remain unknown.",
            },
        }
        return Evidence(state, encode(state))

    async def _prioritize(
        self, check: Check, base: Evidence, candidates: list[Candidate], budget: RequestBudget, trace: dict[str, Any]
    ) -> list[Candidate]:
        try:
            ranked = await rank_candidates(
                check, base, candidates, budget, self._predict, trace, PromptRegistry.for_question(check.rule.question)
            )
        except (SelectionStoppedError, ContextLimitError) as exc:
            trace["selection_stop"] = "provider_context_limit" if isinstance(exc, ContextLimitError) else str(exc)
            by_id = {candidate.id: candidate for candidate in candidates}
            ranked = sorted(
                ((item["relevance"], by_id[item["id"]]) for item in trace["candidates"]), key=lambda pair: -pair[0]
            )
        if not ranked:
            trace.setdefault("selection_stop", "selection_budget")
            return candidates
        # Low relevance is not proof of irrelevance; keep remaining candidates in syntax order.
        scored = {item.id for _, item in ranked}
        trace["method"] = "rule_relevance"
        return [item for _, item in ranked] + [item for item in candidates if item.id not in scored]

    def _prepare_round(
        self,
        check: Check,
        previous_bytes: int,
        round_number: int,
        trace: dict[str, Any],
        rubric: PromptRegistry,
    ) -> CompactionRound | None:
        if check.target.scope == "file" or self.planner.limits.oversized_context == "skip":
            return None

        recipe = self.local.recipe(check, self.limits.max_candidates)
        budget = self._budget(previous_bytes, round_number)
        wire = {check.id: self.planner.question_wires[check.id]}
        target_only = self._compose(recipe, [], False)
        if not budget.fits(self.planner.state(target_only, (check,), rubric), wire):
            return None

        scaffold = budget.fits(
            self.planner.state(self._compose(recipe, [], True), (check,), rubric),
            wire,
        )
        base = self._compose(recipe, [], scaffold)
        candidates = list(recipe.candidates)
        entry: dict[str, Any] = {
            "round": round_number + 1,
            "predictions": [],
            "candidates": [],
            "omitted_candidates": [],
            "candidate_count": recipe.candidate_count,
            "candidate_limit_omissions": max(0, recipe.candidate_count - len(candidates)),
            "scaffold": scaffold,
            "budget": budget.limits.model_dump(mode="json"),
            "method": "syntax",
        }
        trace["compactions"].append(entry)
        return CompactionRound(recipe, budget, wire, scaffold, base, candidates, entry)

    async def _ordered_candidates(
        self,
        check: Check,
        round_state: CompactionRound,
        rubric: PromptRegistry,
    ) -> list[Candidate]:
        candidates = round_state.candidates
        if not candidates or not self.limits.semantic:
            return candidates
        all_evidence = self._compose(round_state.recipe, candidates, round_state.scaffold)
        if round_state.budget.fits(self.planner.state(all_evidence, (check,), rubric), round_state.wire):
            return candidates
        return await self._prioritize(
            check,
            round_state.base,
            candidates,
            round_state.budget,
            round_state.trace,
        )

    def _fit_candidates(
        self,
        check: Check,
        candidates: list[Candidate],
        round_state: CompactionRound,
        rubric: PromptRegistry,
    ) -> list[Candidate]:
        selected: list[Candidate] = []
        for candidate in candidates:
            proposed = self._compose(round_state.recipe, [*selected, candidate], round_state.scaffold)
            request_state = self.planner.state(proposed, (check,), rubric)
            if round_state.budget.fits(request_state, round_state.wire):
                selected.append(candidate)
            else:
                round_state.trace["omitted_candidates"].append({"id": candidate.id, "reason": "context_budget"})
        return selected

    async def compact(
        self, check: Check, previous_bytes: int, round_number: int, trace: dict[str, Any], rubric: PromptRegistry
    ) -> Evidence | None:
        round_state = self._prepare_round(check, previous_bytes, round_number, trace, rubric)
        if round_state is None:
            return None

        candidates = await self._ordered_candidates(check, round_state, rubric)
        selected = self._fit_candidates(check, candidates, round_state, rubric)
        evidence = self._compose(round_state.recipe, selected, round_state.scaffold)
        round_state.trace.update({
            "selected": [item.metadata() for item in selected],
            "state_sha256": evidence.key,
        })
        assert evidence.contains(check.target.path, *round_state.recipe.target)
        return evidence
