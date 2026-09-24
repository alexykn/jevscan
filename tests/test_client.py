import asyncio
import json
import time

import httpx
import pytest

from jevscan.core.assessment import assess
from jevscan.core.client import JevClient, RequestLimiter
from jevscan.core.config import Config
from jevscan.core.context import ContextBuilder
from jevscan.core.inference import Inference
from jevscan.core.models import ParsedFile, Severity, Summary, Target, Unit
from jevscan.core.planning import Planner, Request
from jevscan.core.protocol import Check, ContextLimitError, JevError, RequestRejectedError, validate_response
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


async def test_concurrent_phase_reservations_use_request_local_totals(config: Config, basic_rule: Rule) -> None:
    both_started = asyncio.Event()
    starts = 0

    async def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        if starts == 2:
            both_started.set()
        await both_started.wait()
        return httpx.Response(200, json=response_body())

    summary = Summary("live")
    questions = {"q00000": basic_rule.question}
    async with JevClient(config.jev, "test-key", transport=httpx.MockTransport(handle)) as client:
        inference = Inference(client, None, summary)
        await asyncio.gather(
            inference.predict(b"x" * 300, questions),
            inference.predict(b"y" * 600, questions, enrichment=True),
        )
    assert summary.evaluation_reserved_input_tokens == 100
    assert summary.enrichment_reserved_input_tokens == 200
    assert client.estimated_input_tokens == 300


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
        instructions = body["questions"]["q00000"]["instructions"]
        target = instructions["target"]
        assert target["name"] == unit.qualified_name
        assert target["path"] == unit.path and target["start_line"] == unit.start_line
        assert target["span"] == [unit.start_byte, unit.end_byte]
        assert not {"scope", "language", "id", "start_byte", "end_byte"} & target.keys()
        prompt = body["state"]["jevscan_prompt"]
        assert instructions["rubric"] in prompt["rubrics"]
        assert instructions["task"] == "Apply referenced rubric."
        assert "state.jevscan_prompt.rubrics" in prompt["policy"]
        assert "state.jevscan_prompt.policy" in prompt["policy"]
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
    decisions = {name: assess(checks[name], answer) for name, answer in result.answers.items()}
    assert decisions["choice"].finding is not None
    assert decisions["choice"].finding.severity == Severity.ERROR
    # The signal reaches error, but confidence supports only warning. Do not hide the indicated error.
    assert decisions["score"].status == "unknown" and decisions["score"].finding is None
    assert decisions["score"].tentative_finding is not None
    assert decisions["score"].tentative_finding.severity == Severity.ERROR
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


@pytest.mark.parametrize("descending", [False, True])
@pytest.mark.parametrize(
    "score,confidence,status,tentative",
    [
        (0.9, 0.4, "unknown", None),
        (1.0, 0.59, "unknown", "warning"),
        (1.66, 0.58, "unknown", "warning"),
        (1.0, 0.6, "warning", None),
        (2.0, 0.69, "unknown", "error"),
        (2.8, 0.3, "unknown", "error"),
        (2.0, 0.7, "error", None),
    ],
)
def test_signal_severity_is_independent_of_confidence(unit, descending, score, confidence, status, tentative):
    from jevscan.core.protocol import ScoreAnswer

    threshold = "max_score" if descending else "min_score"
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
                "warning": {threshold: 2.0 if descending else 1.0, "min_confidence": 0.6},
                "error": {threshold: 1.0 if descending else 2.0, "min_confidence": 0.7},
            },
        },
    })
    answer = ScoreAnswer(
        type="score",
        score=3 - score if descending else score,
        confidence=confidence,
        probabilities={"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.4},
    )
    decision = assess(Check("q", Target.from_unit(unit), "flow", rule), answer)
    assert decision.status == status
    assert (str(decision.tentative_finding.severity) if decision.tentative_finding else None) == tentative
    if tentative:
        assert decision.finding is None and decision.reason == "low_confidence"


@pytest.mark.parametrize(
    "selected,probability,confidence,tentative",
    [
        ("bad", 0.95, 0.2, "error"),
        ("bad", 0.8, 0.4, "warning"),
        ("bad", 0.55, 0.4, None),
        ("clean", 0.95, 0.2, None),
        ("missing", 0.95, 0.9, None),
        ("na", 0.95, 0.2, None),
    ],
)
def test_only_eligible_choice_signals_have_tentative_severity(unit, selected, probability, confidence, tentative):
    from jevscan.core.protocol import ChoiceAnswer

    labels = {"bad": "Defect", "clean": "Justified", "missing": "Missing evidence", "na": "Not applicable"}
    rule = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "choice", "instructions": "Classify the check.", "criteria": labels},
        "report": {
            "message": "Defect.",
            "choices": ["bad"],
            "uncertain_choices": ["missing"],
            "not_applicable_choices": ["na"],
            "levels": {
                "warning": {"min_probability": 0.6, "min_confidence": 0.5},
                "error": {"min_probability": 0.9, "min_confidence": 0.7},
            },
        },
    })
    answer = ChoiceAnswer(
        type="choice",
        choice=selected,
        confidence=confidence,
        probabilities={key: probability if key == selected else (1 - probability) / 3 for key in labels},
    )
    decision = assess(Check("q", Target.from_unit(unit), "contract", rule), answer)
    assert decision.status == "unknown" and decision.finding is None
    assert (str(decision.tentative_finding.severity) if decision.tentative_finding else None) == tentative


def test_ambiguous_noul_requires_a_directional_signal_for_tentative_severity(unit, basic_rule):
    from jevscan.core.protocol import NoulAnswer

    for expected in (True, False):
        rule = Rule.model_validate({
            **basic_rule.model_dump(),
            "report": {
                "message": "Cohesion",
                "expected": expected,
                "uncertain_range": [0.4, 0.6],
                "levels": {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.9}},
            },
        })
        check = Check("q", Target.from_unit(unit), "cohesion", rule)
        for value in (0.45, 0.5, 0.55):
            decision = assess(check, NoulAnswer(type="noul", noul=value))
            assert decision.status == "unknown" and decision.reason == "probability_ambiguous"
            assert decision.finding is None
            probability = value if expected else 1 - value
            assert bool(decision.tentative_finding) is (probability >= 0.5)


@pytest.mark.parametrize(
    "status,body,rejected",
    [
        (413, None, True),
        (400, {"code": "max_tokens_exceeded"}, True),
        (422, {"error": "max_tokens_exceeded"}, True),
        (400, {"error": {"code": "max_tokens_exceeded"}}, True),
        (422, {"detail": {"code": "max_tokens_exceeded"}}, True),
        (422, {"detail": [{"type": "max_tokens_exceeded", "msg": "private source"}]}, True),
        (422, {"detail": [{"type": "missing", "msg": "max_tokens_exceeded"}]}, False),
        (400, {"message": "max_tokens_exceeded"}, False),
        (401, {"code": "max_tokens_exceeded"}, False),
    ],
)
async def test_size_error_envelopes_are_strict_and_safe(status, body, rejected, basic_rule):
    from jevscan.core.config import JevConfig
    from jevscan.core.protocol import ContextLimitError, JevError

    config = JevConfig(requests_per_minute=0, retries=0)

    def handle(_request):
        return httpx.Response(status, json=body, headers={"x-typesafe-request-id": "safe-id"})

    async with JevClient(config, "secret-key", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ContextLimitError if rejected else JevError) as captured:
            await client.evaluate(b"{}", {"q": basic_rule.question})
    if rejected:
        assert isinstance(captured.value, ContextLimitError)
        assert captured.value.metadata()["status"] == status
        assert captured.value.metadata()["request_id"] == "safe-id"
    assert "secret-key" not in str(captured.value)


async def test_unknown_400_exposes_only_sanitized_machine_metadata(basic_rule):
    from jevscan.core.config import JevConfig

    body = {
        "detail": [{"type": "validation_error", "msg": "private source should never escape"}],
        "message": "more private source",
    }

    def handle(_request):
        return httpx.Response(400, json=body, headers={"x-typesafe-request-id": "safe-id"})

    async with JevClient(
        JevConfig(requests_per_minute=0, retries=0), "secret-key", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(RequestRejectedError) as caught:
            await client.evaluate(b"{}", {"q": basic_rule.question})
    metadata = caught.value.metadata()
    assert metadata["status"] == 400
    assert metadata["machine_fields"] == {"type": ["validation_error"]}
    assert metadata["request_id"] == "safe-id"
    assert "private source" not in str(caught.value) and "secret-key" not in str(caught.value)


async def test_repeated_equivalent_request_rejections_trip_global_circuit_breaker(basic_rule):
    from jevscan.core.config import JevConfig

    def handle(_request):
        return httpx.Response(422, json={"error": {"type": "invalid_request"}})

    async with JevClient(
        JevConfig(requests_per_minute=0, retries=0), "key", transport=httpx.MockTransport(handle)
    ) as client:
        for _ in range(2):
            with pytest.raises(RequestRejectedError):
                await client.evaluate(b"{}", {"q": basic_rule.question})
        with pytest.raises(JevError, match="3 equivalent requests"):
            await client.evaluate(b"{}", {"q": basic_rule.question})


def test_known_jev_limits_bound_null_user_caps_and_defaults_keep_headroom() -> None:
    from jevscan.core.config import EvaluationConfig
    from jevscan.core.planning import RequestBudget

    default = EvaluationConfig()
    assert default.max_context_tokens == 28_000 and default.max_total_tokens == 56_000
    known = RequestBudget(EvaluationConfig(max_context_tokens=None, max_total_tokens=None), "jev-latest")
    assert known.max_context_tokens == 32_000 and known.max_total_tokens == 64_000
    pinned = RequestBudget(EvaluationConfig(max_context_tokens=None, max_total_tokens=None), "jev-1.13.0")
    assert pinned.max_context_tokens == 32_000 and pinned.max_total_tokens == 64_000
    above_provider = RequestBudget(EvaluationConfig(max_context_tokens=40_000, max_total_tokens=70_000), "jev-latest")
    assert above_provider.max_context_tokens == 32_000 and above_provider.max_total_tokens == 64_000
    unknown = RequestBudget(EvaluationConfig(max_context_tokens=None, max_total_tokens=None), "jev-future")
    assert unknown.max_context_tokens is None and unknown.max_total_tokens is None


def test_token_calibration_only_tightens_byte_estimates() -> None:
    from jevscan.core.model_limits import TokenCalibration

    calibration = TokenCalibration()
    assert calibration.effective(3.0) == 3.0
    calibration.observe(20_000, 10_000)
    assert calibration.observations == 1
    assert calibration.effective(3.0) == pytest.approx(1.8)
    calibration.observe(40_000, 10_000)
    assert calibration.observations == 2
    assert calibration.effective(3.0) == pytest.approx(1.8)


async def test_request_budget_stops_before_an_extra_paid_attempt(config: Config, unit: Unit) -> None:
    from jevscan.core.config import BudgetConfig
    from jevscan.core.protocol import BudgetExhaustedError

    plan = request_for(config, unit)
    calls = 0

    async def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_body())

    async with JevClient(
        config.jev,
        "test-key",
        transport=httpx.MockTransport(handle),
        budget=BudgetConfig(max_requests=1),
    ) as client:
        await client.evaluate(plan.body, plan.questions)
        with pytest.raises(BudgetExhaustedError, match="request budget exhausted"):
            await client.evaluate(plan.body, plan.questions)
    assert calls == 1 and client.requests == 1


async def test_cost_budget_uses_conservative_request_estimate(config: Config, unit: Unit) -> None:
    from jevscan.core.config import BudgetConfig
    from jevscan.core.protocol import BudgetExhaustedError

    plan = request_for(config, unit)

    async def never(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("budget guard should reject before transport")

    async with JevClient(
        config.jev,
        "test-key",
        transport=httpx.MockTransport(never),
        budget=BudgetConfig(max_cost=0.000000001, input_cost_per_million=0.042),
    ) as client:
        with pytest.raises(BudgetExhaustedError, match="estimated cost budget"):
            await client.evaluate(plan.body, plan.questions)
    assert client.requests == 0
