"""Strict offline semantic-calibration cases and production-path replay.

Calibration data is an auditable record of a model judgment, not a second
scoring implementation. Cases carry the canonical material required to
recompute their identities before the stored answer is sent through the
normal :func:`jevscan.core.assessment.assess` path.
"""

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Iterable, Literal, Mapping, TextIO, cast

from pydantic import (
    AliasChoices,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from jevscan.core.assessment import Assessment, assess
from jevscan.core.config import EnrichmentConfig
from jevscan.core.context import Evidence
from jevscan.core.enrichment import EVIDENCE_FAMILIES, routing_decision, routing_questions
from jevscan.core.models import Kind, Target
from jevscan.core.protocol import (
    Answer,
    Check,
    JevError,
    encode,
    prompt_binder,
    validate_answer,
    validate_prompt_registry,
)
from jevscan.core.rules import ReportPolicy, Rule, StrictModel

CALIBRATION_VERSION = 1
CalibrationLabel = Literal["Agree", "Partial", "Disagree"]
AdjudicatedSeverity = Literal["warning", "error"]
Sha256Hash = Annotated[StrictStr, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
_ANSWER_ADAPTER = TypeAdapter(Answer)


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


def _has_canonical_not_applicable_route(capture: FinalCaptureMaterial, check: Check) -> bool:
    config = capture.review.get("routing_config")
    if not isinstance(config, Mapping):
        raise TypeError("final not-applicable capture is missing routing configuration")
    raw_families = config.get("allowed_families")
    if not isinstance(raw_families, list) or any(not isinstance(item, str) for item in raw_families):
        raise ValueError("final not-applicable capture has invalid routing families")
    families = tuple(raw_families)
    if not families or any(family not in EVIDENCE_FAMILIES for family in families):
        raise ValueError("final not-applicable capture has invalid routing families")
    limits = EnrichmentConfig.model_validate({
        "enabled": True,
        "mode": "targeted",
        "min_route_confidence": config.get("min_route_confidence"),
        "min_route_probability": config.get("min_route_probability"),
        "min_evidence_probability": config.get("min_evidence_probability"),
    })
    questions = routing_questions(check, families)
    answers: dict[str, Answer] = {}
    for prediction in capture.review.get("predictions", []):
        if not isinstance(prediction, Mapping) or prediction.get("phase") != "route":
            continue
        wires = prediction.get("question_wires")
        raw_answers = prediction.get("answers")
        if not isinstance(wires, Mapping) or not isinstance(raw_answers, Mapping):
            raise TypeError("final not-applicable capture has incomplete route prediction")
        for name, raw_wire in wires.items():
            if name not in questions or raw_wire != check.auxiliary(questions[name]):
                raise ValueError("final not-applicable capture has a non-canonical route wire")
            if name not in raw_answers:
                raise ValueError("final not-applicable capture route answer is missing")
            answer = _ANSWER_ADAPTER.validate_python(raw_answers[name])
            validate_answer(answer, questions[name], name)
            answers[name] = answer
    if set(answers) != set(questions):
        raise ValueError("final not-applicable capture does not contain the complete route decision")
    decision = routing_decision(
        answers,
        families,
        min_route_confidence=limits.min_route_confidence,
        min_route_probability=limits.min_route_probability,
        min_evidence_probability=limits.min_evidence_probability,
    )
    return decision.disposition == "not_applicable"


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
        if self.capture is not None:
            if self.capture.answer.model_dump(mode="json") != self.answer.model_dump(mode="json"):
                raise ValueError("capture.answer must match answer")
            if self.capture.evidence.model_dump(mode="json") != self.evidence.model_dump(mode="json"):
                raise ValueError("capture.evidence must match evidence")
            if self.question_wire != self.capture.question_wire:
                raise ValueError("capture.question_wire must match question_wire")
            if self.capture.returned_model != self.returned_model:
                raise ValueError("capture.returned_model must match returned_model")
            if (self.capture.context_complete, self.capture.target_complete) != (
                self.context_complete,
                self.target_complete,
            ):
                raise ValueError("capture completeness must match the case")
            check = Check(self.case_id, self.target, self.rule_id, self.rule)
            expected = assess(check, self.answer, self.context_complete)
            disposition = self.capture.disposition
            if disposition.status == "not_applicable":
                if disposition.reason == "model_routed_not_applicable":
                    if not _has_canonical_not_applicable_route(self.capture, check):
                        raise ValueError("unsupported final not-applicable disposition")
                elif (disposition.status, disposition.reason) != (expected.status, expected.reason):
                    raise ValueError("final disposition does not match production assessment")
            elif (disposition.status, disposition.reason) != (expected.status, expected.reason):
                raise ValueError("final disposition does not match production assessment")
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


@dataclass(frozen=True, slots=True)
class Fraction:
    count: int
    denominator: int

    @property
    def value(self) -> float | None:
        return self.count / self.denominator if self.denominator else None

    def as_dict(self) -> dict[str, int | float | None]:
        return {"count": self.count, "denominator": self.denominator, "fraction": self.value}


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    record_index: int
    case: CalibrationCase
    assessment: Assessment
    effective_rule: Rule
    effective_hashes: IdentityHashes
    comparable: bool = True
    mismatches: tuple[dict[str, Any], ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.assessment.status in {"warning", "error"}

    @property
    def signal(self) -> bool:
        return self.confirmed or self.assessment.tentative_finding is not None

    @property
    def reason(self) -> str:
        return ", ".join(str(item["field"]) for item in self.mismatches)


@dataclass(frozen=True, slots=True)
class RuleSplitReport:
    rule_id: str
    split: str
    total: int
    label_counts: dict[str, int]
    strict_supported_warning: Fraction
    confirmed_recall: Fraction
    review_list_recall: Fraction
    disagree_confirmed: Fraction
    partial_confirmed: Fraction
    partial_any_signal: Fraction
    confirmed_severity_agreement: Fraction
    review_list_severity_agreement: Fraction
    severity_confusion: dict[str, dict[str, int]]
    severity_counts: dict[str, int]
    tentative_severity_counts: dict[str, int]
    non_comparable: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "split": self.split,
            "total": self.total,
            "label_counts": self.label_counts,
            "strict_supported_warning": self.strict_supported_warning.as_dict(),
            "confirmed_recall": self.confirmed_recall.as_dict(),
            "review_list_recall": self.review_list_recall.as_dict(),
            "disagree_confirmed": self.disagree_confirmed.as_dict(),
            "partial_confirmed": self.partial_confirmed.as_dict(),
            "partial_any_signal": self.partial_any_signal.as_dict(),
            "confirmed_severity_agreement": self.confirmed_severity_agreement.as_dict(),
            "review_list_severity_agreement": self.review_list_severity_agreement.as_dict(),
            "severity_confusion": self.severity_confusion,
            "severity_counts": self.severity_counts,
            "tentative_severity_counts": self.tentative_severity_counts,
            "non_comparable": self.non_comparable,
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    records: tuple[ReplayRecord, ...]
    reports: dict[str, dict[str, RuleSplitReport]]
    non_comparable: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        ordered_records = sorted(
            self.records,
            key=lambda record: (record.case.case_id, record.case.rule_id, record.case.split, record.record_index),
        )
        return {
            "version": CALIBRATION_VERSION,
            "cases": len(self.records),
            "records": [_record_as_dict(record) for record in ordered_records],
            "non_comparable": list(self.non_comparable),
            "rules": {
                rule_id: {split: report.as_dict() for split, report in splits.items()}
                for rule_id, splits in sorted(self.reports.items())
            },
        }


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


def _report_override(rule: Rule, override: ReportPolicy | Mapping[str, Any] | None) -> Rule:
    if override is None:
        return rule
    policy = override if isinstance(override, ReportPolicy) else ReportPolicy.model_validate(override)
    return Rule.model_validate({**rule.model_dump(mode="python"), "report": policy.model_dump(mode="python")})


def _effective_hashes(case: CalibrationCase, rule: Rule) -> IdentityHashes:
    values = case.hashes.model_dump(mode="python")
    values["rule"] = _sha256_bytes(encode(rule.model_dump(mode="json")))
    values["report"] = _sha256_bytes(encode(rule.report.model_dump(mode="json")))
    return IdentityHashes.model_validate(values)


def replay_case(
    case: CalibrationCase,
    report_override: ReportPolicy | Mapping[str, Any] | None = None,
    *,
    record_index: int = 0,
) -> ReplayRecord:
    """Replay one case through the production Check and assess paths."""
    rule = _report_override(case.rule, report_override)
    check = Check(case.case_id, case.target, case.rule_id, rule)
    assessment = assess(check, case.answer, case.context_complete)
    if (
        case.capture is not None
        and case.capture.disposition.status == "not_applicable"
        and case.capture.disposition.reason == "model_routed_not_applicable"
    ):
        assessment = Assessment("not_applicable", case.capture.disposition.reason)
    return ReplayRecord(
        record_index,
        case,
        assessment,
        rule,
        _effective_hashes(case, rule),
    )


def _comparability_value(record: ReplayRecord, field: str) -> Any:
    return record.case.comparability.model_dump(mode="json")[field]


def _value_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mark_non_comparable(
    records: list[ReplayRecord],
) -> tuple[list[ReplayRecord], tuple[dict[str, Any], ...]]:
    by_case: dict[str, list[ReplayRecord]] = defaultdict(list)
    for record in records:
        by_case[record.case.case_id].append(record)

    notices: list[dict[str, Any]] = []
    marked = list(records)
    fields = ("question", "evidence", "prompt", "endpoint", "model")
    for case_id, group in sorted(by_case.items()):
        conflicts: list[dict[str, Any]] = []
        for field in fields:
            values = [_comparability_value(record, field) for record in group]
            if len({_value_key(value) for value in values}) <= 1:
                continue
            conflicts.append({
                "field": field,
                "records": [
                    {
                        "record_index": record.record_index,
                        "case_id": record.case.case_id,
                        "rule_id": record.case.rule_id,
                        "split": record.case.split,
                        "value": _comparability_value(record, field),
                    }
                    for record in group
                ],
            })
        if not conflicts:
            continue
        notices.append({"case_id": case_id, "fields": [item["field"] for item in conflicts], "conflicts": conflicts})
        conflict_tuple = tuple(conflicts)
        marked = [
            ReplayRecord(
                record.record_index,
                record.case,
                record.assessment,
                record.effective_rule,
                record.effective_hashes,
                record.comparable and record.case.case_id != case_id,
                conflict_tuple if record.case.case_id == case_id else record.mismatches,
            )
            for record in marked
        ]
    return marked, tuple(notices)


def _count(records: Iterable[ReplayRecord], predicate: Callable[[ReplayRecord], bool]) -> int:
    return sum(predicate(record) for record in records)


def _fraction(
    records: Iterable[ReplayRecord],
    numerator: Callable[[ReplayRecord], bool],
    denominator: Callable[[ReplayRecord], bool],
) -> Fraction:
    values = list(records)
    return Fraction(_count(values, numerator), _count(values, denominator))


def _label_count(records: list[ReplayRecord], label: str) -> int:
    return _count(records, lambda record: record.case.label == label)


def _status_count(records: list[ReplayRecord], status: str) -> int:
    return _count(records, lambda record: record.assessment.status == status)


def _tentative_count(records: list[ReplayRecord], severity: str) -> int:
    return _count(
        records,
        lambda record: (
            record.assessment.tentative_finding is not None
            and str(record.assessment.tentative_finding.severity) == severity
        ),
    )


SEVERITY_OUTCOMES = (
    "none",
    "confirmed_warning",
    "confirmed_error",
    "tentative_warning",
    "tentative_error",
)


def _observed_severity(record: ReplayRecord, *, include_tentative: bool) -> str | None:
    if record.assessment.finding is not None:
        severity = str(record.assessment.finding.severity)
        return severity if severity in {"warning", "error"} else None
    if include_tentative and record.assessment.tentative_finding is not None:
        severity = str(record.assessment.tentative_finding.severity)
        return severity if severity in {"warning", "error"} else None
    return None


def _severity_agreement(record: ReplayRecord, *, include_tentative: bool) -> bool:
    expected = record.case.adjudicated_severity
    return expected is not None and _observed_severity(record, include_tentative=include_tentative) == expected


def _severity_outcome(record: ReplayRecord) -> str:
    if record.assessment.finding is not None:
        severity = str(record.assessment.finding.severity)
        if severity in {"warning", "error"}:
            return f"confirmed_{severity}"
    if record.assessment.tentative_finding is not None:
        severity = str(record.assessment.tentative_finding.severity)
        if severity in {"warning", "error"}:
            return f"tentative_{severity}"
    return "none"


def _severity_confusion(records: list[ReplayRecord]) -> dict[str, dict[str, int]]:
    return {
        adjudicated: {
            outcome: _count(
                records,
                lambda record, adjudicated=adjudicated, outcome=outcome: (
                    record.case.adjudicated_severity == adjudicated and _severity_outcome(record) == outcome
                ),
            )
            for outcome in SEVERITY_OUTCOMES
        }
        for adjudicated in ("warning", "error")
    }


def _make_report(rule_id: str, split: str, records: list[ReplayRecord]) -> RuleSplitReport:
    usable = [record for record in records if record.comparable]
    has_adjudicated_severity = lambda record: record.case.adjudicated_severity is not None
    return RuleSplitReport(
        rule_id=rule_id,
        split=split,
        total=len(usable),
        label_counts={label: _label_count(usable, label) for label in ("Agree", "Partial", "Disagree")},
        strict_supported_warning=_fraction(
            usable, lambda record: record.case.label == "Agree" and record.confirmed, lambda record: record.confirmed
        ),
        confirmed_recall=_fraction(
            usable,
            lambda record: record.case.label == "Agree" and record.confirmed,
            lambda record: record.case.label == "Agree",
        ),
        review_list_recall=_fraction(
            usable,
            lambda record: record.case.label == "Agree" and record.signal,
            lambda record: record.case.label == "Agree",
        ),
        disagree_confirmed=_fraction(
            usable,
            lambda record: record.case.label == "Disagree" and record.confirmed,
            lambda record: record.case.label == "Disagree",
        ),
        partial_confirmed=_fraction(
            usable,
            lambda record: record.case.label == "Partial" and record.confirmed,
            lambda record: record.case.label == "Partial",
        ),
        partial_any_signal=_fraction(
            usable,
            lambda record: record.case.label == "Partial" and record.signal,
            lambda record: record.case.label == "Partial",
        ),
        confirmed_severity_agreement=_fraction(
            usable,
            lambda record: _severity_agreement(record, include_tentative=False),
            has_adjudicated_severity,
        ),
        review_list_severity_agreement=_fraction(
            usable,
            lambda record: _severity_agreement(record, include_tentative=True),
            has_adjudicated_severity,
        ),
        severity_confusion=_severity_confusion(usable),
        severity_counts={
            status: _status_count(usable, status) for status in ("ok", "warning", "error", "unknown", "not_applicable")
        },
        tentative_severity_counts={severity: _tentative_count(usable, severity) for severity in ("warning", "error")},
        non_comparable=len(records) - len(usable),
    )


def replay_cases(
    cases: Iterable[CalibrationCase],
    report_overrides: Mapping[str, ReportPolicy | Mapping[str, Any]] | None = None,
) -> CalibrationReport:
    """Replay cases and aggregate exact count/denominator metrics by rule and split."""
    records = [
        replay_case(
            case,
            report_overrides.get(case.rule_id) if report_overrides else None,
            record_index=index,
        )
        for index, case in enumerate(cases)
    ]
    marked, notices = _mark_non_comparable(records)
    grouped: dict[tuple[str, str], list[ReplayRecord]] = defaultdict(list)
    for record in marked:
        grouped[(record.case.rule_id, record.case.split)].append(record)
    reports: dict[str, dict[str, RuleSplitReport]] = defaultdict(dict)
    for (rule_id, split), group in sorted(grouped.items()):
        reports[rule_id][split] = _make_report(rule_id, split, group)
    return CalibrationReport(tuple(marked), dict(reports), notices)


def _target_as_dict(target: Target) -> dict[str, Any]:
    return {
        "id": target.id,
        "scope": target.scope,
        "path": target.path,
        "language": target.language,
        "qualified_name": target.qualified_name,
        "start_byte": target.start_byte,
        "end_byte": target.end_byte,
        "start_line": target.start_line,
        "end_line": target.end_line,
        "kind": str(target.kind) if target.kind is not None else None,
        "display_name": target.display_name,
    }


def _finding_as_dict(finding: Any) -> dict[str, Any] | None:
    if finding is None:
        return None
    return {
        "rule": finding.rule,
        "severity": str(finding.severity),
        "message": finding.message,
        "target": _target_as_dict(finding.target),
        "value": finding.value,
        "probability": finding.probability,
        "confidence": finding.confidence,
    }


def _record_as_dict(record: ReplayRecord) -> dict[str, Any]:
    case = record.case
    finding = _finding_as_dict(record.assessment.finding)
    tentative = _finding_as_dict(record.assessment.tentative_finding)
    outcome = {
        "status": record.assessment.status,
        "reason": record.assessment.reason,
        "finding": finding,
        "tentative_finding": tentative,
    }
    return {
        "record_index": record.record_index,
        "case_id": case.case_id,
        "rule_id": case.rule_id,
        "split": case.split,
        "label": case.label,
        "adjudicated_severity": case.adjudicated_severity,
        "explanation": case.explanation,
        "provenance": case.provenance,
        "rule": case.rule.model_dump(mode="json"),
        "effective_rule": record.effective_rule.model_dump(mode="json"),
        "target": _target_as_dict(case.target),
        "answer": case.answer.model_dump(mode="json"),
        "context_complete": case.context_complete,
        "target_complete": case.target_complete,
        "evidence": case.evidence.model_dump(mode="json"),
        "prompt": case.prompt.model_dump(mode="json"),
        "endpoint": case.endpoint,
        "requested_model": case.requested_model,
        "returned_model": case.returned_model,
        "hashes": case.hashes.model_dump(mode="json"),
        "comparability": case.comparability.model_dump(mode="json"),
        "effective_hashes": record.effective_hashes.model_dump(mode="json"),
        "comparable": record.comparable,
        "comparability_mismatches": list(record.mismatches),
        "status": record.assessment.status,
        "reason": record.assessment.reason,
        "finding": finding,
        "tentative_finding": tentative,
        "outcome": outcome,
    }


def write_report(report: CalibrationReport, destination: TextIO) -> None:
    json.dump(report.as_dict(), destination, ensure_ascii=False, sort_keys=True, indent=2)
    destination.write("\n")
