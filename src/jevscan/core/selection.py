"""Rule-derived independent relevance judgments shared by compaction and enrichment."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from jevscan.core.inference import Prediction
from jevscan.core.planning import RequestBudget
from jevscan.core.protocol import Check, ContextLimitError, NoulAnswer, auxiliary_questions, encode
from jevscan.core.rules import NoulQuestion, Question


class EvidenceCandidate(Protocol):
    @property
    def id(self) -> str: ...
    def preview(self) -> dict[str, Any]: ...
    def metadata(self) -> dict[str, Any]: ...


SelectionCall = Callable[[bytes, dict[str, Question], dict[str, bytes]], Awaitable[Prediction]]


def selection_request(
    check: Check, state: dict[str, Any], candidates: Sequence[EvidenceCandidate]
) -> tuple[bytes, dict[str, Question], dict[str, bytes]]:
    questions: dict[str, Question] = {
        candidate.id: NoulQuestion(
            type="noul",
            instructions=(
                f"Would the complete source identified by `{candidate.id}` in `candidate_context` provide concrete "
                "evidence useful for deciding the supplied rule about the exact supplied target? "
                "Apply that rule's actual instructions, criteria and exclusions, not its name or a generic code-quality notion. "
                "Judge relevance to either outcome, not support for a defect. Several candidates or none may help. "
                "A matching short name, evidence already present or an unrelated operation is insufficient. "
                "Previews can be partial; do not invent their omitted contents. Low relevance is not proof of safe omission."
            ),
        )
        for candidate in candidates
    }
    body_state = encode({**state, "candidate_context": [candidate.preview() for candidate in candidates]})
    return body_state, questions, auxiliary_questions(check, questions)


async def rank_candidates(
    check: Check,
    state: dict[str, Any],
    candidates: Sequence[EvidenceCandidate],
    budget: RequestBudget,
    call: SelectionCall,
    trace: dict[str, Any],
) -> dict[str, float]:
    """Split auxiliary questions on size rejection; an unrankable candidate stays unranked."""
    probabilities: dict[str, float] = {}

    async def evaluate(batch: Sequence[EvidenceCandidate]) -> None:
        encoded, questions, wire = selection_request(check, state, batch)
        try:
            prediction = await call(encoded, questions, wire)
        except ContextLimitError:
            if len(batch) > 1:
                middle = len(batch) // 2
                await evaluate(batch[:middle])
                await evaluate(batch[middle:])
            else:
                trace["omitted_candidates"].append({"id": batch[0].id, "reason": "selection_provider_limit"})
            return
        for candidate in batch:
            answer = prediction.response.answers[candidate.id]
            assert isinstance(answer, NoulAnswer)
            probabilities[candidate.id] = answer.noul
            trace["candidates"].append({**candidate.metadata(), "relevance": answer.noul})

    pending: list[EvidenceCandidate] = []
    for candidate in candidates:
        encoded, _, wire = selection_request(check, state, [*pending, candidate])
        if pending and not budget.fits(encoded, wire):
            await evaluate(pending)
            pending = []
        encoded, _, wire = selection_request(check, state, [candidate])
        if budget.fits(encoded, wire):
            pending.append(candidate)
        else:
            trace["omitted_candidates"].append({"id": candidate.id, "reason": "selection_budget"})
    if pending:
        await evaluate(pending)
    return probabilities
