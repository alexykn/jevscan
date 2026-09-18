"""Async Jev wire client. Protocol checked against TypeSafe's Python SDK v0.6.0.

A shared limiter owns pacing for every attempt, including retries. The client never logs
API keys or request/response bodies. Source text leaves the machine only in live mode.
"""

import asyncio
import json
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jevscan import __version__
from jevscan.core.config import ChoiceQuestion, JevConfig, NoulQuestion, Rule, ScoreQuestion
from jevscan.core.context import WorkItem, questions_for
from jevscan.core.models import Finding


class JevError(RuntimeError):
    """Transport, API, or response-contract failure; never contains the submitted source."""


class RequestTooLarge(JevError):
    """A unit's request cannot be sent within the configured resource budget."""


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True, allow_inf_nan=False)


class NoulAnswer(WireModel):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


class ChoiceAnswer(WireModel):
    type: Literal["choice"]
    choice: str
    confidence: float = Field(ge=0, le=1)
    probabilities: dict[str, float]


class ScoreAnswer(WireModel):
    type: Literal["score"]
    score: float
    confidence: float = Field(ge=0, le=1)
    probabilities: dict[str, float]


Answer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]


class Usage(WireModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class JevResponse(WireModel):
    model: str
    usage: Usage = Field(default_factory=Usage)
    answers: dict[str, Answer]


def validate_response(raw: bytes | str, rules: dict[str, Rule]) -> JevResponse:
    try:
        response = JevResponse.model_validate_json(raw)
    except ValidationError as exc:
        # ValidationError can include raw input values; do not leak those to terminal/logs.
        raise JevError("Jev response does not match the expected answer schema") from exc
    if set(response.answers) != set(rules):
        raise JevError("Jev response question IDs do not match the submitted question IDs")
    for name, rule in rules.items():
        answer, question = response.answers[name], rule.question
        if isinstance(question, NoulQuestion):
            if not isinstance(answer, NoulAnswer):
                raise JevError(f"{name}: expected a noul answer")
            continue
        if isinstance(question, ChoiceQuestion):
            if not isinstance(answer, ChoiceAnswer) or answer.choice not in question.criteria:
                raise JevError(f"{name}: invalid choice answer")
            expected_keys = set(question.criteria)
        else:
            assert isinstance(question, ScoreQuestion)
            if not isinstance(answer, ScoreAnswer) or not 0 <= answer.score <= len(question.criteria) - 1:
                raise JevError(f"{name}: invalid score answer")
            expected_keys = {str(i) for i in range(len(question.criteria))}
        probabilities = answer.probabilities
        if set(probabilities) != expected_keys:
            raise JevError(f"{name}: probability labels do not match the rubric")
        if any(not 0 <= p <= 1 for p in probabilities.values()):
            raise JevError(f"{name}: invalid probability value")
    return response


def findings_from(work: WorkItem, response: JevResponse) -> list[Finding]:
    findings = []
    for name, rule in work.rules.items():
        answer, report = response.answers[name], rule.report
        probability = None
        confidence = None
        if isinstance(answer, NoulAnswer):
            probability = answer.noul if report.expected else 1 - answer.noul
            assert report.min_probability is not None
            selected = probability >= report.min_probability
            value: str | float = answer.noul
        elif isinstance(answer, ChoiceAnswer):
            probability = answer.probabilities[answer.choice]
            confidence = answer.confidence
            assert report.choices is not None and report.min_probability is not None
            selected = answer.choice in report.choices and probability >= report.min_probability
            value = answer.choice
        else:
            confidence = answer.confidence
            if report.min_score is not None:
                selected = answer.score >= report.min_score
            else:
                assert report.max_score is not None
                selected = answer.score <= report.max_score
            value = answer.score
        if report.min_confidence is not None:
            assert confidence is not None
            selected = selected and confidence >= report.min_confidence
        if selected:
            findings.append(Finding(name, report.severity, report.message, work.unit, value, probability, confidence))
    return findings


class RequestLimiter:
    def __init__(self, requests_per_minute: float) -> None:
        self.interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self.next_allowed = 0.0
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while (delay := self.next_allowed - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            self.next_allowed = time.monotonic() + self.interval

    def defer(self, seconds: float) -> None:
        # All limiter operations run on the same event loop; no cross-thread state is shared.
        self.next_allowed = max(self.next_allowed, time.monotonic() + seconds)


def _retry_after(headers: httpx.Headers) -> float | None:
    milliseconds = headers.get("retry-after-ms")
    seconds = headers.get("retry-after")
    try:
        if milliseconds is not None:
            value = float(milliseconds) / 1000
        elif seconds is not None:
            try:
                value = float(seconds)
            except ValueError:
                moment = parsedate_to_datetime(seconds)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                value = (moment - datetime.now(timezone.utc)).total_seconds()
        else:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return max(0.0, value) if math.isfinite(value) else None


def endpoint_from(value: str) -> str:
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise JevError("TYPESAFE_BASE_URL must be an origin, without credentials, path, query, or fragment")
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise JevError("TYPESAFE_BASE_URL must use HTTPS, except localhost test endpoints")
    if not parts.hostname:
        raise JevError("TYPESAFE_BASE_URL is missing its hostname")
    return value.rstrip("/")


class JevClient:
    def __init__(self, config: JevConfig, api_key: str, *, base_url: str = "https://api.typesafe.ai",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not api_key.strip():
            raise JevError("set TYPESAFE_API_KEY for live analysis, or use --offline")
        self.config = config
        self.base_url = endpoint_from(base_url)
        self.limiter = RequestLimiter(config.requests_per_minute)
        self.requests = 0
        self.http = httpx.AsyncClient(
            base_url=self.base_url, timeout=config.timeout_seconds, follow_redirects=False, transport=transport,
            limits=httpx.Limits(max_connections=config.concurrency, max_keepalive_connections=config.concurrency),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": f"jevscan/{__version__}"},
        )

    def body_for(self, work: WorkItem) -> bytes:
        if len(work.rules) > self.config.max_questions:
            raise RequestTooLarge(f"unit has more than jev.max_questions ({self.config.max_questions}) applicable rules")
        body = json.dumps({"model": self.config.model, "state": work.state, "questions": questions_for(work.rules)},
                          sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > self.config.max_request_bytes:
            raise RequestTooLarge(f"serialized request exceeds jev.max_request_bytes ({self.config.max_request_bytes})")
        return body

    async def evaluate(self, body: bytes, rules: dict[str, Rule]) -> JevResponse:
        for attempt in range(self.config.retries + 1):
            await self.limiter.acquire()
            self.requests += 1
            try:
                response = await self.http.post("/v1/systemone", content=body)
            except httpx.RequestError as exc:
                if attempt == self.config.retries:
                    raise JevError(f"Jev connection failed after {attempt + 1} attempts ({type(exc).__name__})") from exc
                await asyncio.sleep(self._backoff(attempt))
                continue
            if response.is_success:
                return validate_response(response.content, rules)
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            if not retryable or attempt == self.config.retries:
                request_id = response.headers.get("x-typesafe-request-id", "unavailable")
                raise JevError(f"Jev HTTP {response.status_code}; request ID: {request_id}")
            server_delay = _retry_after(response.headers)
            delay = server_delay if server_delay is not None else self._backoff(attempt)
            if delay > self.config.max_retry_delay:
                raise JevError("Jev requested a retry delay above jev.max_retry_delay; stopping rather than retrying early")
            if response.status_code == 429:
                self.limiter.defer(delay)
            else:
                await asyncio.sleep(delay)
        raise AssertionError("retry loop must return or raise")

    def _backoff(self, attempt: int) -> float:
        # Jitter is not security-sensitive randomness.
        return min(self.config.max_retry_delay, 0.5 * 2 ** attempt + random.random() * 0.25)  # noqa: S311

    async def __aenter__(self) -> "JevClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.http.aclose()
