"""Opt-in final judgment capture without changing normal scan reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from jevscan.core.assessment import Assessment
from jevscan.core.protocol import (
    PROMPT_VERSION,
    QUESTION_POLICY,
    PromptRegistry,
    encode,
    validate_prompt_registry,
)

if TYPE_CHECKING:
    from jevscan.core.evaluation import Judgment


class FinalJudgmentSink(Protocol):
    def _evidence_state(self, judgment: Judgment) -> dict[str, Any]:
        if judgment.wire_state:
            return json.loads(judgment.wire_state)
        registry = PromptRegistry.for_question(judgment.check.rule.question)
        return registry.bind_state(judgment.context.state)

    @staticmethod
    def _question_wire(judgment: Judgment) -> dict[str, Any]:
        question_wire = judgment.question_wire or judgment.check.question()
        if question_wire != judgment.check.question():
            raise ValueError("final judgment question does not match its canonical wire")
        return question_wire

    @staticmethod
    def _final_sources(
        evidence: Mapping[str, Any],
        source_documents: Mapping[str, str],
    ) -> dict[str, str]:
        paths = {
            document.get("path")
            for document in evidence.get("documents", [])
            if isinstance(document, dict)
        }
        return {path: source_documents[path] for path in paths if path in source_documents}

    @staticmethod
    def _capture_review(
        judgment: Judgment,
        review: Mapping[str, Any] | None,
        source_documents: Mapping[str, str],
    ) -> dict[str, Any]:
        capture_review = dict(review or judgment.review)
        initial_state = capture_review.get("initial_evidence_state")
        if initial_state is not None:
            _validate_sources(initial_state, source_documents)
        return capture_review

    def _row(
        self,
        judgment: Judgment,
        assessment: Assessment,
        source_documents: Mapping[str, str],
        review: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        evidence = self._evidence_state(judgment)
        question_wire = self._question_wire(judgment)
        validate_prompt_registry(evidence, judgment.check.rule.question)
        final_sources = self._final_sources(evidence, source_documents)
        _validate_sources(evidence, final_sources)
        capture_review = self._capture_review(judgment, review, source_documents)
        return {
            "version": 1,
            "phase": "final",
            "kind": "judgment",
            "case_id": _stable_case_id(judgment, source_documents),
            "rule_id": judgment.check.rule_id,
            "rule": judgment.check.rule.model_dump(mode="json"),
            "target": judgment.check.target.metadata(),
            "answer": judgment.answer.model_dump(mode="json"),
            "returned_model": judgment.model,
            "requested_model": self.requested_model,
            "endpoint": self.endpoint,
            "cached": bool(getattr(judgment, "cached", False)),
            "fully_cached": bool(getattr(judgment, "fully_cached", False)),
            "prompt": {"version": PROMPT_VERSION, "policy": QUESTION_POLICY},
            "question_wire": question_wire,
            "context_complete": judgment.evidence["context_complete"],
            "target_complete": judgment.evidence["target_complete"],
            "evidence": {"state": evidence, "source_documents": final_sources},
            "assessment": assessment_record(assessment),
            "disposition": {"status": assessment.status, "reason": assessment.reason},
            "review": capture_review,
            "inference": judgment.inference,
        }

    def record(
        self,
        judgment: Judgment,
        assessment: Assessment,
        source_documents: Mapping[str, str],
        review: Mapping[str, Any] | None = None,
    ) -> None:
        self._write_row(self._row(judgment, assessment, source_documents, review))

    def _write_row(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()
            self._digest.update(line.encode("utf-8"))
            self._records += 1

    def record_skip(self, target: Any, rule_id: str, reason: str, kind: str = "skip") -> None:
        identity = encode({"target": target.metadata(), "rule_id": rule_id})
        row = {
            "version": 1,
            "phase": "final",
            "kind": kind,
            "case_id": f"skip-{hashlib.sha256(identity).hexdigest()[:24]}",
            "rule_id": rule_id,
            "target": target.metadata(),
            "reason": reason,
        }
        self._write_row(row)

    def mark_complete(self, complete: bool, outcome: str) -> None:
        self._completed = complete
        self.metadata = {**self.metadata, "outcome": outcome}

    def close(self) -> None:
        self._stream.close()
        self._meta_path.write_text(
            json.dumps(
                {
                    **self.metadata,
                    "version": 1,
                    "phase": "final",
                    "requested_model": self.requested_model,
                    "endpoint": self.endpoint,
                    "records": self._records,
                    "complete": self._completed,
                    "capture_sha256": "sha256:" + self._digest.hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
