"""Real extraction and the production planner/executor with a mocked HTTP boundary."""

import json
from pathlib import Path

import httpx
import pytest

from jevscan.core.cache import AnswerCache
from jevscan.core.client import JevClient
from jevscan.core.config import Config, EvaluationConfig, JevConfig
from jevscan.core.context import ContextBuilder
from jevscan.core.evaluation import evaluate_file
from jevscan.core.models import FileJob, Summary
from jevscan.core.parser import parse_source
from jevscan.core.planning import Planner, Request
from jevscan.core.protocol import JevError
from jevscan.core.rules import Rule

pytestmark = [pytest.mark.parser, pytest.mark.usefixtures("grammar_runtime")]


class Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def configured(basic_rule: Rule, **budget) -> Config:
    return Config(
        rules={"cohesion": basic_rule},
        evaluation=EvaluationConfig(**budget),
        jev=JevConfig(requests_per_minute=0, retries=0),
    )


def planned(source: str, config: Config, language: str = "python") -> Planner:
    path = "sample.rs" if language == "rust" else "sample.py"
    parsed = parse_source(source.encode(), FileJob(path, path, language, language))
    assert not parsed.failed, parsed.diagnostics
    return Planner(ContextBuilder(parsed), config)


def answer(request: httpx.Request, probability: float = 0.95) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "model": "test",
            "answers": {key: {"type": "noul", "noul": probability} for key in reversed(body["questions"])},
        },
    )


def test_large_class_shared_once_with_independent_method_bindings(basic_rule: Rule) -> None:
    source = 'class Coordinator:\n    """BODY_SENTINEL ' + "some background. " * 2300 + '"""\n'
    source += "    def first(self): return self.second()\n    def second(self): return 1\n"
    planner = planned(source, configured(basic_rule))
    requests = list(planner.plan())
    assert len(source.encode()) > 32_000
    assert len(requests) == 1 and isinstance(requests[0], Request)
    request = requests[0]
    assert request.body.count(b"BODY_SENTINEL") == 1
    assert {check.target.qualified_name for check in request.checks} == {
        "Coordinator",
        "Coordinator.first",
        "Coordinator.second",
    }
    body = json.loads(request.body)
    assert body["state"]["documents"][0]["content"] == source.rstrip()
    for check in request.checks:
        assert body["questions"][check.id]["instructions"]["target"] == check.target.metadata()


def test_rust_owner_supplies_fields_and_sibling_impls(basic_rule: Rule) -> None:
    source = "struct S { field: i32 }\nimpl S { fn first(&self) -> i32 { self.field } }\n"
    source += "impl S { fn second(&self) -> i32 { self.first() } }\n"
    requests = list(planned(source, configured(basic_rule), "rust").plan())
    assert len(requests) == 1 and isinstance(requests[0], Request)
    request = requests[0]
    assert request.evidence.state["documents"][0]["content"] == source
    assert {check.target.qualified_name for check in request.checks} == {"impl S::first", "impl S::second"}


def test_file_and_unit_targets_share_evidence_but_not_identity(basic_rule: Rule) -> None:
    document = basic_rule.model_dump()
    file_rule = Rule.model_validate({**document, "target": "file", "context": "file", "applies_to": []})
    unit_rule = Rule.model_validate({**document, "context": "file"})
    config = configured(basic_rule).model_copy(update={"rules": {"file-check": file_rule, "unit-check": unit_rule}})
    planner = planned("GLOBAL = 1\ndef f(): return GLOBAL\n", config)
    requests = list(planner.plan())
    assert len(requests) == 1 and isinstance(requests[0], Request)
    assert {check.target.scope for check in requests[0].checks} == {"file", "unit"}
    # Top-level behavior with no lexical units is still eligible for a file judgment.
    empty_units = planned("GLOBAL = 1\n", config)
    assert len(empty_units.checks) == 1 and empty_units.checks[0].target.scope == "file"


@pytest.mark.parametrize(
    "limits",
    [
        {"max_questions": 1},
        {"max_context_tokens": 1500, "max_total_tokens": 1500, "token_reserve": 100},
        {"max_request_bytes": 2500},
    ],
)
def test_request_budgets_split_questions_not_source(basic_rule: Rule, limits: dict) -> None:
    config = configured(basic_rule, **limits).model_copy(update={"rules": {f"rule{i}": basic_rule for i in range(6)}})
    planner = planned("class S:\n    def first(self): return 1\n    def second(self): return 2\n", config)
    requests = list(planner.plan())
    assert len(requests) > 1 and all(isinstance(item, Request) for item in requests)
    sent = []
    for request in requests:
        assert isinstance(request, Request)
        assert len(request.body) <= config.evaluation.max_request_bytes
        assert len(request.checks) <= config.evaluation.max_questions
        assert planner.fits(request.evidence, request.checks)
        sent.extend(check.id for check in request.checks)
    assert sorted(sent) == sorted(check.id for check in planner.checks)
    assert len({request.evidence.encoded for request in requests if isinstance(request, Request)}) == 1


async def test_oversized_target_is_explicit_and_children_remain_evaluable(basic_rule: Rule) -> None:
    file_rule = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    config = configured(basic_rule, max_context_tokens=1500, max_total_tokens=3000, token_reserve=100)
    config = config.model_copy(update={"rules": {"owner": basic_rule, "file": file_rule}})
    source = 'class Huge:\n    """' + "context " * 10_000 + '"""\n    def work(self): return 1\n'
    planner = planned(source, config)
    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(answer)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    records = [event for event in sink.events if event["event"] == "evaluation"]
    method = next(event for event in records if event["target"]["qualified_name"] == "Huge.work")
    assert method["answers"] and method["evidence"]["owner"]["target_complete"]
    assert not method["evidence"]["owner"]["context_complete"]
    assert all(not event["answers"] and event["skipped_rules"] for event in records if event is not method)
    assert summary.units_evaluated == 1 and summary.file_targets_skipped == 1
    assert summary.checks_skipped == 2 and summary.context_reduced == 1
    assert summary.exit_code("never") == 2


async def test_strict_context_never_silently_reduces_evidence(basic_rule: Rule) -> None:
    config = configured(
        basic_rule, max_context_tokens=1500, max_total_tokens=3000, token_reserve=100, oversized_context="skip"
    )
    source = 'class Huge:\n    """' + "context " * 10_000 + '"""\n    def work(self): return 1\n'
    sink, summary = Sink(), Summary("live")

    def never(_request):
        raise AssertionError("strict local rejection must not make an HTTP request")

    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(never)) as client:
        await evaluate_file(planned(source, config), client, None, sink, summary)
    assert summary.checks_evaluated == 0 and summary.checks_skipped == 2 and summary.incomplete
    assert not any(event.get("context_selection", {}).get("compactions") for event in sink.events)


async def test_provider_rejection_splits_questions_and_attributes_answers_once(basic_rule: Rule) -> None:
    source = "class S:\n" + "".join(f"    def method{i}(self): return {i}\n" for i in range(7))
    config = configured(basic_rule)
    planner = planned(source, config)

    async def handle(request: httpx.Request) -> httpx.Response:
        if len(json.loads(request.content)["questions"]) > 2:
            return httpx.Response(400, json={"error": {"code": "max_tokens_exceeded"}})
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
        assert 1 < client.requests <= 2 * len(planner.checks) - 1
    assert summary.checks_evaluated == len(planner.checks) and not summary.incomplete
    records = [event for event in sink.events if event["event"] == "evaluation"]
    assert len({event["target"]["id"] for event in records}) == len(planner.checks)


async def test_provider_state_rejection_reduces_to_owner_without_cutting_target(basic_rule: Rule) -> None:
    rule = Rule.model_validate({**basic_rule.model_dump(), "context": "file"})
    config = configured(rule)
    source = 'PADDING = "' + "background " * 4000 + '"\nclass S:\n    def work(self): return 1\n'
    planner = planned(source, config)
    accepted = []

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if len(body["state"]["documents"][0]["content"]) > 1000:
            return httpx.Response(413)
        accepted.append(body)
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert accepted and all(body["state"]["documents"][0]["content"].startswith("class S:") for body in accepted)
    assert summary.units_evaluated == 2 and summary.context_reduced == 2 and summary.incomplete


async def test_unrecoverable_provider_size_error_is_bounded(basic_rule: Rule) -> None:
    file_rule = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    config = configured(file_rule)
    planner = planned("GLOBAL = 1\n", config)
    sink, summary = Sink(), Summary("live")
    async with JevClient(
        config.jev, "test-key", transport=httpx.MockTransport(lambda _r: httpx.Response(413))
    ) as client:
        await evaluate_file(planner, client, None, sink, summary)
        assert client.requests == 1
    assert summary.file_targets_skipped == 1 and summary.checks_evaluated == 0 and summary.incomplete


async def test_cache_reclassifies_thresholds_and_invalidates_changed_evidence(basic_rule: Rule, tmp_path: Path) -> None:
    config = configured(basic_rule)
    original = "class S:\n    def work(self): return self.field\n    field = 1\n"
    changed = original.replace("field = 1", "field = 2")
    stricter = basic_rule.model_dump()
    stricter["report"]["levels"]["error"]["min_probability"] = 0.90
    new_config = config.model_copy(update={"rules": {"cohesion": Rule.model_validate(stricter)}})
    async with AnswerCache(tmp_path / "answers.db", 3600) as cache:
        for source, settings, requests, severity in (
            (original, config, 1, "warning"),
            (original, new_config, 0, "error"),
            (changed, new_config, 1, "error"),
        ):
            sink, summary = Sink(), Summary("live")
            async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(answer)) as client:
                await evaluate_file(planned(source, settings), client, cache, sink, summary)
                assert client.requests == requests
            assert summary.findings[severity] == 2
            assert summary.units_cached == (2 if not requests else 0)


async def test_out_of_order_answers_keep_method_attribution(basic_rule: Rule) -> None:
    rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"]})
    config = configured(rule)
    planner = planned("class S:\n    def good(self): return 1\n    def bad(self): return 2\n", config)

    async def handle(request: httpx.Request) -> httpx.Response:
        questions = json.loads(request.content)["questions"]
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {
                    key: {
                        "type": "noul",
                        "noul": 0.99 if question["instructions"]["target"]["qualified_name"].endswith("bad") else 0.1,
                    }
                    for key, question in reversed(list(questions.items()))
                },
            },
        )

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
        assert client.requests == 1
    assert [(event["target"]["qualified_name"], event["statuses"]["cohesion"]) for event in sink.events] == [
        ("S.good", "ok"),
        ("S.bad", "error"),
    ]


async def test_failure_retains_prior_results_and_marks_unanswered_checks(basic_rule: Rule) -> None:
    rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"]})
    config = configured(rule, max_questions=1)
    planner = planned("class S:\n    def first(self): return 1\n    def second(self): return 2\n", config)
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return answer(request) if calls == 1 else httpx.Response(500)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(JevError):
            await evaluate_file(planner, client, None, sink, summary)
    assert summary.checks_evaluated == 1 and summary.checks_skipped == 1 and summary.units_failed == 1
    records = [event for event in sink.events if event["event"] == "evaluation"]
    assert records[0]["answers"] and not records[1]["answers"]
    assert summary.incomplete


def test_utf8_target_ranges_and_exact_request_byte_accounting(basic_rule: Rule) -> None:
    source = 'LABEL = "日本語 λ"\nclass Café:\n    def méthode(self): return LABEL\n'
    planner = planned(source, configured(basic_rule, max_questions=1))
    for request in planner.plan():
        assert isinstance(request, Request)
        assert planner.estimate(request.evidence, request.checks)[2] == len(request.body)
        body = json.loads(request.body)
        document = body["state"]["documents"][0]
        evidence = document["content"].encode("utf-8")
        for question in body["questions"].values():
            target = question["instructions"]["target"]
            relative = target["start_byte"] - document["start_byte"]
            length = target["end_byte"] - target["start_byte"]
            assert evidence[relative : relative + length] == source.encode()[target["start_byte"] : target["end_byte"]]
            assert target["qualified_name"] in {"Café", "Café.méthode"}


def test_many_owner_envelopes_keep_correct_sources_when_reused(basic_rule: Rule) -> None:
    source = "\n".join(f"class C{i}:\n    def value(self): return {i}\n" for i in range(12))
    planner = planned(source, configured(basic_rule))
    requests = list(planner.plan())
    assert len(requests) == 12
    for index, request in enumerate(requests):
        assert isinstance(request, Request)
        assert request.evidence.state["documents"][0]["content"].startswith(f"class C{index}:")
        assert {check.target.qualified_name for check in request.checks} == {f"C{index}", f"C{index}.value"}


async def test_tentative_severity_reclassifies_from_cache_without_double_counting(tmp_path, basic_rule):
    rule = Rule.model_validate({
        "applies_to": ["function"],
        "question": {
            "type": "score",
            "instructions": "How bad?",
            "criteria": ["Clear", "Local", "Obscured", "Tangled"],
        },
        "report": {
            "message": "Review flow.",
            "levels": {
                "warning": {"min_score": 1.0, "min_confidence": 0.6},
                "error": {"min_score": 2.0, "min_confidence": 0.7},
            },
        },
    })
    config = configured(basic_rule).model_copy(update={"rules": {"flow": rule}})
    source = "def work(): return 1\n"
    calls = []

    def raw_score(request):
        calls.append(request)
        questions = json.loads(request.content)["questions"]
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {
                    key: {
                        "type": "score",
                        "score": 2.4,
                        "confidence": 0.65,
                        "probabilities": {"0": 0.01, "1": 0.04, "2": 0.49, "3": 0.46},
                    }
                    for key in questions
                },
            },
        )

    async with AnswerCache(tmp_path / "answers.sqlite3", 3600) as cache:
        for confidence, expected in ((0.7, "unknown"), (0.65, "error")):
            doc = rule.model_dump()
            doc["report"]["levels"]["error"]["min_confidence"] = confidence
            current = config.model_copy(update={"rules": {"flow": Rule.model_validate(doc)}})
            sink, summary = Sink(), Summary("live")
            async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(raw_score)) as client:
                await evaluate_file(planned(source, current), client, cache, sink, summary)
            event = next(event for event in sink.events if event["event"] == "evaluation")
            assert event["statuses"]["flow"] == expected and summary.checks_evaluated == 1
            assert summary.uncertain == (expected == "unknown")
            assert summary.tentative_findings["error"] == (expected == "unknown")
            assert summary.findings["error"] == (expected == "error")
            assert summary.findings["warning"] == 0
            assert bool(event["tentative_findings"]) is (expected == "unknown")
            assert summary.exit_code("warning") == summary.exit_code("error") == (expected == "error")
            if expected == "error":
                assert event["cached"] and summary.cache_hits == 1
    assert len(calls) == 1


async def test_large_complete_file_is_attempted_before_any_compaction(basic_rule):
    rule = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    config = configured(rule)
    source = "# " + "context with words " * 8000 + "\ndef f(): return 1\n"
    planner = planned(source, config)
    assert planner.estimate(planner.context.requested(planner.checks[0]), planner.checks)[0] > 46_000
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert len(bodies) == 1 and bodies[0]["state"]["documents"][0]["content"] == source
    assert summary.context_reduced == summary.compaction_calls == summary.checks_skipped == 0


async def test_rejected_source_is_not_reprobed_for_every_method(basic_rule):
    source = 'class Large:\n    """' + "background " * 10000 + '"""\n'
    source += "".join(f"    def f{i}(self): return {i}\n" for i in range(24))
    planner = planned(source, configured(basic_rule))
    rejected, compact = [], []

    def handle(request):
        body = json.loads(request.content)
        if sum(len(doc["content"]) for doc in body["state"]["documents"]) > 10_000:
            rejected.append(body)
            return httpx.Response(422, json={"error": "max_tokens_exceeded"})
        compact.append(body)
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(configured(basic_rule).jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert len(rejected) == 2  # one batch and one smallest-question probe
    assert len(compact) == 24 and summary.units_evaluated == 24
    assert summary.units_skipped == 1 and summary.checks_skipped == 1
    coverage = [event for event in sink.events if event["event"] == "coverage"]
    assert len(coverage) == 1 and coverage[0]["context_reduced_targets"] == 24


async def test_aggregate_size_failure_splits_without_losing_or_duplicating_answers(basic_rule):
    config = configured(basic_rule).model_copy(update={"rules": {f"R{i}": basic_rule for i in range(9)}})
    source = "def f(): return 1\n"
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(413) if len(body["questions"]) > 2 else answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planned(source, config), client, None, sink, summary)
    assert summary.checks_evaluated == 9 and summary.checks_skipped == summary.context_reduced == 0
    assert all(body["state"]["documents"][0]["content"] == source for body in calls)
    assert len({json.dumps(body, sort_keys=True) for body in calls}) == len(calls)
    assert len(calls) <= 18


async def test_compaction_retains_referenced_source_and_fields_not_unrelated_bodies(basic_rule):
    source = (
        "import math\nLIMIT = 7\ndef validate(value): return value < LIMIT\n"
        "class Owner:\n    counter = 0\n    def __init__(self): self.counter = 1\n"
        "    def work(self, value): return validate(value) and self.counter\n"
        '    def irrelevant(self):\n        """' + "unrelated " * 7000 + '"""\n        return 99\n'
    )
    rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"], "context": "file"})
    config = configured(rule, max_context_tokens=2500, max_total_tokens=5000, token_reserve=100)
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planned(source, config), client, None, sink, summary)
    actual = next(
        body
        for body in bodies
        if any(
            question["instructions"]["target"]["qualified_name"] == "Owner.work"
            for question in body["questions"].values()
        )
    )
    content = "\n".join(document["content"] for document in actual["state"]["documents"])
    assert "def work(self, value): return validate(value) and self.counter" in content
    assert "def validate(value): return value < LIMIT" in content and "LIMIT = 7" in content
    assert "self.counter = 1" in content and "counter = 0" in content and "import math" in content
    assert "unrelated unrelated" not in content
    assert not actual["state"]["coverage"]["file_complete"]
    assert summary.compaction_calls == 0  # known dependencies fit without asking a model


async def test_permanent_rejection_has_bounded_progress_and_never_scores_a_file_subset(basic_rule):
    file_rule = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    config = configured(basic_rule).model_copy(update={"rules": {"file": file_rule, "unit": basic_rule}})
    source = 'VALUE = "' + "λ" * 20000 + '"\ndef f(): return 1\n'
    bodies = []

    def handle(request):
        bodies.append(request.content)
        return httpx.Response(400, json={"error": {"code": "max_tokens_exceeded"}})

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planned(source, config), client, None, sink, summary)
    assert len(bodies) <= 5 and len(set(bodies)) == len(bodies)
    assert summary.checks_evaluated == 0 and summary.checks_skipped == 2
    for raw in bodies:
        body = json.loads(raw)
        if any(question["instructions"]["target"]["scope"] == "file" for question in body["questions"].values()):
            assert body["state"]["coverage"]["file_complete"]
    assert summary.incomplete and summary.exit_code("never") == 2


async def test_semantic_compaction_questions_bind_custom_yaml_rule_and_stop_at_call_limit(tmp_path, basic_rule):
    import yaml

    from jevscan.core.config import load_config

    source = (
        'def allowed(x):\n    """'
        + "allowed background " * 210
        + '"""\n    return x > 0\n'
        + 'def audited(x):\n    """'
        + "audited background " * 210
        + '"""\n    return x\n'
        + "def work(x): return allowed(x) and audited(x)\n"
    )
    rule = basic_rule.model_dump(mode="json", exclude_none=True)
    rule.update({"name": "TEAM01", "context": "file", "applies_to": ["function"]})
    rule["question"]["instructions"] = "Does the target satisfy the special TEAM contract?"
    document = {
        "version": 4,
        "rules": [rule],
        "lint": {"select": ["TEAM01"]},
        "evaluation": {"max_context_tokens": 2300, "max_total_tokens": 4600, "token_reserve": 100},
        "compaction": {"max_calls_per_file": 1},
        "jev": {"requests_per_minute": 0},
    }
    path = tmp_path / "jevscan.yaml"
    path.write_text(yaml.safe_dump(document))
    config = load_config([tmp_path], explicit=path).config
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planned(source, config), client, None, sink, summary)
    selectors = [body for body in calls if "candidate_context" in body["state"]]
    assert len(selectors) == summary.compaction_calls == 1
    for question in selectors[0]["questions"].values():
        instructions = question["instructions"]
        assert instructions["rule"] == config.rules["TEAM01"].question.model_dump(mode="json")
        assert instructions["target"]["scope"] == "unit"
        assert "JEV04" not in json.dumps(instructions)
    results = [event for event in sink.events if event["event"] == "evaluation"]
    work = next(event for event in results if event["target"]["qualified_name"] == "work")
    assert work["evidence"]["TEAM01"]["target_complete"] and summary.checks_evaluated == 3
    assert not any("initial_answer" in body["state"] or "predictions" in body["state"] for body in calls)


async def test_one_rejected_file_rule_cannot_silently_omit_other_file_rules(basic_rule):
    first = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    second_doc = first.model_dump()
    second_doc["question"]["instructions"] = "A different, longer question with a potentially different tokenization."
    second = Rule.model_validate(second_doc)
    config = configured(first).model_copy(update={"rules": {"A": first, "B": second}})

    def handle(request):
        body = json.loads(request.content)
        if (
            len(body["questions"]) > 1
            or next(iter(body["questions"].values()))["instructions"]["task"] != second.question.instructions
        ):
            return httpx.Response(413)
        assert body["state"]["coverage"]["file_complete"]
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planned("DATA = 1\n", config), client, None, sink, summary)
    event = next(event for event in sink.events if event["event"] == "evaluation")
    assert set(event["answers"]) == {"B"} and set(event["skipped_rules"]) == {"A"}


async def test_equal_compacted_context_still_batches_independent_rules(basic_rule):
    rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"]})
    config = configured(rule).model_copy(update={"rules": {f"R{i}": rule for i in range(3)}})
    source = 'class Big:\n    """' + "irrelevant " * 5000 + '"""\n'
    source += "".join(f"    def f{i}(self): return {i}\n" for i in range(3))
    accepted = []

    def handle(request):
        body = json.loads(request.content)
        if len(request.content) > 20_000:
            return httpx.Response(413)
        accepted.append(body)
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planned(source, config), client, None, sink, summary)
    assert any(len(body["questions"]) > 1 for body in accepted)
    assert sum(len(body["questions"]) for body in accepted) == 9 == summary.checks_evaluated
    assert len(accepted) < 9 and summary.checks_skipped == 0
