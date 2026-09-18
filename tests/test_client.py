import asyncio
import json
import time

import httpx
import pytest

from jevscan.core.assessment import assess
from jevscan.core.client import JevClient, RequestLimiter
from jevscan.core.config import Config
from jevscan.core.context import ContextBuilder
from jevscan.core.models import ParsedFile, Severity, Target, Unit
from jevscan.core.planning import Planner, Request
from jevscan.core.protocol import Check, ContextLimitError, JevError, validate_response
from jevscan.core.rules import Rule


def request_for(config: Config, unit: Unit) -> Request:
    parsed = ParsedFile(unit.path, unit.language, b"def work():\n    return 1\n", (unit,))
    requests = list(Planner(ContextBuilder(parsed), config).plan())
    assert len(requests) == 1 and isinstance(requests[0], Request)
    return requests[0]


def response_body(probability: float = 0.93) -> dict:
    return {
        "model": "jev-test-pinned",
        "usage": {"input_tokens": 100, "output_tokens": 0},
        "answers": {"q00000": {"type": "noul", "noul": probability}},
    }


async def test_actual_http_contract_and_retry_attempts(config: Config, unit: Unit) -> None:
    requests = []
    plan = request_for(config, unit)

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/systemone"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == config.jev.model
        assert body["questions"]["q00000"]["type"] == "noul"
        assert body["questions"]["q00000"]["instructions"]["target"]["id"] == unit.id
        if len(requests) < 3:
            return httpx.Response(429 if len(requests) == 1 else 503, headers={"Retry-After": "0"})
        return httpx.Response(200, json=response_body())

    settings = config.jev.model_copy(update={"retries": 3})
    async with JevClient(settings, "test-key", transport=httpx.MockTransport(handle)) as client:
        result = await client.evaluate(plan.body, plan.questions)
        assert client.requests == 3
    decision = assess(plan.checks[0], result.answers["q00000"])
    assert decision.status == "warning" and decision.finding is not None and decision.finding.probability == 0.93


@pytest.mark.parametrize("status", [400, 401])
async def test_non_retryable_failure_is_not_retried(config: Config, unit: Unit, status: int) -> None:
    calls = 0

    async def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"secret": "must-not-appear", "message": "max_tokens_exceeded"})

    settings = config.jev.model_copy(update={"retries": 5})
    plan = request_for(config, unit)
    async with JevClient(settings, "test-key", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(JevError, match=f"HTTP {status}") as caught:
            await client.evaluate(plan.body, plan.questions)
    assert calls == 1
    assert "must-not-appear" not in str(caught.value)


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "noul", "noul": 1.5},
        {"type": "noul", "noul": "0.9"},
        {"type": "noul", "noul": True},
        {"type": "noul", "noul": float("nan")},
        {"type": "unknown", "noul": 0.9},
    ],
)
def test_malformed_api_answers_are_not_quality_findings(config: Config, answer: dict) -> None:
    body = response_body()
    body["answers"]["q00000"] = answer
    with pytest.raises(JevError):
        validate_response(json.dumps(body), {"q00000": config.rules["cohesion"].question})


def test_choice_and_score_thresholds(unit: Unit) -> None:
    rules = {
        "choice": Rule.model_validate({
            "applies_to": ["function"],
            "question": {
                "type": "choice",
                "instructions": "Which?",
                "criteria": {"bad": "Defect", "unknown": "Unknown"},
            },
            "report": {
                "message": "Choice finding",
                "choices": ["bad"],
                "uncertain_choices": ["unknown"],
                "levels": {
                    "warning": {"min_probability": 0.6, "min_confidence": 0.5},
                    "error": {"min_probability": 0.9, "min_confidence": 0.7},
                },
            },
        }),
        "score": Rule.model_validate({
            "applies_to": ["function"],
            "question": {"type": "score", "instructions": "How?", "criteria": ["Good", "Mixed", "Bad"]},
            "report": {
                "message": "Score finding",
                "levels": {
                    "warning": {"min_score": 1.0, "min_confidence": 0.5},
                    "error": {"min_score": 1.5, "min_confidence": 0.7},
                },
            },
        }),
    }
    questions = {name: rule.question for name, rule in rules.items()}
    body = {
        "model": "test",
        "answers": {
            "choice": {
                "type": "choice",
                "choice": "bad",
                "confidence": 0.8,
                "probabilities": {"bad": 0.95, "unknown": 0.05},
            },
            "score": {
                "type": "score",
                "score": 1.8,
                "confidence": 0.6,
                "probabilities": {"0": 0.05, "1": 0.1, "2": 0.85},
            },
        },
    }
    result = validate_response(json.dumps(body), questions)
    checks = {name: Check(name, Target.from_unit(unit), name, rule) for name, rule in rules.items()}
    findings = [assess(checks[name], answer).finding for name, answer in result.answers.items()]
    assert [finding.severity for finding in findings if finding] == [Severity.ERROR, Severity.WARNING]
    body["answers"]["choice"].update(choice="unknown", confidence=0.99, probabilities={"bad": 0.01, "unknown": 0.99})
    body["answers"]["score"].update(score=0.8, confidence=0.4)
    result = validate_response(json.dumps(body), questions)
    assert all(assess(checks[name], answer).status == "unknown" for name, answer in result.answers.items())


async def test_limiter_paces_and_respects_a_later_deferral() -> None:
    limiter = RequestLimiter(6000)
    await limiter.acquire()
    started = time.monotonic()
    pending = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.002)
    limiter.defer(0.03)
    await pending
    assert time.monotonic() - started >= 0.029


async def test_explicit_size_failure_is_owned_by_planner(config: Config, unit: Unit) -> None:
    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "max_tokens_exceeded", "message": "private source"}})

    plan = request_for(config, unit)
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ContextLimitError) as caught:
            await client.evaluate(plan.body, plan.questions)
        assert client.requests == 1
    assert "private source" not in str(caught.value)


@pytest.mark.parametrize("ids", [[], ["invented"], ["q00000", "extra"]])
def test_response_question_ids_must_match_exactly(config: Config, ids: list[str]) -> None:
    body = response_body()
    body["answers"] = {key: {"type": "noul", "noul": 0.95} for key in ids}
    with pytest.raises(JevError, match="question IDs"):
        validate_response(json.dumps(body), {"q00000": config.rules["cohesion"].question})


def test_inverse_noul_and_descending_score_levels(unit: Unit, basic_rule: Rule) -> None:
    noul = Rule.model_validate({
        **basic_rule.model_dump(),
        "report": {
            "message": "Evidence for no.",
            "expected": False,
            "levels": {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.9}},
        },
    })
    score = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "score", "instructions": "How clear?", "criteria": ["Bad", "Mixed", "Good"]},
        "report": {"message": "Low clarity.", "levels": {"warning": {"max_score": 1.0}, "error": {"max_score": 0.5}}},
    })
    questions = {"n": noul.question, "s": score.question}
    response = validate_response(
        json.dumps({
            "model": "test",
            "answers": {
                "n": {"type": "noul", "noul": 0.05},
                "s": {
                    "type": "score",
                    "score": 0.5,
                    "confidence": 0.9,
                    "probabilities": {"0": 0.5, "1": 0.5, "2": 0.0},
                },
            },
        }),
        questions,
    )
    for key, rule in (("n", noul), ("s", score)):
        assert assess(Check(key, Target.from_unit(unit), key, rule), response.answers[key]).status == "error"
