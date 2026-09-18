"""One validated, cached prediction path for both scan and enrichment questions."""

from dataclasses import dataclass

from jevscan.core.cache import AnswerCache, cache_key
from jevscan.core.client import JevClient
from jevscan.core.models import Summary
from jevscan.core.protocol import JevResponse, validate_response
from jevscan.core.rules import Question


@dataclass(frozen=True, slots=True)
class Prediction:
    response: JevResponse
    cached: bool


@dataclass(slots=True)
class Inference:
    client: JevClient
    cache: AnswerCache | None
    summary: Summary

    async def predict(self, body: bytes, questions: dict[str, Question], *, enrichment: bool = False) -> Prediction:
        if enrichment:
            self.summary.enrichment_calls += 1
        key = cache_key(self.client.base_url, body)
        raw = await self.cache.get(key) if self.cache else None
        if raw is not None:
            response = validate_response(raw, questions)
            self.summary.cache_hits += 1
            self.summary.enrichment_cache_hits += enrichment
            return Prediction(response, True)
        response = await self.client.evaluate(body, questions)
        self.summary.input_tokens += response.usage.input_tokens or 0
        self.summary.output_tokens += response.usage.output_tokens or 0
        if self.cache:
            await self.cache.put(key, response.model_dump_json().encode())
        return Prediction(response, False)
