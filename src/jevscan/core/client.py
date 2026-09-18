"""Async Jev transport; all attempts, including size-recovery requests, share pacing.

A shared limiter owns pacing for every attempt, including retries. The client never logs
API keys or request/response bodies. Source text leaves the machine only in live mode.
"""

import asyncio
import math
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import urlsplit

import httpx

from jevscan import __version__
from jevscan.core.config import JevConfig
from jevscan.core.protocol import ContextLimitError, JevError, JevResponse, validate_response
from jevscan.core.rules import Question


def _context_rejected(response: httpx.Response) -> bool:
    if response.status_code == 413:
        return True
    if response.status_code not in {400, 422}:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    error = body.get("error", body.get("detail", body))
    return isinstance(error, dict) and error.get("code") == "max_tokens_exceeded"


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
                    moment = moment.replace(tzinfo=UTC)
                value = (moment - datetime.now(UTC)).total_seconds()
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
    def __init__(
        self,
        config: JevConfig,
        api_key: str,
        *,
        base_url: str = "https://api.typesafe.ai",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise JevError("set TYPESAFE_API_KEY for live analysis, or use --offline")
        self.config = config
        self.base_url = endpoint_from(base_url)
        self.limiter = RequestLimiter(config.requests_per_minute)
        self.requests = 0
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

    async def evaluate(self, body: bytes, questions: dict[str, Question]) -> JevResponse:
        for attempt in range(self.config.retries + 1):
            await self.limiter.acquire()
            self.requests += 1
            try:
                response = await self.http.post("/v1/systemone", content=body)
            except httpx.RequestError as exc:
                if attempt == self.config.retries:
                    raise JevError(
                        f"Jev connection failed after {attempt + 1} attempts ({type(exc).__name__})"
                    ) from exc
                await asyncio.sleep(self._backoff(attempt))
                continue
            if response.is_success:
                return validate_response(response.content, questions)
            if _context_rejected(response):
                request_id = response.headers.get("x-typesafe-request-id", "")[:100]
                request_id = "".join(c for c in request_id if c.isalnum() or c in "-_")
                raise ContextLimitError(
                    "Jev rejected the request's context size",
                    status=response.status_code,
                    code="payload_too_large" if response.status_code == 413 else "max_tokens_exceeded",
                    request_id=request_id,
                )
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            if not retryable or attempt == self.config.retries:
                request_id = response.headers.get("x-typesafe-request-id", "unavailable")[:100]
                request_id = "".join(c for c in request_id if c.isalnum() or c in "-_")
                raise JevError(f"Jev HTTP {response.status_code}; request ID: {request_id}")
            server_delay = _retry_after(response.headers)
            delay = server_delay if server_delay is not None else self._backoff(attempt)
            if delay > self.config.max_retry_delay:
                raise JevError(
                    "Jev requested a retry delay above jev.max_retry_delay; stopping rather than retrying early"
                )
            if response.status_code == 429:
                self.limiter.defer(delay)
            else:
                await asyncio.sleep(delay)
        raise AssertionError("retry loop must return or raise")

    def _backoff(self, attempt: int) -> float:
        # Jitter is not security-sensitive randomness.
        return min(self.config.max_retry_delay, 0.5 * 2**attempt + random.random() * 0.25)  # noqa: S311

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.http.aclose()
