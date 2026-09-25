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
    def record(
        self,
        judgment: Judgment,
        assessment: Assessment,
        source_documents: Mapping[str, str],
        review: Mapping[str, Any] | None = None,
    ) -> None: ...

    def record_skip(self, target: Any, rule_id: str, reason: str, kind: str = "skip") -> None: ...


def _finding(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "rule": value.rule,
        "severity": str(value.severity),
        "message": value.message,
        "target": value.target.metadata(),
        "value": value.value,
        "probability": value.probability,
        "confidence": value.confidence,
    }


def assessment_record(value: Assessment) -> dict[str, Any]:
    return {
        "status": value.status,
        "reason": value.reason,
        "finding": _finding(value.finding),
        "tentative_finding": _finding(value.tentative_finding),
    }


def _judgment_evidence_state(judgment: Judgment) -> dict[str, Any]:
    if judgment.wire_state:
        return json.loads(judgment.wire_state)
    registry = PromptRegistry.for_question(judgment.check.rule.question)
    return registry.bind_state(judgment.context.state)


def _stable_case_id(judgment: Judgment, source_documents: Mapping[str, str]) -> str:
    state = _judgment_evidence_state(judgment)
    identity = encode({
        "target": judgment.check.target.metadata(),
        "rule_id": judgment.check.rule_id,
        "question": judgment.question_wire or judgment.check.question(),
        "model": judgment.model,
        "evidence_sha256": hashlib.sha256(encode(state)).hexdigest(),
        "source_documents": {
            path: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for path, content in sorted(source_documents.items())
        },
    })
    return "final-" + hashlib.sha256(identity).hexdigest()[:24]


def _evidence_documents(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    documents = state.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("final judgment evidence has no documents")
    if any(not isinstance(document, dict) for document in documents):
        raise TypeError("final judgment evidence document is not an object")
    return documents


def _validate_source_document(document: dict[str, Any], source_documents: Mapping[str, str]) -> None:
    path = document.get("path")
    if not isinstance(path, str) or path not in source_documents:
        raise ValueError(f"final judgment source snapshot is missing {path!r}")
    content = document.get("content")
    if not isinstance(content, str):
        raise TypeError(f"final judgment evidence content is invalid for {path!r}")
    start, end = document.get("start_byte"), document.get("end_byte")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError(f"final judgment evidence is not a slice of its authoritative source snapshot: {path}")
    source = source_documents[path].encode("utf-8")
    if source[start:end].decode("utf-8") != content:
        raise ValueError(f"final judgment evidence is not a slice of its authoritative source snapshot: {path}")


def _validate_sources(state: Mapping[str, Any], source_documents: Mapping[str, str]) -> None:
    for document in _evidence_documents(state):
        _validate_source_document(document, source_documents)


def _question_wire(judgment: Judgment) -> dict[str, Any]:
    question_wire = judgment.question_wire or judgment.check.question()
    if question_wire != judgment.check.question():
        raise ValueError("final judgment question does not match its canonical wire")
    return question_wire


def _final_sources(
    evidence: Mapping[str, Any],
    source_documents: Mapping[str, str],
) -> dict[str, str]:
    paths = {document.get("path") for document in evidence.get("documents", []) if isinstance(document, dict)}
    return {path: source_documents[path] for path in paths if path in source_documents}


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


@dataclass(slots=True)
class FinalJudgmentRecorder:
    """Write immutable final judgments and exact source snapshots to JSONL."""

    path: Path
    requested_model: str
    endpoint: str
    metadata: dict[str, Any]
    _lock: Lock = field(init=False, repr=False)
    _stream: Any = field(init=False, repr=False)
    _records: int = field(init=False, repr=False)
    _completed: bool = field(init=False, repr=False)
    _digest: Any = field(init=False, repr=False)
    _meta_path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        meta_path = self.path.with_suffix(".meta.json")
        self._lock = Lock()
        self._records = 0
        self._completed = False
        self._digest = hashlib.sha256()
        self._meta_path = meta_path
        self._stream = self.path.open("x", encoding="utf-8")
        try:
            meta_path.open("x", encoding="utf-8").close()
        except BaseException:
            self._stream.close()
            self.path.unlink(missing_ok=True)
            raise

    def _row(
        self,
        judgment: Judgment,
        assessment: Assessment,
        source_documents: Mapping[str, str],
        review: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        evidence = _judgment_evidence_state(judgment)
        question_wire = _question_wire(judgment)
        validate_prompt_registry(evidence, judgment.check.rule.question)
        final_sources = _final_sources(evidence, source_documents)
        _validate_sources(evidence, final_sources)
        capture_review = _capture_review(judgment, review, source_documents)
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
