"""Exercise the real route/retrieve/select/reassess path without provider credentials."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from jevscan.core.assessment import assess
from jevscan.core.cache import AnswerCache
from jevscan.core.client import JevClient
from jevscan.core.config import Config, EnrichmentConfig, EvaluationConfig, JevConfig, load_config
from jevscan.core.context import ContextBuilder
from jevscan.core.enrichment import ROUTES
from jevscan.core.evaluation import evaluate_file
from jevscan.core.models import FileJob, Kind, Summary
from jevscan.core.parser import parse_source
from jevscan.core.planning import Planner
from jevscan.core.protocol import JevError, NoulAnswer
from jevscan.core.retrieval import SourceIndex
from jevscan.core.rules import Rule

pytestmark = [pytest.mark.parser, pytest.mark.usefixtures("grammar_runtime")]


@pytest.fixture
def evidence_rule() -> Rule:
    return Rule.model_validate({
        "applies_to": ["function"],
        "context": "unit",
        "require_body": True,
        "question": {
            "type": "choice",
            "instructions": "Is the input contract visibly established?",
            "criteria": {
                "defect": "The contract is violated.",
                "clean": "The behavior is justified.",
                "missing": "Relevant code is absent.",
            },
        },
        "report": {
            "message": "Contract violation",
            "choices": ["defect"],
            "uncertain_choices": ["missing"],
            "levels": {
                "warning": {"min_probability": 0.6, "min_confidence": 0.5},
                "error": {"min_probability": 0.9, "min_confidence": 0.7},
            },
        },
    })


def choice(labels, selected: str, confidence: float = 0.9) -> dict:
    return {
        "type": "choice",
        "choice": selected,
        "confidence": confidence,
        "probabilities": {key: 0.95 if key == selected else 0.05 / (len(labels) - 1) for key in labels},
    }


def plan(source: str, config: Config) -> Planner:
    parsed = parse_source(source.encode(), FileJob("target.py", "target.py", "python", "python"))
    assert not parsed.failed
    return Planner(ContextBuilder(parsed), config)


class Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


class Responses:
    def __init__(
        self,
        route: str = "callers",
        *,
        route_confidence: float = 0.9,
        relevance: float = 0.9,
        final: str = "clean",
        status: int = 200,
    ) -> None:
        self.route, self.route_confidence, self.relevance = route, route_confidence, relevance
        self.final, self.status = final, status
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        questions, state = body["questions"], body["state"]
        if "route" in questions:
            if self.status != 200:
                return httpx.Response(self.status)
            answers = {"route": choice(ROUTES, self.route, self.route_confidence)}
        elif "candidate_context" in state:
            answers = {key: {"type": "noul", "noul": self.relevance} for key in questions}
        else:
            selected = self.final if "supplemental_evidence" in state else "missing"
            answers = {key: choice(question["criteria"], selected) for key, question in questions.items()}
        return httpx.Response(200, json={"model": "jev-test", "answers": answers, "usage": {"input_tokens": 11}})


async def run_review(
    tmp_path: Path,
    rule: Rule,
    responses: Callable[[httpx.Request], httpx.Response],
    *,
    cache=None,
    enrichment=None,
    evaluation=None,
    source="def work(value):\n    return value + 1\n",
):
    config = Config(
        rules={"contract": rule},
        jev=JevConfig(requests_per_minute=0, retries=0),
        enrichment=enrichment or EnrichmentConfig(),
        evaluation=evaluation or EvaluationConfig(),
    )
    (tmp_path / "target.py").write_text(source)
    planner = plan(source, config)
    sink, summary = Sink(), Summary("live")
    index = SourceIndex(tmp_path, config.scan, config.enrichment)
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(responses)) as client:
        await evaluate_file(planner, client, cache, sink, summary, index)
    return [event for event in sink.events if event["event"] == "evaluation"], summary, index


async def test_closed_routing_relevance_and_fresh_judgment(tmp_path: Path, evidence_rule: Rule) -> None:
    caller = "from target import work\ndef checked(value):\n    assert isinstance(value, int)\n    return work(value)\n"
    (tmp_path / "caller.py").write_text(caller)
    responses = Responses()
    events, summary, _ = await run_review(tmp_path, evidence_rule, responses)
    event = events[0]
    assert len(responses.requests) == 4  # initial + route + batched relevance + original question
    assert event["statuses"]["contract"] == "ok"
    review = event["reviews"]["contract"]
    assert review["initial_answer"]["choice"] == "missing"
    assert review["route"] == "callers" and review["outcome"] == "reassessed"
    assert review["selected"][0]["target"]["path"] == "caller.py"
    assert review["selected"][0]["reference"]["name"] == "work"
    last = responses.requests[-1]
    assert last["questions"] == responses.requests[0]["questions"]
    assert len(last["state"]["documents"]) == 2
    assert "return work(value)" in last["state"]["documents"][0]["content"]
    assert not {"initial_answer", "route", "relevance", "expected_verdict"} & last["state"].keys()
    assert (summary.enrichment_reviewed, summary.enrichment_reruns, summary.enrichment_resolved) == (1, 1, 1)
    assert summary.checks_evaluated == 1 and not summary.incomplete


@pytest.mark.parametrize(
    "route,confidence,outcome,status",
    [
        ("sufficient", 0.9, "sufficient", "unknown"),
        ("not_applicable", 0.9, "not_applicable", "not_applicable"),
        ("unavailable", 0.9, "unavailable", "unknown"),
        ("callers", 0.1, "route_uncertain", "unknown"),
    ],
)
async def test_stopping_routes_do_not_read_other_files(tmp_path, evidence_rule, route, confidence, outcome, status):
    responses = Responses(route, route_confidence=confidence)
    events, summary, index = await run_review(tmp_path, evidence_rule, responses)
    assert len(responses.requests) == 2 and index._catalogue is None
    assert events[0]["reviews"]["contract"]["outcome"] == outcome
    assert events[0]["statuses"]["contract"] == status
    assert summary.enrichment_reruns == 0


@pytest.mark.parametrize(
    "relevance,final,outcome", [(0.3, "clean", "no_relevant_evidence"), (0.9, "missing", "reassessed")]
)
async def test_no_forced_selection_or_recursive_confidence_hunting(tmp_path, evidence_rule, relevance, final, outcome):
    (tmp_path / "caller.py").write_text("from target import work\ndef use(): return work(1)\n")
    responses = Responses(relevance=relevance, final=final)
    events, _, _ = await run_review(tmp_path, evidence_rule, responses)
    assert events[0]["statuses"]["contract"] == "unknown"
    assert events[0]["reviews"]["contract"]["outcome"] == outcome
    assert len(responses.requests) == (3 if relevance < 0.65 else 4)


async def test_changed_caller_invalidates_selection_and_final_but_reuses_initial_cache(tmp_path, evidence_rule):
    caller = tmp_path / "caller.py"
    caller.write_text("from target import work\ndef use(): return work(1)\n")
    async with AnswerCache(tmp_path / "cache.sqlite3", 3600) as cache:
        first = Responses()
        await run_review(tmp_path, evidence_rule, first, cache=cache)
        second = Responses()
        events, summary, _ = await run_review(tmp_path, evidence_rule, second, cache=cache)
        assert second.requests == [] and summary.enrichment_cache_hits == 3
        assert events[0]["cached"]
        caller.write_text("from target import work\ndef use(): return work(2)\n")
        third = Responses(final="defect")
        events, summary, _ = await run_review(tmp_path, evidence_rule, third, cache=cache)
        assert len(third.requests) == 2  # router and original evidence still cached
        assert events[0]["statuses"]["contract"] == "error" and summary.findings["error"] == 1
        assert summary.cache_hits == 2


async def test_limits_are_explicit_and_enrichment_cannot_hide_failures(tmp_path, evidence_rule):
    responses = Responses()
    events, _, index = await run_review(
        tmp_path, evidence_rule, responses, enrichment=EnrichmentConfig(max_calls_per_file=1)
    )
    assert len(responses.requests) == 2 and index._catalogue is None
    assert events[0]["reviews"]["contract"]["outcome"] == "call_budget"
    responses = Responses(status=413)
    events, summary, _ = await run_review(tmp_path, evidence_rule, responses)
    assert events[0]["reviews"]["contract"]["outcome"] == "provider_context_limit"
    assert summary.enrichment_reruns == 0
    with pytest.raises(JevError, match="HTTP 500"):
        await run_review(tmp_path, evidence_rule, Responses(status=500))


async def test_check_budget_and_rule_opt_out(tmp_path, evidence_rule):
    source = "def work(v): return v\ndef another(v): return v\n"
    responses = Responses("sufficient")
    events, summary, _ = await run_review(
        tmp_path, evidence_rule, responses, source=source, enrichment=EnrichmentConfig(max_checks_per_file=1)
    )
    assert [event["reviews"]["contract"]["outcome"] for event in events] == ["sufficient", "check_budget"]
    assert summary.enrichment_reviewed == 1
    responses = Responses()
    events, _, index = await run_review(tmp_path, evidence_rule.model_copy(update={"enrich": False}), responses)
    assert len(responses.requests) == 1 and not events[0]["reviews"] and index._catalogue is None


def test_default_applicability_and_noul_uncertainty(tmp_path):
    config = load_config([tmp_path], cwd=tmp_path).config
    planner = plan(
        """from typing import Protocol
class EmptyError(Exception): pass
class Contract(Protocol):
    def run(self): ...
class Actual:
    def run(self): return 1
""",
        config,
    )
    assert not any(check.target.qualified_name == "Contract.run" for check in planner.checks)
    assert not any(
        check.rule_id == "unhelpful-decomposition" and check.target.qualified_name != "Actual"
        for check in planner.checks
    )
    check = next(check for check in planner.checks if check.rule_id == "mixed-responsibilities")
    for value, status in (
        (0.1, "ok"),
        (0.4, "unknown"),
        (0.51, "unknown"),
        (0.599, "unknown"),
        (0.6, "warning"),
        (0.95, "error"),
    ):
        result = assess(check, NoulAnswer(type="noul", noul=value))
        assert result.status == status
        if status == "unknown":
            assert result.reason == "probability_ambiguous"


async def test_candidate_batches_obey_question_budget_and_preserve_overlap(tmp_path, evidence_rule):
    for index in range(3):
        (tmp_path / f"caller{index}.py").write_text(f"def use(): return work({index})\n")
    responses = Responses()
    events, summary, _ = await run_review(
        tmp_path, evidence_rule, responses, evaluation=EvaluationConfig(max_questions=1)
    )
    selections = [body for body in responses.requests if "candidate_context" in body["state"]]
    assert len(selections) == 3 and all(len(body["questions"]) == 1 for body in selections)
    assert len(events[0]["reviews"]["contract"]["selected"]) == 3
    assert summary.enrichment_reruns == 1
    assert responses.requests[-1]["state"]["retrieval_coverage"]["discovery_complete"]

    # Owner/file candidates overlap. The final document is the union, not duplicate source.
    source = 'class Owner:\n    label = "λ"\n    def work(self): return self.label\n'
    rule = evidence_rule.model_copy(update={"context": "unit", "applies_to": [Kind.METHOD]})
    responses = Responses("enclosing_context")
    events, _, index = await run_review(tmp_path, rule, responses, source=source)
    assert index._catalogue is None
    state = responses.requests[-1]["state"]
    assert len(state["documents"]) == 1 and state["documents"][0]["content"] == source
    assert state["coverage"]["file_complete"]
    assert events[0]["evidence"]["contract"]["context_complete"]


async def test_no_candidate_means_no_selection_or_reassessment(tmp_path, evidence_rule):
    responses = Responses()
    events, _, _ = await run_review(tmp_path, evidence_rule, responses)
    assert len(responses.requests) == 2
    review = events[0]["reviews"]["contract"]
    assert review["outcome"] == "no_relevant_evidence"
    assert review["retrieval"]["matched_candidates"] == 0


async def test_malformed_auxiliary_answer_is_an_error_not_a_clean_result(tmp_path, evidence_rule):
    responses = Responses()

    def malformed(request):
        body = json.loads(request.content)
        if "route" in body["questions"]:
            return httpx.Response(200, json={"model": "test", "answers": {"invented": {"type": "noul", "noul": 1.0}}})
        return responses(request)

    with pytest.raises(JevError, match="question IDs"):
        await run_review(tmp_path, evidence_rule, malformed)


async def test_confident_primary_answer_does_not_request_enrichment(tmp_path, evidence_rule):
    def clean(request):
        body = json.loads(request.content)
        assert "route" not in body["questions"]
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {key: choice(question["criteria"], "clean") for key, question in body["questions"].items()},
            },
        )

    events, summary, index = await run_review(tmp_path, evidence_rule, clean)
    assert events[0]["statuses"]["contract"] == "ok"
    assert not events[0]["reviews"] and summary.enrichment_calls == 0 and index._catalogue is None
