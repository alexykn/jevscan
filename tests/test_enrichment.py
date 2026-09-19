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
from jevscan.core.enrichment import DISPOSITIONS, EVIDENCE_FAMILIES
from jevscan.core.evaluation import evaluate_file
from jevscan.core.models import FileJob, Kind, Summary
from jevscan.core.parser import parse_source
from jevscan.core.planning import Planner
from jevscan.core.protocol import JevError, NoulAnswer
from jevscan.core.retrieval import SourceIndex
from jevscan.core.rules import ChoiceQuestion, Rule, TargetedEnrichmentPolicy

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
        families: dict[str, float] | None = None,
        relevance: float = 0.9,
        final: str = "clean",
        initial: str = "missing",
        initial_confidence: float = 0.9,
        status: int = 200,
    ) -> None:
        self.route, self.route_confidence, self.relevance = route, route_confidence, relevance
        self.final, self.initial, self.initial_confidence, self.status = (
            final,
            initial,
            initial_confidence,
            status,
        )
        self.disposition = route if route in DISPOSITIONS else "local_evidence"
        self.families = (
            families if families is not None else {name: 0.9 if name == route else 0.1 for name in EVIDENCE_FAMILIES}
        )
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        questions, state = body["questions"], body["state"]
        if any(name in questions for name in ("disposition", *EVIDENCE_FAMILIES)):
            if self.status != 200:
                return httpx.Response(self.status)
            answers = {
                name: choice(DISPOSITIONS, self.disposition, self.route_confidence)
                if name == "disposition"
                else {"type": "noul", "noul": self.families[name]}
                for name in questions
            }
        elif "candidate_context" in state:
            answers = {key: {"type": "noul", "noul": self.relevance} for key in questions}
        else:
            enriched = "supplemental_evidence" in state
            selected = self.final if enriched else self.initial
            confidence = 0.9 if enriched else self.initial_confidence
            answers = {key: choice(question["criteria"], selected, confidence) for key, question in questions.items()}
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
    assert review["evidence_families"] == ["callers"] and review["outcome"] == "reassessed"
    assert review["selected"][0]["target"]["path"] == "caller.py"
    assert review["selected"][0]["reference"]["name"] == "work"
    last = responses.requests[-1]
    assert last["questions"] == responses.requests[0]["questions"]
    assert len(last["state"]["documents"]) == 2
    assert "return work(value)" in last["state"]["documents"][0]["content"]
    assert not {"initial_answer", "route", "relevance", "expected_verdict"} & last["state"].keys()
    metrics = event["inference"]["contract"]
    assert metrics["question_bytes"] > 0 and not metrics["shared_request"]
    assert metrics["request"]["input_bytes"] > metrics["request"]["state_bytes"] > 0
    assert metrics["request"]["question_count"] == 1
    assert metrics["request"]["evidence_group_density"] == 1.0
    assert metrics["request"]["input_tokens"] == 11
    assert (summary.enrichment_reviewed, summary.enrichment_reruns, summary.enrichment_resolved) == (1, 1, 1)
    assert summary.checks_evaluated == 1 and not summary.incomplete


async def test_declared_targeted_enrichment_works_for_renamed_custom_rule(tmp_path: Path, evidence_rule: Rule) -> None:
    rule = evidence_rule.model_copy(update={"targeted_enrichment": TargetedEnrichmentPolicy(when_choices=["missing"])})
    (tmp_path / "caller.py").write_text("from target import work\ndef use(): return work(1)\n")
    responses = Responses()
    events, _, _ = await run_review(tmp_path, rule, responses)
    review = events[0]["reviews"]["contract"]
    assert review["routing_mode"] == "declared_rule_families"
    assert "disposition" not in review["predictions"][0].get("answers", {})
    assert review["outcome"] == "reassessed"


async def test_declared_targeted_enrichment_matches_admitted_trigger(tmp_path: Path, evidence_rule: Rule) -> None:
    report = evidence_rule.report.model_copy(update={"not_applicable_choices": ["clean"]})
    rule = evidence_rule.model_copy(
        update={
            "report": report,
            "enrich_on": ["applicability"],
            "targeted_enrichment": TargetedEnrichmentPolicy(when_reasons=["applicability"]),
        }
    )
    (tmp_path / "caller.py").write_text("from target import work\ndef use(): return work(1)\n")
    responses = Responses(initial="defect", initial_confidence=0.1)
    events, _, _ = await run_review(tmp_path, rule, responses)
    review = events[0]["reviews"]["contract"]
    assert review["trigger"] == "applicability" and review["initial_reason"] == "low_confidence"
    assert review["routing_mode"] == "declared_rule_families"
    assert "disposition" not in review["predictions"][0].get("answers", {})


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
    check = next(check for check in planner.checks if check.rule_id == "JEV01")
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
    assert responses.requests[-1]["state"]["retrieval_coverage"]["families"]["callers"]["discovery_complete"]

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
    assert review["retrieval"]["families"]["callers"]["matched_candidates"] == 0


async def test_malformed_auxiliary_answer_is_an_error_not_a_clean_result(tmp_path, evidence_rule):
    responses = Responses()

    def malformed(request):
        body = json.loads(request.content)
        if any(name in body["questions"] for name in ("disposition", *EVIDENCE_FAMILIES)):
            return httpx.Response(200, json={"model": "test", "answers": {"invented": {"type": "noul", "noul": 1.0}}})
        return responses(request)

    with pytest.raises(JevError, match="question IDs"):
        await run_review(tmp_path, evidence_rule, malformed)


async def test_confident_primary_answer_does_not_request_enrichment(tmp_path, evidence_rule):
    def clean(request):
        body = json.loads(request.content)
        assert "disposition" not in body["questions"]
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


@pytest.mark.parametrize("kind", ["score", "noul", "choice"])
async def test_intrinsic_uncertainty_does_not_route_or_load_the_catalogue(tmp_path, evidence_rule, kind):
    if kind == "choice":
        rule = evidence_rule
    else:
        question = {"type": kind, "instructions": "Judge the structure."}
        levels = {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.9}}
        if kind == "score":
            question["criteria"] = ["Clear", "Local", "Obscured", "Tangled"]
            levels = {
                "warning": {"min_score": 1.0, "min_confidence": 0.6},
                "error": {"min_score": 2.0, "min_confidence": 0.7},
            }
        rule = Rule.model_validate({
            "applies_to": ["function"],
            "question": question,
            "report": {
                "message": "Inspect structure",
                "levels": levels,
                **({"uncertain_range": [0.4, 0.6]} if kind == "noul" else {}),
            },
        })

    def primary_only(request):
        body = json.loads(request.content)
        assert "disposition" not in body["questions"]
        if kind == "score":
            raw = {
                "type": "score",
                "score": 1.66,
                "confidence": 0.58,
                "probabilities": {"0": 0.05, "1": 0.3, "2": 0.59, "3": 0.06},
            }
        elif kind == "noul":
            raw = {"type": "noul", "noul": 0.55}
        else:
            assert isinstance(rule.question, ChoiceQuestion)
            raw = choice(rule.question.criteria, "clean", 0.3)
        return httpx.Response(200, json={"model": "test", "answers": dict.fromkeys(body["questions"], raw)})

    events, summary, index = await run_review(tmp_path, rule, primary_only)
    assert summary.enrichment_calls == summary.enrichment_reviewed == 0 and index._catalogue is None
    assert events[0]["statuses"]["contract"] == "unknown" and not events[0]["reviews"]
    assert bool(events[0]["tentative_findings"]) is (kind != "choice")
    assert summary.findings == {"info": 0, "warning": 0, "error": 0}
    assert summary.tentative_findings["warning"] == (kind != "choice")


async def test_evidence_gaps_win_the_budget_before_earlier_optional_reviews(tmp_path, evidence_rule):
    document = evidence_rule.model_dump()
    document["question"]["criteria"]["na"] = "No applicable operation"
    document["report"]["not_applicable_choices"] = ["na"]
    document["enrich_on"] = ["missing_evidence", "reduced_context", "applicability", "low_confidence"]
    rule = Rule.model_validate(document)
    assert isinstance(rule.question, ChoiceQuestion)
    labels = rule.question.criteria
    responses = Responses("sufficient")

    def heterogeneous(request):
        body = json.loads(request.content)
        if any(name in body["questions"] for name in ("disposition", *EVIDENCE_FAMILIES)):
            return responses(request)
        answers = {}
        for key, question in body["questions"].items():
            target = question["instructions"]["target"]["qualified_name"]
            answers[key] = choice(labels, "missing" if target == "last_gap" else "na", 0.3)
        return httpx.Response(200, json={"model": "test", "answers": answers})

    source = "def first(v): return v\ndef second(v): return v\ndef last_gap(v): return v\n"
    events, summary, _ = await run_review(
        tmp_path, rule, heterogeneous, source=source, enrichment=EnrichmentConfig(max_checks_per_file=1)
    )
    assert len(responses.requests) == 1
    route = responses.requests[0]["questions"]["disposition"]["instructions"]
    assert route["target"]["qualified_name"] == "last_gap"
    assert [event["target"]["qualified_name"] for event in events] == ["first", "second", "last_gap"]
    assert [event["reviews"]["contract"]["outcome"] for event in events] == [
        "check_budget",
        "check_budget",
        "sufficient",
    ]
    assert events[-1]["reviews"]["contract"]["trigger"] == "missing_evidence"
    assert summary.enrichment_reviewed == 1


async def test_reduced_context_is_actionable_even_when_reason_is_low_confidence(tmp_path, evidence_rule):
    rule = evidence_rule.model_copy(update={"context": "file"})
    responses = Responses("sufficient")

    def low_confidence(request):
        body = json.loads(request.content)
        if any(name in body["questions"] for name in ("disposition", *EVIDENCE_FAMILIES)):
            return responses(request)
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {key: choice(rule.question.criteria, "clean", 0.3) for key in body["questions"]},
            },
        )

    source = 'PADDING = "' + "background " * 1000 + '"\ndef work(v): return v\n'
    events, summary, _ = await run_review(
        tmp_path,
        rule,
        low_confidence,
        source=source,
        evaluation=EvaluationConfig(max_context_tokens=1500, max_total_tokens=3000, token_reserve=100),
    )
    review = events[0]["reviews"]["contract"]
    assert review["trigger"] == "reduced_context" and review["initial_reason"] == "low_confidence"
    assert summary.enrichment_reviewed == 1 and summary.incomplete


async def test_context_sensitive_rule_can_opt_into_low_confidence(tmp_path, evidence_rule):
    rule = evidence_rule.model_copy(update={"enrich_on": ["low_confidence"]})
    responses = Responses("sufficient")

    def low_confidence(request):
        body = json.loads(request.content)
        if any(name in body["questions"] for name in ("disposition", *EVIDENCE_FAMILIES)):
            return responses(request)
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {key: choice(rule.question.criteria, "clean", 0.3) for key in body["questions"]},
            },
        )

    events, summary, _ = await run_review(tmp_path, rule, low_confidence)
    assert summary.enrichment_reviewed == 1 and len(responses.requests) == 1
    assert events[0]["reviews"]["contract"]["trigger"] == "low_confidence"


async def test_multiple_evidence_families_share_one_ranked_candidate_pool(tmp_path, evidence_rule):
    (tmp_path / "caller.py").write_text("def use(): return work(2)\n")
    (tmp_path / "helper.py").write_text("def helper(v): return v + 1\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_target.py").write_text("def test_work(): assert work(2) == 3\n")
    probabilities = {"callers": 0.8, "definitions": 0.8, "tests": 0.8, "enclosing_context": 0.1}
    responses = Responses(families=probabilities)
    events, summary, _ = await run_review(
        tmp_path, evidence_rule, responses, source="def work(value): return helper(value)\n"
    )
    review = events[0]["reviews"]["contract"]
    assert review["disposition"] == "local_evidence"
    assert review["evidence_families"] == ["callers", "definitions", "tests"]
    assert review["evidence_probabilities"] == probabilities
    routing_questions = responses.requests[1]["questions"]
    assert {key: value["type"] for key, value in routing_questions.items()} == {
        "disposition": "choice",
        **dict.fromkeys(EVIDENCE_FAMILIES, "noul"),
    }
    selected = review["selected"]
    assert {item["target"]["path"] for item in selected} == {"caller.py", "helper.py", "tests/test_target.py"}
    assert len({item["id"] for item in selected}) == len(selected) == 3
    shared = next(item for item in selected if item["target"]["path"] == "tests/test_target.py")
    assert review["retrieval"]["candidate_families"][shared["id"]] == ["callers", "tests"]
    assert len(responses.requests) == 4 and summary.enrichment_reruns == 1
    assert responses.requests[0]["questions"] == responses.requests[-1]["questions"]
    state = responses.requests[-1]["state"]
    assert not {"initial_answer", "disposition", "evidence_probabilities", "predictions"} & state.keys()
    assert events[0]["statuses"]["contract"] == "ok"


@pytest.mark.parametrize(
    "disposition,outcome",
    [("local_evidence", "no_evidence_family"), ("sufficient", "sufficient"), ("unavailable", "unavailable")],
)
async def test_no_family_and_disposition_stops_are_not_overridden(tmp_path, evidence_rule, disposition, outcome):
    scores = dict.fromkeys(EVIDENCE_FAMILIES, 0.1 if disposition == "local_evidence" else 0.95)
    responses = Responses(disposition, families=scores)
    events, summary, index = await run_review(tmp_path, evidence_rule, responses)
    assert events[0]["reviews"]["contract"]["outcome"] == outcome
    assert summary.enrichment_reruns == 0 and index._catalogue is None
    assert len(responses.requests) == 2


async def test_combined_families_obey_one_global_candidate_budget(tmp_path, evidence_rule):
    for i in range(5):
        (tmp_path / f"caller{i}.py").write_text(f"def use{i}(): return work({i})\n")
    (tmp_path / "helper.py").write_text("def helper(v): return v\n")
    responses = Responses(families={"callers": 0.9, "definitions": 0.8, "tests": 0.1, "enclosing_context": 0.1})
    events, _, _ = await run_review(
        tmp_path,
        evidence_rule,
        responses,
        source="def work(v): return helper(v)\n",
        enrichment=EnrichmentConfig(max_candidates=2, max_evidence=2),
    )
    review = events[0]["reviews"]["contract"]
    assert len(review["candidates"]) == 2
    assert "helper.py" in {item["target"]["path"] for item in review["selected"]}
    assert review["retrieval"]["combined_candidate_limit_omissions"] == 1
    assert review["retrieval"]["families"]["callers"]["candidate_limit_omissions"] == 3


async def test_split_routing_respects_call_budget_without_incomplete_decisions(tmp_path, evidence_rule):
    responses = Responses()
    events, summary, index = await run_review(
        tmp_path,
        evidence_rule,
        responses,
        enrichment=EnrichmentConfig(max_calls_per_file=2),
        evaluation=EvaluationConfig(max_questions=1),
    )
    review = events[0]["reviews"]["contract"]
    assert review["outcome"] == "call_budget"
    assert len(review["predictions"]) == 2
    assert summary.enrichment_calls == 2 and summary.enrichment_reruns == 0
    assert index._catalogue is None


async def test_targeted_enrichment_only_asks_rule_declared_families(tmp_path, evidence_rule):
    rule = evidence_rule.model_copy(update={"enrichment_families": ["callees"]})
    source = "def helper(v): return v\ndef work(value): return helper(value)\n"
    responses = Responses("definitions")
    events, _, _ = await run_review(tmp_path, rule, responses, source=source)
    route = next(body["questions"] for body in responses.requests if "disposition" in body["questions"])
    assert set(route) == {"disposition", "definitions"}
    assert any(event["reviews"].get("contract", {}).get("evidence_families") == ["definitions"] for event in events)


async def test_full_enrichment_mode_restores_all_evidence_families(tmp_path, evidence_rule):
    rule = evidence_rule.model_copy(update={"enrichment_families": ["callees"]})
    responses = Responses("sufficient")
    await run_review(tmp_path, rule, responses, enrichment=EnrichmentConfig(mode="full"))
    route = next(body["questions"] for body in responses.requests if "disposition" in body["questions"])
    assert set(route) == {"disposition", *EVIDENCE_FAMILIES}
