import asyncio
import json
import time

import httpx
import pytest

from jevscan.core.client import JevClient, JevError, RequestLimiter, RequestTooLarge, findings_from, validate_response
from jevscan.core.config import Config, Rule
from jevscan.core.context import WorkItem
from jevscan.core.models import Severity, Unit


def response_body(probability: float = 0.93) -> dict:
    return {"model": "jev-test-pinned", "usage": {"input_tokens": 100, "output_tokens": 0},
            "answers": {"cohesion": {"type": "noul", "noul": probability}}}


async def test_actual_http_contract_and_retry_attempts(config: Config, unit: Unit) -> None:
    requests = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/systemone"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == config.jev.model
        assert body["questions"]["cohesion"]["type"] == "noul"
        if len(requests) < 3:
            return httpx.Response(429 if len(requests) == 1 else 503, headers={"Retry-After": "0"})
        return httpx.Response(200, json=response_body())

    settings = config.jev.model_copy(update={"retries": 3})
    work = WorkItem(unit, {"source": "def work(): pass"}, config.rules)
    async with JevClient(settings, "test-key", transport=httpx.MockTransport(handle)) as client:
        result = await client.evaluate(client.body_for(work), work.rules)
        assert client.requests == 3
    assert findings_from(work, result)[0].probability == 0.93


async def test_non_retryable_failure_is_not_retried(config: Config, unit: Unit) -> None:
    calls = 0

    async def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"secret": "must-not-appear"})

    settings = config.jev.model_copy(update={"retries": 5})
    async with JevClient(settings, "test-key", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(JevError, match="HTTP 401") as caught:
            await client.evaluate(client.body_for(WorkItem(unit, {}, config.rules)), config.rules)
    assert calls == 1
    assert "must-not-appear" not in str(caught.value)


@pytest.mark.parametrize("answer", [
    {"type": "noul", "noul": 1.5},
    {"type": "noul", "noul": "0.9"},
    {"type": "noul", "noul": True},
    {"type": "noul", "noul": float("nan")},
    {"type": "unknown", "noul": 0.9},
])
def test_malformed_api_answers_are_not_quality_findings(config: Config, answer: dict) -> None:
    body = response_body()
    body["answers"]["cohesion"] = answer
    with pytest.raises(JevError):
        validate_response(json.dumps(body), config.rules)


def test_choice_and_score_thresholds(unit: Unit) -> None:
    rules = {
        "choice": Rule.model_validate({
            "applies_to": ["function"],
            "question": {"type": "choice", "instructions": "Which?", "criteria": {"bad": "Defect", "unknown": "Unknown"}},
            "report": {
                "message": "Choice finding",
                "choices": ["bad"],
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
    body = {"model": "test", "answers": {
        "choice": {"type": "choice", "choice": "bad", "confidence": 0.8, "probabilities": {"bad": 0.95, "unknown": 0.05}},
        "score": {"type": "score", "score": 1.8, "confidence": 0.6, "probabilities": {"0": 0.05, "1": 0.1, "2": 0.85}},
    }}
    result = validate_response(json.dumps(body), rules)
    findings = findings_from(WorkItem(unit, {}, rules), result)
    assert [(finding.rule, finding.severity) for finding in findings] == [
        ("choice", Severity.ERROR),
        ("score", Severity.WARNING),
    ]

    body["answers"]["choice"]["probabilities"] = {"bad": 0.5, "unknown": 0.5}
    body["answers"]["score"]["score"] = 0.8
    result = validate_response(json.dumps(body), rules)
    assert findings_from(WorkItem(unit, {}, rules), result) == []


async def test_limiter_paces_and_respects_a_later_deferral() -> None:
    limiter = RequestLimiter(6000)
    await limiter.acquire()
    started = time.monotonic()
    pending = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.002)
    limiter.defer(0.03)
    await pending
    assert time.monotonic() - started >= 0.029


async def test_request_budget_never_truncates_source(config: Config, unit: Unit) -> None:
    async with JevClient(config.jev.model_copy(update={"max_request_bytes": 1024}), "test-key") as client:
        work = WorkItem(unit, {"source": "λ" * 1000}, config.rules)
        with pytest.raises(RequestTooLarge):
            client.body_for(work)
        assert work.state["source"] == "λ" * 1000
        assert client.requests == 0
