"""Strict semantic-calibration case schema, provenance, and identity validation."""

import hashlib
import json
from bisect import bisect_left
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, TextIO, cast

from pydantic import (
    AliasChoices,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from jevscan.core.assessment import assess
from jevscan.core.calibration_capture_validation import has_canonical_not_applicable_route
from jevscan.core.context import Evidence
from jevscan.core.models import Kind, Target
from jevscan.core.protocol import Answer, Check, JevError, encode, prompt_binder, validate_answer, validate_prompt_registry
from jevscan.core.rules import Rule, StrictModel

CALIBRATION_VERSION = 1
CalibrationLabel = Literal["Agree", "Partial", "Disagree"]
AdjudicatedSeverity = Literal["warning", "error"]
Sha256Hash = Annotated[StrictStr, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


class TargetRecord(StrictModel):
    """Strict JSON representation of the internal target dataclass."""

    id: StrictStr = Field(min_length=1)
    scope: Literal["unit", "file"]
    path: StrictStr = Field(min_length=1)
    language: StrictStr = Field(min_length=1)
    qualified_name: StrictStr = Field(min_length=1)
    start_byte: StrictInt = Field(ge=0)
    end_byte: StrictInt = Field(ge=0)
    start_line: StrictInt = Field(ge=1)
    end_line: StrictInt = Field(ge=1)
    kind: Kind | None = None
    display_name: StrictStr = ""

    @model_validator(mode="after")
    def ordered_span(self) -> "TargetRecord":
        if self.end_byte < self.start_byte or self.end_line < self.start_line:
            raise ValueError("target end must not precede target start")
        return self

    def to_target(self) -> Target:
        return Target(**self.model_dump())


class PromptMaterial(StrictModel):
    """The prompt contract whose identity is bound to a calibration case."""

    version: StrictInt = Field(ge=1)
    policy: StrictStr = Field(min_length=1)


class PromptCompatibility(StrictModel):
    """The prompt fields that must be equal for paired model comparisons."""

    version: StrictInt = Field(ge=1)
    identity: Sha256Hash


class IdentityHashes(StrictModel):
    """All hashes declared by a case, including audit-only identities."""

    question: Sha256Hash
    evidence: Sha256Hash
    source_documents: dict[StrictStr, Sha256Hash] = Field(min_length=1)
    rule: Sha256Hash
    report: Sha256Hash
    prompt: Sha256Hash
    endpoint: Sha256Hash
    requested_model: Sha256Hash
    returned_model: Sha256Hash

    @field_validator("source_documents")
    @classmethod
    def valid_source_names(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not name for name in value):
            raise ValueError("source document names must be nonempty")
        return value


class EvidenceMaterial(StrictModel):
    """Exact encoded evidence state plus complete source material.

    ``state`` is the object passed to the provider and is hashed with the
    normal protocol ``encode`` function. ``source_documents`` contains the
    complete UTF-8 documents from which the state was selected, allowing
    source-content identities to be recomputed instead of merely trusted.
    """

    state: dict[str, Any] = Field(min_length=1)
    source_documents: dict[StrictStr, StrictStr] = Field(min_length=1)

    @field_validator("source_documents")
    @classmethod
    def valid_source_names(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not name for name in value):
            raise ValueError("evidence source document names must be nonempty")
        return value

    @model_validator(mode="after")
    def validate_documents(self) -> "EvidenceMaterial":
        documents = self.state.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ValueError("evidence.state.documents must be a nonempty list")

        paths = {
            _validate_evidence_document(index, document, self.source_documents)
            for index, document in enumerate(documents)
        }
        if paths != set(self.source_documents):
            raise ValueError("evidence source material must exactly match evidence document paths")
        return self


class FinalDisposition(StrictModel):
    status: Literal["ok", "warning", "error", "unknown", "not_applicable"]
    reason: StrictStr


class FinalCaptureMaterial(StrictModel):
    """The final production judgment and its exact review provenance."""

    phase: Literal["final"]
    question_wire: dict[str, Any] = Field(min_length=1)
    evidence: EvidenceMaterial
    answer: Answer
    disposition: FinalDisposition
    context_complete: StrictBool
    target_complete: StrictBool
    returned_model: StrictStr = Field(min_length=1)
    assessment: dict[str, Any] = Field(min_length=1)
    initial: dict[str, Any] | None = None
    review: dict[str, Any] = Field(default_factory=dict)


def _validate_evidence_document(index: int, document: Any, sources: Mapping[str, str]) -> str:
    if not isinstance(document, Mapping):
        raise TypeError(f"evidence.state.documents[{index}] must be an object")
    path = document.get("path")
    content = document.get("content")
    if not isinstance(path, str) or not path:
        raise ValueError(f"evidence.state.documents[{index}].path must be nonempty")
    if not isinstance(content, str):
        raise TypeError(f"evidence.state.documents[{index}].content must be a string")
    source = sources.get(path)
    if source is None:
        raise ValueError(f"evidence source material is missing {path!r}")
    _validate_evidence_slice(index, document, source, content)
    return path


def _validate_evidence_slice(index: int, document: Mapping[str, Any], source: str, content: str) -> None:
    start, end = document.get("start_byte"), document.get("end_byte")
    source_bytes = source.encode("utf-8")
    _validate_byte_span(
        source_bytes,
        start,
        end,
        f"evidence.state.documents[{index}] has invalid byte offsets",
        f"evidence.state.documents[{index}] byte offsets are outside UTF-8 boundaries",
    )
    start, end = cast(int, start), cast(int, end)
    _validate_lines(index, document, source_bytes, start, end)
    selected = source_bytes[start:end].decode("utf-8")
    if selected != content:
        raise ValueError(f"evidence.state.documents[{index}] content is not its source slice")


def _is_codepoint_boundary(source: bytes, offset: int) -> bool:
    return offset == 0 or offset == len(source) or source[offset] & 0xC0 != 0x80


def _validate_byte_span(
    source: bytes,
    start: Any,
    end: Any,
    message: str,
    boundary_message: str | None = None,
) -> None:
    if not _is_integer(start) or not _is_integer(end):
        raise ValueError(message)
    if start < 0:
        raise ValueError(message)
    if end < start:
        raise ValueError(message)
    if end > len(source):
        raise ValueError(message)
    if not _is_codepoint_boundary(source, start):
        raise ValueError(boundary_message or message)
    if not _is_codepoint_boundary(source, end):
        raise ValueError(boundary_message or message)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return _is_integer(value) and value >= 1


def _line_for_offset(source: bytes, offset: int) -> int:
    newlines = [index for index, byte in enumerate(source) if byte == 10]
    return bisect_left(newlines, offset) + 1


def _validate_lines(
    index: int,
    document: Mapping[str, Any],
    source: bytes,
    start: int,
    end: int,
) -> None:
    start_line, end_line = document.get("start_line"), document.get("end_line")
    if not _is_positive_int(start_line) or not _is_positive_int(end_line):
        raise ValueError(f"evidence.state.documents[{index}] has invalid line numbers")
    expected = (_line_for_offset(source, start), _line_for_offset(source, max(start, end - 1)))
    if (start_line, end_line) != expected:
        raise ValueError(f"evidence.state.documents[{index}] line numbers do not match its UTF-8 byte span")


def _validate_target_span(target: Target, source: str) -> None:
    source_bytes = source.encode("utf-8")
    start, end = target.start_byte, target.end_byte
    _validate_byte_span(source_bytes, start, end, "target byte span is outside UTF-8 boundaries")
    expected = (_line_for_offset(source_bytes, start), _line_for_offset(source_bytes, max(start, end - 1)))
    if (target.start_line, target.end_line) != expected:
        raise ValueError("target line numbers do not match its UTF-8 byte span")


def _validate_case_target_material(case: "CalibrationCase") -> None:
    if case.context_complete and not case.target_complete:
        raise ValueError("context_complete requires target_complete")
    source = case.evidence.source_documents.get(case.target.path)
    if source is None:
        raise ValueError("target.path must be present in complete evidence source_documents")
    _validate_target_span(case.target, source)
    if not (case.context_complete or case.target_complete):
        return
    evidence = Evidence(case.evidence.state, encode(case.evidence.state))
    if not evidence.contains(case.target.path, case.target.start_byte, case.target.end_byte):
        raise ValueError("target span is not covered by target-path evidence")


class ComparabilityMetadata(StrictModel):
    """Only identities that must agree for paired model-answer comparisons."""

    question: Sha256Hash
    evidence: Sha256Hash
    prompt: PromptCompatibility
    endpoint: StrictStr = Field(min_length=1)
    model: StrictStr = Field(min_length=1)


def _validate_capture_contract(case: "CalibrationCase") -> None:
    capture = case.capture
    if capture is None:
        return

    if capture.answer.model_dump(mode="json") != case.answer.model_dump(mode="json"):
        raise ValueError("capture.answer must match answer")
    if capture.evidence.model_dump(mode="json") != case.evidence.model_dump(mode="json"):
        raise ValueError("capture.evidence must match evidence")
    if case.question_wire != capture.question_wire:
        raise ValueError("capture.question_wire must match question_wire")
    if capture.returned_model != case.returned_model:
        raise ValueError("capture.returned_model must match returned_model")
    if (capture.context_complete, capture.target_complete) != (case.context_complete, case.target_complete):
        raise ValueError("capture completeness must match the case")

    check = Check(case.case_id, case.target, case.rule_id, case.rule)
    expected = assess(check, case.answer, case.context_complete)
    disposition = capture.disposition
    if disposition.status == "not_applicable" and disposition.reason == "model_routed_not_applicable":
        if not has_canonical_not_applicable_route(capture, check):
            raise ValueError("unsupported final not-applicable disposition")
        return
    if (disposition.status, disposition.reason) != (expected.status, expected.reason):
        raise ValueError("final disposition does not match production assessment")


class CalibrationCase(StrictModel):
    """One immutable, versioned answer and its adjudicated semantic label."""

    version: Literal[1]
    case_id: StrictStr = Field(min_length=1)
    rule_id: StrictStr = Field(min_length=1)
    rule: Rule
    target: Target
    answer: Answer
    context_complete: StrictBool
    target_complete: StrictBool
    question_wire: dict[str, Any] | None = None
    capture: FinalCaptureMaterial | None = None
    split: StrictStr = Field(min_length=1)
    label: CalibrationLabel
    adjudicated_severity: AdjudicatedSeverity | None = Field(
        default=None,
        validation_alias=AliasChoices("adjudicated_severity", "severity"),
    )
    explanation: StrictStr = Field(min_length=1)
    provenance: dict[str, Any] = Field(min_length=1)
    evidence: EvidenceMaterial
    prompt: PromptMaterial
    endpoint: StrictStr = Field(min_length=1)
    requested_model: StrictStr = Field(min_length=1)
    returned_model: StrictStr = Field(min_length=1)
    hashes: IdentityHashes
    comparability: ComparabilityMetadata

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_answer_fields(cls, value: Any) -> Any:
        """Reject extras before the provider-compatible answer model sees them."""
        if not isinstance(value, Mapping):
            return value
        answer = value.get("answer")
        if not isinstance(answer, Mapping):
            return value
        answer_type = answer.get("type")
        expected_fields = {
            "noul": {"type", "noul"},
            "choice": {"type", "choice", "confidence", "probabilities"},
            "score": {"type", "score", "confidence", "probabilities"},
        }.get(answer_type)
        if expected_fields is None:
            return value
        unknown = sorted(set(answer) - expected_fields)
        missing = sorted(expected_fields - set(answer))
        if unknown or missing:
            details = []
            if unknown:
                details.append(f"unknown={unknown}")
            if missing:
                details.append(f"missing={missing}")
            raise ValueError(f"answer fields do not match type {answer_type!r}: {', '.join(details)}")
        return value

    @model_validator(mode="before")
    @classmethod
    def decode_target(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and isinstance(value.get("target"), Mapping):
            document = dict(value)
            document["target"] = TargetRecord.model_validate(value["target"]).to_target()
            return document
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> "CalibrationCase":
        if self.label == "Disagree" and self.adjudicated_severity is not None:
            raise ValueError("Disagree cases must not carry adjudicated severity")
        _validate_capture_contract(self)
        _validate_case_target_material(self)
        self._validate_answer_and_identity()
        return self

    def _validate_answer_and_identity(self) -> None:
        try:
            validate_answer(self.answer, self.rule.question, self.rule_id)
        except JevError as exc:
            raise ValueError(str(exc)) from exc

        check = Check(self.case_id, self.target, self.rule_id, self.rule)
        binder = prompt_binder(self.prompt.version, self.prompt.policy)
        canonical_wire = binder.bind(self.rule.question, self.target, self.prompt.policy)
        if self.prompt.version == 6:
            validate_prompt_registry(self.evidence.state, self.rule.question)
        if self.question_wire is not None and self.question_wire != canonical_wire:
            raise ValueError("question_wire must match the canonical primary binding")
        computed_hashes = _computed_hashes(
            self,
            check,
            canonical_wire,
        )
        mismatches: list[str] = []
        declared_hashes = self.hashes.model_dump(mode="python")
        for name, computed in computed_hashes.items():
            if declared_hashes[name] != computed:
                mismatches.append(f"hashes.{name}")

        expected_comparability = {
            "question": computed_hashes["question"],
            "evidence": computed_hashes["evidence"],
            "prompt": {"version": self.prompt.version, "identity": computed_hashes["prompt"]},
            "endpoint": self.endpoint,
            "model": self.returned_model,
        }
        actual_comparability = self.comparability.model_dump(mode="python")
        for name, expected in expected_comparability.items():
            if actual_comparability[name] != expected:
                mismatches.append(f"comparability.{name}")

        if mismatches:
            raise ValueError("calibration identity mismatch: " + ", ".join(mismatches))

    @property
    def target_record(self) -> Target:
        return self.target

    @property
    def model(self) -> str:
        """Backward-readable name for the returned concrete model."""
        return self.returned_model

    @property
    def comparability_key(self) -> tuple[tuple[str, str], ...]:
        metadata = self.comparability.model_dump(mode="json")
        flattened = {
            "question": metadata["question"],
            "evidence": metadata["evidence"],
            "prompt.version": str(metadata["prompt"]["version"]),
            "prompt.identity": metadata["prompt"]["identity"],
            "endpoint": metadata["endpoint"],
            "model": metadata["model"],
        }
        return tuple(sorted(flattened.items()))



def _computed_hashes(
    case: CalibrationCase,
    check: Check,
    question_wire: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "question": _sha256_bytes(encode(check.question() if question_wire is None else question_wire)),
        "evidence": _sha256_bytes(encode(case.evidence.state)),
        "source_documents": {
            path: _sha256_text(content) for path, content in sorted(case.evidence.source_documents.items())
        },
        "rule": _sha256_bytes(encode(case.rule.model_dump(mode="json"))),
        "report": _sha256_bytes(encode(case.rule.report.model_dump(mode="json"))),
        "prompt": _sha256_bytes(encode(case.prompt.model_dump(mode="json"))),
        "endpoint": _sha256_text(case.endpoint),
        "requested_model": _sha256_text(case.requested_model),
        "returned_model": _sha256_text(case.returned_model),
    }


def _case_from_line(line: str, line_number: int) -> CalibrationCase:
    try:
        value = json.loads(line)
        return CalibrationCase.model_validate(value)
    except (json.JSONDecodeError, JevError, ValidationError, TypeError, ValueError) as exc:
        raise ValueError(f"calibration JSONL line {line_number} is invalid: {exc}") from exc


def load_cases(source: Path | str | TextIO) -> list[CalibrationCase]:
    """Load strict one-case-per-line JSONL without contacting a provider."""
    if isinstance(source, (Path, str)):
        path = Path(source)
        with path.open(encoding="utf-8") as stream:
            lines = stream.readlines()
    else:
        lines = source.readlines()
    cases: list[CalibrationCase] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ValueError(f"calibration JSONL line {line_number} is empty")
        cases.append(_case_from_line(line, line_number))
    if not cases:
        raise ValueError("calibration dataset is empty")
    return cases


