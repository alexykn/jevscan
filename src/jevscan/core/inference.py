"""One validated, cached prediction path for scan, compaction, and enrichment questions."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from jevscan.core.cache import AnswerCache, cache_key, judgment_cache_key
from jevscan.core.client import JevClient, ReservationUsage
from jevscan.core.model_limits import TokenCalibration
from jevscan.core.models import Summary
from jevscan.core.protocol import (
    Answer,
    ContextLimitError,
    JevResponse,
    decode_cached_answer,
    validate_answer,
    validate_response,
)
from jevscan.core.rules import Question


@dataclass(frozen=True, slots=True)
class Prediction:
    response: JevResponse
    cached: bool
    metrics: dict[str, Any] = field(default_factory=dict)


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
            cached[name] = decode_cached_answer(raw, question)
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

    def _record_phase_call(self, enrichment: bool, compaction: bool) -> str:
        if compaction:
            self.summary.compaction_calls += 1
            return "compaction"
        if enrichment:
            self.summary.enrichment_calls += 1
            return "enrichment"
        return "evaluation"

    def _observe_calibration(self, body: bytes, response: JevResponse) -> None:
        if self.calibration is not None:
            self.calibration.observe(len(body), response.usage.input_tokens)

    def _record_cached(self, response: JevResponse, *, enrichment: bool, compaction: bool) -> None:
        self.summary.cache_hits += 1
        self.summary.enrichment_cache_hits += enrichment
        self.summary.compaction_cache_hits += compaction
        self._observe_calibration(b"", response)

    async def _cached_response(
        self,
        key: bytes,
        body: bytes,
        questions: dict[str, Question],
        *,
        enrichment: bool,
        compaction: bool,
    ) -> JevResponse | None:
        raw = await self.cache.get(key) if self.cache else None
        if raw is None:
            return None
        response = validate_response(raw, questions)
        self.summary.cache_hits += 1
        self.summary.enrichment_cache_hits += enrichment
        self.summary.compaction_cache_hits += compaction
        self._observe_calibration(body, response)
        return response

    def _reserve_phase(self, phase: str, reservation: ReservationUsage) -> None:
        field = f"{phase}_reserved_input_tokens"
        setattr(self.summary, field, getattr(self.summary, field) + reservation.input_tokens)

    def _record_live_usage(self, phase: str, body: bytes, response: JevResponse) -> None:
        self._observe_calibration(body, response)
        input_tokens = response.usage.input_tokens or 0
        self.summary.input_tokens += input_tokens
        field = f"{phase}_input_tokens"
        setattr(self.summary, field, getattr(self.summary, field) + input_tokens)
        self.summary.output_tokens += response.usage.output_tokens or 0

    async def _live_response(
        self,
        phase: str,
        body: bytes,
        questions: dict[str, Question],
    ) -> JevResponse:
        reservation = ReservationUsage()
        try:
            return await self.client.evaluate(body, questions, reservation=reservation)
        except ContextLimitError:
            self.summary.size_rejections += 1
            raise
        finally:
            self._reserve_phase(phase, reservation)

    async def _cache_response(self, key: bytes, response: JevResponse) -> None:
        if self.cache is not None:
            await self.cache.put(key, response.model_dump_json().encode())

    async def predict(
        self,
        body: bytes,
        questions: dict[str, Question],
        *,
        enrichment: bool = False,
        compaction: bool = False,
        state_bytes: int | None = None,
        question_bytes: int | None = None,
        state: dict[str, Any] | None = None,
    ) -> Prediction:
        phase = self._record_phase_call(enrichment, compaction)
        key = cache_key(self.client.base_url, body)
        metrics = self._metrics(body, questions, state_bytes, question_bytes, state)

        cached = await self._cached_response(
            key,
            body,
            questions,
            enrichment=enrichment,
            compaction=compaction,
        )
        if cached is not None:
            self._usage(metrics, cached)
            return Prediction(cached, True, metrics)

        response = await self._live_response(phase, body, questions)
        self._record_live_usage(phase, body, response)
        await self._cache_response(key, response)
        self._usage(metrics, response)
        return Prediction(response, False, metrics)

    @staticmethod
    def _metrics(
        body: bytes,
        questions: dict[str, Question],
        state_bytes: int | None,
        question_bytes: int | None,
        state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "input_bytes": len(body),
            "question_count": len(questions),
            "state_bytes": state_bytes,
            "question_bytes": question_bytes,
        }
        if state is not None:
            metrics.update(Inference._evidence_metrics(state))
        return metrics

    @staticmethod
    def _evidence_metrics(state: dict[str, Any]) -> dict[str, Any]:
        documents = state.get("documents", [])
        valid = [document for document in documents if isinstance(document, dict)]
        paths = {document.get("path") for document in valid}
        source_bytes = sum(
            len(content.encode("utf-8"))
            for document in valid
            if isinstance((content := document.get("content", "")), str)
        )
        return {
            "evidence_documents": len(documents),
            "evidence_files": len(paths),
            "evidence_source_bytes": source_bytes,
            "evidence_group_density": round(len(documents) / max(1, len(paths)), 3),
        }

    @staticmethod
    def _usage(metrics: dict[str, Any], response: JevResponse) -> None:
        metrics["input_tokens"] = response.usage.input_tokens
        metrics["output_tokens"] = response.usage.output_tokens
