"""One validated, cached prediction path for scan, compaction, and enrichment questions."""

import json
from dataclasses import dataclass

from jevscan.core.cache import AnswerCache, cache_key, judgment_cache_key
from jevscan.core.client import JevClient
from jevscan.core.model_limits import TokenCalibration
from jevscan.core.models import Summary
from jevscan.core.protocol import Answer, ContextLimitError, JevError, JevResponse, validate_answer, validate_response
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
    calibration: TokenCalibration | None = None

    async def cached_judgments(
        self, state: bytes, question_wires: dict[str, bytes], questions: dict[str, Question]
    ) -> dict[str, tuple[Answer, str]]:
        """Return validated answers independent of the HTTP batch that originally produced them."""
        if self.cache is None or not questions:
            return {}
        keys = {
            name: judgment_cache_key(self.client.base_url, self.client.config.model, state, question_wires[name])
            for name in questions
        }
        raw_by_key = await self.cache.get_judgments(tuple(keys.values()))
        cached: dict[str, tuple[Answer, str]] = {}
        for name, question in questions.items():
            raw = raw_by_key.get(keys[name])
            if raw is None:
                continue
            try:
                payload = json.loads(raw)
                response = validate_response(
                    json.dumps({
                        "model": payload["model"],
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                        "answers": {"cached": payload["answer"]},
                    }),
                    {"cached": question},
                )
            except (KeyError, TypeError, ValueError, JevError) as exc:
                raise JevError("cached judgment does not match the expected answer schema") from exc
            cached[name] = response.answers["cached"], response.model
        self.summary.cache_hits += len(cached)
        return cached

    async def store_judgments(
        self,
        state: bytes,
        question_wires: dict[str, bytes],
        questions: dict[str, Question],
        response: JevResponse,
    ) -> None:
        """Persist each answer under its own exact evidence/question identity."""
        if self.cache is None:
            return
        entries = []
        for name, question in questions.items():
            answer = response.answers[name]
            validate_answer(answer, question, name)
            key = judgment_cache_key(self.client.base_url, self.client.config.model, state, question_wires[name])
            body = json.dumps(
                {"model": response.model, "answer": answer.model_dump(mode="json")},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            entries.append((key, body))
        await self.cache.put_judgments(tuple(entries))

    async def predict(
        self, body: bytes, questions: dict[str, Question], *, enrichment: bool = False, compaction: bool = False
    ) -> Prediction:
        if compaction:
            self.summary.compaction_calls += 1
        if enrichment:
            self.summary.enrichment_calls += 1
        key = cache_key(self.client.base_url, body)
        raw = await self.cache.get(key) if self.cache else None
        if raw is not None:
            response = validate_response(raw, questions)
            self.summary.cache_hits += 1
            self.summary.enrichment_cache_hits += enrichment
            self.summary.compaction_cache_hits += compaction
            if self.calibration is not None:
                self.calibration.observe(len(body), response.usage.input_tokens)
            return Prediction(response, True)
        phase = "compaction" if compaction else "enrichment" if enrichment else "evaluation"
        reserved_before = self.client.estimated_input_tokens
        try:
            response = await self.client.evaluate(body, questions)
        except ContextLimitError:
            self.summary.size_rejections += 1
            raise
        finally:
            reserved = self.client.estimated_input_tokens - reserved_before
            setattr(
                self.summary,
                f"{phase}_reserved_input_tokens",
                getattr(self.summary, f"{phase}_reserved_input_tokens") + reserved,
            )
        if self.calibration is not None:
            self.calibration.observe(len(body), response.usage.input_tokens)
        input_tokens = response.usage.input_tokens or 0
        self.summary.input_tokens += input_tokens
        setattr(self.summary, f"{phase}_input_tokens", getattr(self.summary, f"{phase}_input_tokens") + input_tokens)
        self.summary.output_tokens += response.usage.output_tokens or 0
        if self.cache:
            await self.cache.put(key, response.model_dump_json().encode())
        return Prediction(response, False)
