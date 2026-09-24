"""Real extraction and the production planner/executor with a mocked HTTP boundary."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from jevscan.core.cache import AnswerCache, judgment_cache_key
from jevscan.core.client import JevClient
from jevscan.core.config import Config, EvaluationConfig, JevConfig, load_config
from jevscan.core.context import ContextBuilder
from jevscan.core.evaluation import evaluate_file
from jevscan.core.model_limits import TokenCalibration
from jevscan.core.models import FileJob, Summary
from jevscan.core.parser import parse_source
from jevscan.core.planning import Planner, Request
from jevscan.core.protocol import JevError, PromptRegistry, rubric_reference, validate_prompt_registry
from jevscan.core.rules import ChoiceQuestion, Rule, ScoreQuestion

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


def test_default_batching_shares_evidence_across_more_than_64_questions(basic_rule: Rule) -> None:
    source = "".join(f"def item_{i}(): return {i}\n" for i in range(80))
    planner = planned(source, configured(basic_rule))
    requests = list(planner.plan())
    assert len(planner.checks) == 80
    assert len(requests) == 1
    assert len(requests[0].checks) == 80
    assert planner.fits(requests[0].evidence, requests[0].checks)


def test_primary_locator_byte_spans_distinguish_same_line_duplicate_names(basic_rule: Rule) -> None:
    planner = planned("function duplicate(){} function duplicate(){}\n", configured(basic_rule), "javascript")
    requests = list(planner.plan())
    assert len(requests) == 1
    checks = requests[0].checks
    assert len(checks) == 2
    assert {check.target.qualified_name for check in checks} == {"duplicate"}
    assert {check.target.start_line for check in checks} == {1}
    locators = [json.loads(requests[0].question_wires[check.id])["instructions"]["target"] for check in checks]

    assert {tuple(locator["span"]) for locator in locators} == {
        (check.target.start_byte, check.target.end_byte) for check, locator in zip(checks, locators, strict=True)
    }
    assert len({tuple(locator["span"]) for locator in locators}) == 2
    assert len({locator["path"] for locator in locators}) == 1
    assert len({locator["name"] for locator in locators}) == 1
    assert len({locator["start_line"] for locator in locators}) == 1
    assert len({locator["end_line"] for locator in locators}) == 1
    assert len({locator["kind"] for locator in locators}) == 1


def answer(request: httpx.Request, probability: float = 0.95) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "model": "test",
            "answers": {key: {"type": "noul", "noul": probability} for key in reversed(body["questions"])},
        },
    )


async def test_declared_applicability_is_generic_for_renamed_custom_rules(basic_rule: Rule) -> None:
    rule = Rule.model_validate({
        **basic_rule.model_dump(),
        "applicability": {"requires_any": ["fallback_candidate"]},
    })
    config = Config(
        rules={"RENAMED01": rule},
        evaluation=EvaluationConfig(),
        jev=JevConfig(requests_per_minute=0, retries=0),
    )
    planner = planned("def work(value): return value + 1\n", config)
    assert planner.applicability_skips
    sink, summary = Sink(), Summary("live")

    def no_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an inapplicable target must not call Jev")

    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(no_request)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    event = next(event for event in sink.events if event["event"] == "evaluation")
    assert event["applicability_skips"]["RENAMED01"].endswith("(fallback_candidate)")
    assert not event["skipped_rules"]
    assert summary.applicability_skips == 1
    assert summary.not_applicable == 1 and summary.checks_skipped == 0
    assert not summary.incomplete


def test_declared_applicability_is_generic_for_file_rules(basic_rule: Rule) -> None:
    rule = Rule.model_validate({
        **basic_rule.model_dump(),
        "target": "file",
        "context": "file",
        "applies_to": [],
        "applicability": {"requires_any": ["fallback_candidate"]},
    })
    config = Config(rules={"CUSTOM_FILE": rule})
    source = "VALUE = 1\n"
    planner = planned(source, config)
    assert not planner.checks
    assert planner.applicability_skips[planner.context.file.id]["CUSTOM_FILE"].endswith("(fallback_candidate)")


def test_rust_empty_implementations_reach_body_required_planning(basic_rule: Rule) -> None:
    rule = basic_rule.model_copy(
        update={
            "applies_to": ["function", "method"],
            "require_body": True,
        }
    )
    source = 'trait Store { fn required(&self); fn defaulted(&self) {} }\nextern "C" { fn ffi(); }\nfn empty() {}\n'
    planner = planned(source, configured(rule), "rust")
    selected = {check.target.qualified_name for check in planner.checks}
    assert {"Store::defaulted", "empty"} <= selected
    assert {"Store::required", "ffi"}.isdisjoint(selected)


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
        target = body["questions"][check.id]["instructions"]["target"]
        assert target["path"] == check.target.path
        assert target["name"] == check.target.qualified_name
        assert target["start_line"] == check.target.start_line
        assert target["end_line"] == check.target.end_line
        assert target["span"] == [check.target.start_byte, check.target.end_byte]
        assert target.get("kind") == check.target.kind
        assert not {"scope", "language", "id", "start_byte", "end_byte", "display_name"} & target.keys()


async def test_rule_metrics_distinguish_question_bytes_from_shared_request_usage(basic_rule: Rule) -> None:
    config = configured(basic_rule)
    planner = planned("class S:\n    def first(self): return 1\n    def second(self): return 2\n", config)
    sink, summary = Sink(), Summary("live")

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "test",
                "usage": {"input_tokens": 90, "output_tokens": 3},
                "answers": {key: {"type": "noul", "noul": 0.1} for key in body["questions"]},
            },
        )

    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
        assert client.requests == 1
    metrics = [
        metric for event in sink.events if event["event"] == "evaluation" for metric in event["inference"].values()
    ]
    assert len(metrics) == 3 and all(metric["shared_request"] for metric in metrics)
    assert all(metric["request"]["input_tokens"] == 90 for metric in metrics)
    assert all(metric["request"]["question_bytes"] > metric["question_bytes"] for metric in metrics)
    assert len({metric["request"]["request_sha256"] for metric in metrics}) == 1


def test_rust_owner_supplies_fields_and_sibling_impls(basic_rule: Rule) -> None:
    source = "struct S { field: i32 }\nimpl S { fn first(&self) -> i32 { self.field } }\n"
    source += "impl S { fn second(&self) -> i32 { self.first() } }\n"
    requests = list(planned(source, configured(basic_rule), "rust").plan())
    assert len(requests) == 1 and isinstance(requests[0], Request)
    request = requests[0]
    assert request.evidence.state["documents"][0]["content"] == source
    assert {check.target.qualified_name for check in request.checks} == {"impl S::first", "impl S::second"}


def test_file_and_unit_targets_with_shared_evidence_use_separate_scope_batches(basic_rule: Rule) -> None:
    document = basic_rule.model_dump()
    file_rule = Rule.model_validate({**document, "target": "file", "context": "file", "applies_to": []})
    unit_rule = Rule.model_validate({**document, "context": "file"})
    config = configured(basic_rule).model_copy(update={"rules": {"file-check": file_rule, "unit-check": unit_rule}})
    planner = planned("GLOBAL = 1\ndef f(): return GLOBAL\n", config)
    requests = list(planner.plan())
    assert len(requests) == 2 and all(isinstance(request, Request) for request in requests)
    assert {request.checks[0].target.scope for request in requests} == {"file", "unit"}
    assert requests[0].evidence.key == requests[1].evidence.key
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
        return answer(request, 0.05)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert accepted and all(body["state"]["documents"][0]["content"].startswith("class S:") for body in accepted)
    assert summary.units_evaluated == 2 and summary.context_reduced == 2
    assert summary.checks_skipped == 0 and not summary.incomplete
    assert summary.exit_code("warning") == 0
    reduced = [event for event in sink.events if event.get("code") == "context-reduced"]
    assert reduced and all(event["severity"] == "warning" and not event["incomplete"] for event in reduced)


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


@pytest.mark.parametrize(("probability", "severity"), [(0.95, "warning"), (0.995, "error")])
async def test_advisory_findings_remain_visible_without_blocking_exit(
    basic_rule: Rule, probability: float, severity: str
) -> None:
    advisory = Rule.model_validate({
        **basic_rule.model_dump(),
        "report": {**basic_rule.report.model_dump(), "blocks_exit": False},
    })
    source = "def work():\n    return 1\n"
    for rules, blocking in (({"ADVISORY": advisory}, False), ({"ADVISORY": advisory, "BLOCKING": basic_rule}, True)):
        config = Config(rules=rules, jev=JevConfig(requests_per_minute=0, retries=0))
        sink, summary = Sink(), Summary("live")
        async with JevClient(
            config.jev, "test-key", transport=httpx.MockTransport(lambda request: answer(request, probability))
        ) as client:
            await evaluate_file(planned(source, config), client, None, sink, summary)
        events = [event for event in sink.events if event["event"] == "evaluation"]
        assert [finding["rule"] for event in events for finding in event["findings"]] == sorted(rules)
        assert summary.findings[severity] == len(rules)
        assert summary.advisory_findings[severity] == 1
        assert summary.exit_code("warning") == blocking
        assert summary.exit_code("error") == (blocking and severity == "error")


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
                        "noul": 0.99 if question["instructions"]["target"]["name"].endswith("bad") else 0.1,
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
        for question in body["questions"].values():
            target = question["instructions"]["target"]
            assert target["path"] == "sample.py"
            assert target["start_line"] <= target["end_line"]
            assert len(target["span"]) == 2
            assert 0 <= target["span"][0] <= target["span"][1]
            assert not {"id", "start_byte", "end_byte"} & target.keys()
            assert target["name"] in {"Café", "Café.méthode"}


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


async def test_known_model_limit_preflights_oversized_complete_file_without_http(basic_rule):
    rule = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    config = configured(rule)
    source = "# " + "context with words " * 8000 + "\ndef f(): return 1\n"
    planner = planned(source, config)
    requested = planner.context.requested(planner.checks[0])
    assert planner.estimate(requested, planner.checks)[0] > 46_000
    assert "context" in planner.violations(requested, planner.checks)

    def never(_request):
        raise AssertionError("known Jev planning limit must trigger recovery before HTTP")

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(never)) as client:
        await evaluate_file(planner, client, None, sink, summary)
        assert client.requests == 0
    assert summary.checks_evaluated == 0 and summary.checks_skipped == 1 and summary.incomplete
    event = next(event for event in sink.events if event["event"] == "evaluation")
    assert event["context_selection"]["cohesion"]["trigger"] == "model_context_preflight"


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
    assert not rejected  # documented local limits compact before the provider sees the oversized state
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
        if any(question["instructions"]["target"]["name"] == "Owner.work" for question in body["questions"].values())
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
        if any("kind" not in question["instructions"]["target"] for question in body["questions"].values()):
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
        assert selectors[0]["state"]["jevscan_prompt"]["rubrics"][instructions["rubric"]] == config.rules[
            "TEAM01"
        ].question.model_dump(mode="json")
        assert instructions["target"]["kind"] == "function"
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
        question = next(iter(body["questions"].values()))
        rubric = body["state"]["jevscan_prompt"]["rubrics"][question["instructions"]["rubric"]]
        if len(body["questions"]) > 1:
            return httpx.Response(413)
        if rubric["instructions"] == first.question.instructions:
            return httpx.Response(422, json={"detail": [{"type": "invalid_request"}]})
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


async def test_unknown_request_rejection_isolated_to_one_request_and_scan_continues(basic_rule):
    config = configured(basic_rule, max_questions=1)
    source = "class S:\n    def one(self): return 1\n    def two(self): return 2\n"
    planner = planned(source, config)
    seen = 0

    def handle(request):
        nonlocal seen
        seen += 1
        if seen == 1:
            return httpx.Response(
                400, json={"error": {"type": "invalid_request"}}, headers={"x-typesafe-request-id": "bad-1"}
            )
        return answer(request)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert summary.request_rejections == 1
    assert summary.checks_skipped == 1 and summary.checks_evaluated == len(planner.checks) - 1
    assert summary.incomplete
    diagnostics = [event for event in sink.events if event.get("code") == "provider-request-rejected"]
    assert len(diagnostics) == 1
    assert "invalid_request" in diagnostics[0]["message"]
    assert "bad-1" in diagnostics[0]["message"]
    assert any(event["event"] == "evaluation" and event["answers"] for event in sink.events)


def test_oversized_shared_owner_stays_grouped_until_compaction(basic_rule: Rule) -> None:
    method_rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"]})
    config = configured(method_rule, max_context_tokens=1500, max_total_tokens=3000, token_reserve=100)
    config = config.model_copy(update={"rules": {f"rule{i}": method_rule for i in range(6)}})
    source = 'class Huge:\n    """' + "background " * 5000 + '"""\n    def work(self):\n        return 1\n'
    planner = planned(source, config)
    requests = list(planner.plan())
    assert len(requests) == 1
    assert len(requests[0].checks) == 6
    assert "context" in planner.violations(requests[0].evidence, requests[0].checks)


async def test_oversized_shared_owner_compacts_then_batches_rules(basic_rule: Rule) -> None:
    method_rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"]})
    config = configured(method_rule, max_context_tokens=1500, max_total_tokens=3000, token_reserve=100)
    config = config.model_copy(update={"rules": {f"rule{i}": method_rule for i in range(6)}})
    source = 'class Huge:\n    """' + "background " * 5000 + '"""\n    def work(self):\n        return 1\n'
    planner = planned(source, config)
    seen = []

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert len(body["questions"]) == 6
        assert "background background" not in body["state"]["documents"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {key: {"type": "noul", "noul": 0.1} for key in body["questions"]},
            },
        )

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert client.requests == 1
    assert len(seen) == 1
    assert summary.checks_evaluated == 6


async def test_judgment_cache_survives_batch_composition_change(tmp_path: Path, basic_rule: Rule) -> None:
    method_rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"]})
    source = "class S:\n    def work(self): return 1\n"
    first = configured(method_rule).model_copy(update={"rules": {"a": method_rule, "b": method_rule}})
    second = configured(method_rule).model_copy(update={"rules": {"b": method_rule}})
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "test-pinned",
                "answers": {key: {"type": "noul", "noul": 0.1} for key in body["questions"]},
            },
        )

    async with AnswerCache(tmp_path / "cache.sqlite3", 3600) as cache:
        sink, summary = Sink(), Summary("live")
        async with JevClient(first.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
            await evaluate_file(planned(source, first), client, cache, sink, summary)
            assert client.requests == 1
        sink, summary = Sink(), Summary("live")
        async with JevClient(second.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
            await evaluate_file(planned(source, second), client, cache, sink, summary)
            assert client.requests == 0
        assert summary.checks_evaluated == 1 and summary.units_cached == 1
    assert calls == 1


def test_full_file_line_limit_omits_only_checks_that_require_the_file(basic_rule: Rule) -> None:
    file_rule = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    config = configured(basic_rule).model_copy(
        update={
            "rules": {"unit": basic_rule, "file": file_rule},
            "scan": configured(basic_rule).scan.model_copy(update={"max_full_file_lines": 100}),
        }
    )
    source = "def work():\n    return 1\n" + "# filler\n" * 105
    planner = planned(source, config)
    assert {check.rule_id for check in planner.checks} == {"unit"}
    assert {item.check.rule_id for item in planner.omissions} == {"file"}
    assert "configured limit is 100" in planner.omissions[0].reason


async def test_full_file_line_limit_is_counted_as_incomplete_coverage(basic_rule: Rule) -> None:
    file_rule = Rule.model_validate({**basic_rule.model_dump(), "target": "file", "context": "file", "applies_to": []})
    config = configured(basic_rule).model_copy(
        update={
            "rules": {"unit": basic_rule, "file": file_rule},
            "scan": configured(basic_rule).scan.model_copy(update={"max_full_file_lines": 100}),
        }
    )
    planner = planned("def work():\n    return 1\n" + "# filler\n" * 105, config)
    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(answer)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert summary.checks_evaluated == 1
    assert summary.checks_skipped == 1
    assert summary.incomplete
    coverage = next(event for event in sink.events if event["event"] == "coverage")
    assert coverage["skipped_checks"] == 1 and coverage["skipped_file_checks"] == 1


async def test_one_large_file_can_use_global_request_concurrency(basic_rule: Rule) -> None:
    method_rule = Rule.model_validate({**basic_rule.model_dump(), "applies_to": ["method"]})
    config = configured(method_rule, max_questions=1).model_copy(
        update={"jev": JevConfig(concurrency=4, requests_per_minute=0, retries=0)}
    )
    source = "class S:\n" + "".join(f"    def m{i}(self): return {i}\n" for i in range(20))
    planner = planned(source, config)
    active = peak = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        body = json.loads(request.content)
        active -= 1
        return httpx.Response(
            200,
            json={"model": "test", "answers": {key: {"type": "noul", "noul": 0.1} for key in body["questions"]}},
        )

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert peak == 4
    assert client.requests == 20


async def test_mixed_rule_primitives_share_one_owner_request(tmp_path: Path) -> None:
    config = load_config([tmp_path], cwd=tmp_path).config
    config = config.model_copy(
        update={
            "lint": config.lint.model_copy(update={"select": ["JEV01", "JEV02", "JEV04"]}),
            "jev": config.jev.model_copy(update={"requests_per_minute": 0, "retries": 0}),
        }
    )
    planner = planned(
        "class S:\n    def work(self, value):\n        if value is None:\n            return 0\n        return value\n",
        config,
    )
    requests = list(planner.plan())
    method_request = next(
        request for request in requests if any(check.target.qualified_name == "S.work" for check in request.checks)
    )
    body = json.loads(method_request.body)
    assert {question["type"] for question in body["questions"].values()} == {"noul", "score", "choice"}
    assert len(method_request.checks) == 3

    async def handle(request: httpx.Request) -> httpx.Response:
        submitted = json.loads(request.content)
        answers = {}
        for key, question in submitted["questions"].items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.1}
            elif question["type"] == "score":
                answers[key] = {
                    "type": "score",
                    "score": 0.2,
                    "confidence": 0.9,
                    "probabilities": {"0": 0.8, "1": 0.2, "2": 0.0, "3": 0.0},
                }
            else:
                criteria = question["criteria"]
                clean = "justified_or_absent"
                answers[key] = {
                    "type": "choice",
                    "choice": clean,
                    "confidence": 0.9,
                    "probabilities": {name: (1.0 if name == clean else 0.0) for name in criteria},
                }
        return httpx.Response(200, json={"model": "test", "answers": answers})

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    # One owner state carries three different answer schemas in one paid call.
    assert client.requests == 1
    assert summary.checks_evaluated == 3


def test_shared_evidence_batch_can_mix_noul_score_and_choice_questions(basic_rule: Rule) -> None:
    score = Rule.model_validate({
        "applies_to": ["function"],
        "context": "file",
        "question": {"type": "score", "instructions": "Rate the path.", "criteria": ["clear", "mixed", "tangled"]},
        "report": {
            "message": "Path is unclear.",
            "levels": {
                "warning": {"min_score": 1.0, "min_confidence": 0.5},
                "error": {"min_score": 2.0, "min_confidence": 0.7},
            },
        },
    })
    choice = Rule.model_validate({
        "applies_to": ["function"],
        "context": "file",
        "question": {
            "type": "choice",
            "instructions": "Classify the behavior.",
            "criteria": {"ok": "No defect.", "defect": "A defect is present."},
        },
        "report": {
            "message": "Behavior is defective.",
            "choices": ["defect"],
            "levels": {
                "warning": {"min_probability": 0.6, "min_confidence": 0.5},
                "error": {"min_probability": 0.9, "min_confidence": 0.7},
            },
        },
    })
    noul = basic_rule.model_copy(update={"context": "file", "applies_to": ["function"]})
    config = configured(noul).model_copy(update={"rules": {"noul": noul, "score": score, "choice": choice}})
    source = "VALUE = 1\ndef first(): return VALUE\ndef second(): return VALUE\n"
    planner = planned(source, config)
    requests = list(planner.plan())
    assert len(requests) == 1
    request = requests[0]
    assert {check.rule.question.type for check in request.checks} == {"noul", "score", "choice"}
    assert len(request.checks) == 6
    assert {check.target.qualified_name for check in request.checks} == {"first", "second"}
    body = json.loads(request.body)
    rubrics = body["state"]["jevscan_prompt"]["rubrics"]
    for check in request.checks:
        question = json.loads(request.question_wires[check.id])
        assert question["instructions"]["target"]["name"] == check.target.qualified_name
        assert rubrics[question["instructions"]["rubric"]] == check.rule.question.model_dump(mode="json")
        if question["type"] == "choice":
            assert isinstance(check.rule.question, ChoiceQuestion)
            assert set(question["criteria"]) == set(check.rule.question.criteria)
            assert set(question["criteria"].values()) == {None}
        elif question["type"] == "score":
            assert isinstance(check.rule.question, ScoreQuestion)
            assert question["criteria"] == [str(index) for index in range(len(check.rule.question.criteria))]
        else:
            assert "criteria" not in question

    bounded = config.model_copy(update={"evaluation": config.evaluation.model_copy(update={"max_questions": 2})})
    bounded_requests = list(Planner(planner.context, bounded).plan())
    assert len(bounded_requests) == 3
    assert all(len(batch.checks) <= 2 and batch.questions for batch in bounded_requests)
    assert len({batch.state for batch in bounded_requests}) == 1


def test_judgment_cache_identity_changes_with_the_shared_rubric_registry(basic_rule: Rule) -> None:
    extra_question = basic_rule.question.model_copy(update={"instructions": "Is this a different operation?"})
    extra_rule = basic_rule.model_copy(update={"question": extra_question})
    config = configured(basic_rule).model_copy(update={"rules": {"base": basic_rule, "extra": extra_rule}})
    selected_only = config.model_copy(update={"lint": config.lint.model_copy(update={"select": ["base"]})})
    all_selected = config.model_copy(update={"lint": config.lint.model_copy(update={"select": ["ALL"]})})
    source = "def work(): return 1\n"
    only_planner, all_planner = planned(source, selected_only), planned(source, all_selected)
    only_check = next(check for check in only_planner.checks if check.rule_id == "base")
    all_check = next(check for check in all_planner.checks if check.rule_id == "base")
    only_request = only_planner.request(only_planner.requested_evidence(only_check), (only_check,))
    all_request = all_planner.request(all_planner.requested_evidence(all_check), (all_check,))

    assert only_request.question_wires[only_check.id] == all_request.question_wires[all_check.id]
    assert only_request.state != all_request.state
    assert judgment_cache_key(
        "https://api.typesafe.ai", "jev-latest", only_request.state, only_request.question_wires[only_check.id]
    ) != judgment_cache_key(
        "https://api.typesafe.ai", "jev-latest", all_request.state, all_request.question_wires[all_check.id]
    )


async def test_file_wide_rubrics_fit_calibrated_context_on_public_source() -> None:
    root = Path(__file__).parents[1]
    path = root / "src/jevscan/core/calibration_selection.py"
    relative = path.relative_to(root).as_posix()
    parsed = parse_source(path.read_bytes(), FileJob(str(path), relative, "python", "python"))
    assert not parsed.failed, parsed.diagnostics
    assert parsed.source.count(b"\n") < 3_000

    config = load_config([], cwd=root).config
    calibration = TokenCalibration(bytes_per_token=2.2, observations=1)
    planner = Planner(ContextBuilder(parsed), config, calibration)
    requests = list(planner.plan())
    planned_ids = {check.id for request in requests for check in request.checks}
    assert planned_ids == {check.id for check in planner.checks}

    file_checks = [check for check in planner.checks if check.target.scope == "file"]
    assert {check.rule_id for check in file_checks} == {"JEV07", "JEV09"}
    file_request = next(request for request in requests if request.checks[0].target.scope == "file")
    assert {check.rule_id for check in file_request.checks} == {"JEV07", "JEV09"}
    state = json.loads(file_request.state)
    rubrics = state["jevscan_prompt"]["rubrics"]
    expected_references = {rubric_reference(check.rule.question) for check in file_request.checks}
    assert set(rubrics) == expected_references
    assert json.loads(file_request.body)["state"] == state
    for check in file_request.checks:
        validate_prompt_registry(state, check.rule.question)
    assert len(file_request.evidence.encoded) > 49_000
    assert planner.budget.fits(file_request.state, file_request.question_wires)
    assert planner.estimate(file_request.evidence, file_request.checks)[0] < 28_000

    all_selected = PromptRegistry.from_rules(config.selected_rules())
    unscoped_state = all_selected.state_bytes(file_request.evidence.state)
    assert len(json.loads(unscoped_state)["jevscan_prompt"]["rubrics"]) == len(config.selected_rules())
    assert not planner.budget.fits(unscoped_state, file_request.question_wires)

    selected = config.model_copy(
        update={"lint": config.lint.model_copy(update={"select": ["JEV01", "JEV07", "JEV09"]})}
    )
    live_planner = Planner(ContextBuilder(parsed), selected, TokenCalibration(bytes_per_token=2.2, observations=1))
    live_requests = list(live_planner.plan())
    assert all(live_planner.budget.fits(request.state, request.question_wires) for request in live_requests)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {name: {"type": "noul", "noul": 0.1} for name in body["questions"]},
            },
        )

    sink, summary = Sink(), Summary("live")
    async with JevClient(selected.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(live_planner, client, None, sink, summary)
    assert summary.checks_evaluated == len(live_planner.checks)
    assert {check.rule_id for check in live_planner.checks} == {"JEV01", "JEV07", "JEV09"}
    assert sum(len(body["questions"]) for body in calls) == len(live_planner.checks)


async def test_scoped_registry_survives_batch_splitting_and_unrelated_rule_changes(
    basic_rule: Rule,
) -> None:
    extra_question = basic_rule.question.model_copy(update={"instructions": "Does this preserve a separate contract?"})
    extra_rule = basic_rule.model_copy(update={"question": extra_question})
    file_question = basic_rule.question.model_copy(update={"instructions": "Does the whole file own this state?"})
    file_rule = Rule.model_validate({
        **basic_rule.model_dump(),
        "target": "file",
        "context": "file",
        "applies_to": [],
        "question": file_question.model_dump(),
    })
    config = Config(
        rules={"file": file_rule, "unit": basic_rule, "unit_extra": extra_rule},
        evaluation=EvaluationConfig(),
        jev=JevConfig(requests_per_minute=0, retries=0),
    )
    planner = planned("def work(): return 1\n", config)
    requests = list(planner.plan())
    assert len(requests) == 2
    file_request = next(request for request in requests if request.checks[0].target.scope == "file")
    unit_request = next(request for request in requests if request.checks[0].target.scope == "unit")
    assert file_request.evidence.key == unit_request.evidence.key
    assert set(file_request.registry.rubrics) == {rubric_reference(file_rule.question)}
    assert set(unit_request.registry.rubrics) == {
        rubric_reference(basic_rule.question),
        rubric_reference(extra_rule.question),
    }
    with pytest.raises(ValueError, match="different evidence groups"):
        planner.registry_for((file_request.checks[0], unit_request.checks[0]))

    # A check-only retry retains the registry for the whole shared group.
    unit_subset = planner.request(unit_request.evidence, (unit_request.checks[0],))
    assert unit_subset.registry == unit_request.registry
    assert unit_subset.state == unit_request.state

    units_only = Config(
        rules={"unit": basic_rule, "unit_extra": extra_rule},
        evaluation=config.evaluation,
        jev=config.jev,
    )
    without_unrelated_file_rule = Planner(planner.context, units_only)
    same_unit_group = next(without_unrelated_file_rule.plan())
    unit_check = unit_request.checks[0]
    same_check = next(check for check in same_unit_group.checks if check.rule_id == unit_check.rule_id)
    assert same_unit_group.state == unit_request.state
    assert judgment_cache_key(
        "https://api.typesafe.ai",
        "jev-latest",
        unit_request.state,
        unit_request.question_wires[unit_check.id],
    ) == judgment_cache_key(
        "https://api.typesafe.ai",
        "jev-latest",
        same_unit_group.state,
        same_unit_group.question_wires[same_check.id],
    )

    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {name: {"type": "noul", "noul": 0.1} for name in body["questions"]},
            },
        )

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        await evaluate_file(planner, client, None, sink, summary)
    assert summary.checks_evaluated == len(planner.checks) == 3
    assert sum(len(body["questions"]) for body in calls) == len(planner.checks)
