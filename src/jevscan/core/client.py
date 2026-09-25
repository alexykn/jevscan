"""Async Jev transport; all attempts, including size-recovery requests, share pacing.

The client owns transport admission, paid-attempt accounting, and retry policy.
It never logs API keys or request/response bodies.
"""

import asyncio
import hashlib
import math
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import SplitResult, urlsplit

import httpx

from jevscan import __version__
from jevscan.core.config import BudgetConfig, JevConfig
from jevscan.core.protocol import (
    BudgetExhaustedError,
    ContextLimitError,
    JevError,
    JevResponse,
    RequestRejectedError,
    validate_response,
)
from jevscan.core.rules import Question


@dataclass(slots=True)
class ReservationUsage:
    """Request-local conservative input reservations, including retries."""

    input_tokens: int = 0


@dataclass(slots=True)
class AttemptLedger:
    """Own paid-attempt admission and the counters derived from it."""

    budget: BudgetConfig
    bytes_per_token: float
    token_reserve: int
    requests: int = 0
    completed_requests: int = 0
    retry_attempts: int = 0
    estimated_input_tokens: int = 0
    estimated_cost: float = 0.0

    def _project(self, body: bytes) -> tuple[int, int, int, float]:
        estimated = math.ceil(len(body) / self.bytes_per_token) + self.token_reserve
        requests = self.requests + 1
        tokens = self.estimated_input_tokens + estimated
        cost = tokens * self.budget.input_cost_per_million / 1_000_000
        return estimated, requests, tokens, cost

    def _validate_projection(self, requests: int, tokens: int, cost: float) -> None:
        limits = (
            (
                self.budget.max_requests is None or requests <= self.budget.max_requests,
                f"request budget exhausted at {self.requests} requests",
            ),
            (
                self.budget.max_input_tokens is None or tokens <= self.budget.max_input_tokens,
                f"input-token budget would be exceeded ({tokens} > {self.budget.max_input_tokens})",
            ),
            (
                self.budget.max_cost is None or cost <= self.budget.max_cost,
                f"estimated cost budget would be exceeded ({cost:.4f} > {self.budget.max_cost:.4f})",
            ),
        )
        for valid, message in limits:
            if not valid:
                raise BudgetExhaustedError(message)

    def admit(self, body: bytes) -> int:
        estimated, requests, tokens, cost = self._project(body)
        self._validate_projection(requests, tokens, cost)
        self.requests = requests
        self.estimated_input_tokens = tokens
        self.estimated_cost = cost
        return estimated

    def complete(self) -> None:
        self.completed_requests += 1

    def retry(self) -> None:
        self.retry_attempts += 1


def _safe_request_id(response: httpx.Response) -> str:
    value = response.headers.get("x-typesafe-request-id", "")[:100]
    return "".join(c for c in value if c.isalnum() or c in "-_")


def _safe_machine_value(value: object) -> str | None:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        return None
    return value if all(c.isalnum() or c in "._:-" for c in value) else None


def _machine_fields(body: object) -> dict[str, tuple[str, ...]]:
    """Keep bounded machine tokens only; never retain free-text messages or response bodies."""
    found: dict[str, set[str]] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key)
                if key in {"code", "type", "status", "error"}:
                    safe = _safe_machine_value(child)
                    if safe is not None:
                        found.setdefault(key, set()).add(safe)
                visit(child)
        elif isinstance(value, list):
            for child in value[:64]:
                visit(child)

    visit(body)
    return {key: tuple(sorted(values)) for key, values in sorted(found.items())}


def _json_body(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _context_rejection(response: httpx.Response) -> ContextLimitError | None:
    """Recognize published size signals from bounded machine fields, never free text."""
    code = "content_too_large" if response.status_code == 413 else ""
    if response.status_code in {400, 422}:
        fields = _machine_fields(_json_body(response))
        if any("max_tokens_exceeded" in values for values in fields.values()):
            code = "max_tokens_exceeded"
    if not code:
        return None
    return ContextLimitError(
        "Jev rejected the request's context size",
        status=response.status_code,
        code=code,
        request_id=_safe_request_id(response),
    )


def _request_rejection(response: httpx.Response) -> RequestRejectedError | None:
    if response.status_code not in {400, 422}:
        return None
    body = _json_body(response)
    return RequestRejectedError(
        status=response.status_code,
        machine_fields=_machine_fields(body),
        request_id=_safe_request_id(response),
        fingerprint=hashlib.sha256(response.content).hexdigest()[:16],
    )


@dataclass(slots=True)
class RejectionTracker:
    """Record provider rejection state and classify the resulting failure."""

    counts: dict[tuple[object, ...], int]

    def record(self, response: httpx.Response) -> Exception | None:
        context_rejection = _context_rejection(response)
        if context_rejection is not None:
            return context_rejection
        request_rejection = _request_rejection(response)
        if request_rejection is None:
            return None
        signature = request_rejection.signature
        count = self.counts.get(signature, 0) + 1
        self.counts[signature] = count
        if count >= 3:
            return JevError(
                f"Jev rejected {count} equivalent requests (HTTP {response.status_code}); "
                "stopping because the failure appears systemic"
            )
        return request_rejection


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


def _retry_seconds(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return (moment - datetime.now(UTC)).total_seconds()


def _retry_after(headers: httpx.Headers) -> float | None:
    milliseconds = headers.get("retry-after-ms")
    seconds = headers.get("retry-after")
    try:
        if milliseconds is not None:
            value = float(milliseconds) / 1000
        elif seconds is not None:
            value = _retry_seconds(seconds)
        else:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return max(0.0, value) if math.isfinite(value) else None


def _origin_only(parts: SplitResult) -> bool:
    return not any((parts.username, parts.password, parts.query, parts.fragment)) and parts.path in {"", "/"}


def _allowed_scheme(parts: SplitResult) -> bool:
    localhost = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    return parts.scheme == "https" or (parts.scheme == "http" and localhost)


def endpoint_from(value: str) -> str:
    parts = urlsplit(value)
    if not _origin_only(parts):
        raise JevError("TYPESAFE_BASE_URL must be an origin, without credentials, path, query, or fragment")
    if not _allowed_scheme(parts):
        raise JevError("TYPESAFE_BASE_URL must use HTTPS, except localhost test endpoints")
    if not parts.hostname:
        raise JevError("TYPESAFE_BASE_URL is missing its hostname")
    return value.rstrip("/")


class JevClient:
    def __init__(
        self,
        config: JevConfig,
        api_key: str,
        *,
        base_url: str = "https://api.typesafe.ai",
        transport: httpx.AsyncBaseTransport | None = None,
        budget: BudgetConfig | None = None,
        bytes_per_token: float = 3.0,
        token_reserve: int = 0,
    ) -> None:
        if not api_key.strip():
            raise JevError("set TYPESAFE_API_KEY for live analysis, or use --offline")
        self.config = config
        self.base_url = endpoint_from(base_url)
        self.limiter = RequestLimiter(config.requests_per_minute)
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.ledger = AttemptLedger(budget or BudgetConfig(), bytes_per_token, token_reserve)
        self.rejections = RejectionTracker({})
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=config.concurrency, max_keepalive_connections=config.concurrency),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"jevscan/{__version__}",
            },
        )

    @property
    def requests(self) -> int:
        return self.ledger.requests

    @property
    def completed_requests(self) -> int:
        return self.ledger.completed_requests

    @property
    def retry_attempts(self) -> int:
        return self.ledger.retry_attempts

    @property
    def estimated_input_tokens(self) -> int:
        return self.ledger.estimated_input_tokens

    @property
    def estimated_cost(self) -> float:
        return self.ledger.estimated_cost

    async def _execute_paid_attempt(self, body: bytes, reservation: ReservationUsage | None) -> httpx.Response:
        async with self.semaphore:
            # Pace actual transport starts, not tasks waiting for a connection slot.
            await self.limiter.acquire()
            reserved = self.ledger.admit(body)
            if reservation is not None:
                reservation.input_tokens += reserved
            try:
                return await self.http.post("/v1/systemone", content=body)
            finally:
                self.ledger.complete()

    async def _retry_transport(self, error: httpx.RequestError, attempt: int) -> None:
        if attempt == self.config.retries:
            raise JevError(f"Jev connection failed after {attempt + 1} attempts ({type(error).__name__})") from error
        await asyncio.sleep(self._backoff(attempt))

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        server_delay = _retry_after(response.headers)
        delay = server_delay if server_delay is not None else self._backoff(attempt)
        if delay > self.config.max_retry_delay:
            raise JevError("Jev requested a retry delay above jev.max_retry_delay; stopping rather than retrying early")
        return delay

    @staticmethod
    def _retryable(response: httpx.Response) -> bool:
        return response.status_code in {408, 429} or response.status_code >= 500

    async def _retry_response(self, response: httpx.Response, attempt: int) -> None:
        if not self._retryable(response) or attempt == self.config.retries:
            request_id = _safe_request_id(response) or "unavailable"
            raise JevError(f"Jev HTTP {response.status_code}; request ID: {request_id}")
        delay = self._retry_delay(response, attempt)
        if response.status_code == 429:
            self.limiter.defer(delay)
            return
        await asyncio.sleep(delay)

    async def evaluate(
        self,
        body: bytes,
        questions: dict[str, Question],
        *,
        reservation: ReservationUsage | None = None,
    ) -> JevResponse:
        for attempt in range(self.config.retries + 1):
            if attempt:
                self.ledger.retry()
            try:
                response = await self._execute_paid_attempt(body, reservation)
            except httpx.RequestError as exc:
                await self._retry_transport(exc, attempt)
                continue
            if response.is_success:
                return validate_response(response.content, questions)
            rejection = self.rejections.record(response)
            if rejection is not None:
                raise rejection
            await self._retry_response(response, attempt)
        raise AssertionError("retry loop must return or raise")

    def _backoff(self, attempt: int) -> float:
        # Jitter is not security-sensitive randomness.
        return min(self.config.max_retry_delay, 0.5 * 2**attempt + random.random() * 0.25)  # noqa: S311

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.http.aclose()
