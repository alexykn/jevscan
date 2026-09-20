import hashlib
import json
from bisect import bisect_left
from io import StringIO
from typing import Any

import pytest
from pydantic import ValidationError

from jevscan.core.assessment import Assessment, assess
from jevscan.core.config import EnrichmentConfig
from jevscan.core.enrichment import DISPOSITIONS, allowed_families, routing_questions
from jevscan.core.models import Kind, Target
from jevscan.core.protocol import (
    PROMPT_VERSION,
    QUESTION_POLICY,
    QUESTION_POLICY_V4,
    QUESTION_POLICY_V4_EARLY,
    Check,
    ScoreAnswer,
    bind_question,
    encode,
    prompt_binder,
)
from jevscan.core.rules import Rule
from jevscan.core.semantic_calibration import CalibrationCase, load_cases, replay_case, replay_cases


def score_rule(
    warning: dict[str, Any],
    error: dict[str, Any],
    *,
    message: str = "Review",
) -> Rule:
    return Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "score", "instructions": "How tangled?", "criteria": ["0", "1", "2", "3"]},
        "report": {"message": message, "levels": {"warning": warning, "error": error}},
    })


def score_check(rule: Rule) -> Check:
    target = Target("target", "unit", "sample.py", "python", "sample", 0, 10, 1, 1, Kind.FUNCTION)
    return Check("target", target, "renamed-rule", rule)


def score_answer(score: float, confidence: float, probabilities: dict[str, float]) -> ScoreAnswer:
    return ScoreAnswer(type="score", score=score, confidence=confidence, probabilities=probabilities)


def test_score_mass_sums_selected_raw_levels_and_keeps_expected_score() -> None:
    rule = score_rule({"score_levels": [2, 3], "min_probability": 0.5}, {"score_levels": [3], "min_probability": 0.8})
    check = score_check(rule)
    no_signal = score_answer(2, 0.9, {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0})
    supported = score_answer(2, 0.9, {"0": 0.0, "1": 0.0, "2": 0.3, "3": 0.3})
    changed_mass = score_answer(2, 0.9, {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.1})

    assert assess(check, no_signal).finding is None
    finding = assess(check, supported).finding
    assert finding is not None
    assert finding.value == 2
    assert finding.probability == pytest.approx(0.6)
    assert assess(check, changed_mass).status == "ok"


def test_score_mass_distinguishes_two_of_three_from_one_of_two() -> None:
    rule = score_rule({"score_levels": [2, 3], "min_probability": 0.6}, {"score_levels": [3], "min_probability": 0.9})
    check = score_check(rule)
    two_of_three = score_answer(2, 0.9, {"0": 0.0, "1": 0.0, "2": 0.3, "3": 0.3})
    one_of_two = score_answer(2, 0.9, {"0": 0.3, "1": 0.3, "2": 0.0, "3": 0.0})

    assert assess(check, two_of_three).status == "warning"
    assert assess(check, one_of_two).status == "ok"


def test_score_mass_preserves_severity_first_and_tentative_behavior() -> None:
    rule = score_rule(
        {"score_levels": [2, 3], "min_probability": 0.5, "min_confidence": 0.7},
        {"score_levels": [3], "min_probability": 0.8, "min_confidence": 0.8},
    )
    check = score_check(rule)
    warning = assess(check, score_answer(2, 0.4, {"0": 0.0, "1": 0.0, "2": 0.3, "3": 0.3}))
    unsupported = assess(check, score_answer(1, 0.99, {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0}))

    assert warning.status == "unknown"
    assert warning.finding is None
    assert warning.tentative_finding is not None
    assert warning.tentative_finding.severity == "warning"
    assert unsupported.status == "ok"
    assert unsupported.finding is None


@pytest.mark.parametrize(
    "warning,error",
    [
        ({"score_levels": [2, 3], "min_probability": 0.5}, {"min_score": 3}),
        ({"score_levels": [], "min_probability": 0.5}, {"score_levels": [2], "min_probability": 0.8}),
        ({"score_levels": [2, 2], "min_probability": 0.5}, {"score_levels": [2], "min_probability": 0.8}),
        ({"score_levels": [3, 2], "min_probability": 0.5}, {"score_levels": [2], "min_probability": 0.8}),
        ({"score_levels": [2], "min_probability": 0.5}, {"score_levels": [3], "min_probability": 0.8}),
        ({"score_levels": [2], "min_probability": 0.5, "min_score": 2}, {"score_levels": [2], "min_probability": 0.8}),
    ],
)
def test_invalid_score_mass_contracts_are_rejected(warning: dict[str, Any], error: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        score_rule(warning, error)


def test_choice_and_noul_reject_score_levels() -> None:
    for question in (
        {"type": "choice", "instructions": "Which?", "criteria": {"bad": "Bad", "good": "Good"}},
        {"type": "noul", "instructions": "Is it bad?"},
    ):
        with pytest.raises(ValidationError):
            Rule.model_validate({
                "applies_to": ["function"],
                "question": question,
                "report": {
                    "message": "Review",
                    "choices": ["bad"] if question["type"] == "choice" else None,
                    "levels": {
                        "warning": {"score_levels": [1], "min_probability": 0.5},
                        "error": {"score_levels": [1], "min_probability": 0.8},
                    },
                },
            })


def _hash_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _hash_text(value: str) -> str:
    return _hash_bytes(value.encode("utf-8"))


def _span_lines(source: str, start: int, end: int) -> tuple[int, int]:
    newlines = [index for index, byte in enumerate(source.encode("utf-8")) if byte == 10]
    return bisect_left(newlines, start) + 1, bisect_left(newlines, max(start, end - 1)) + 1


def _target_document(
    *,
    start_byte: int,
    end_byte: int,
    start_line: int,
    end_line: int,
    path: str = "sample.py",
) -> dict[str, Any]:
    return {
        "id": "target",
        "scope": "unit",
        "path": path,
        "language": "python",
        "qualified_name": "sample",
        "start_byte": start_byte,
        "end_byte": end_byte,
        "start_line": start_line,
        "end_line": end_line,
        "kind": "function",
    }


def case_document(
    case_id: str = "case-1",
    *,
    source: str = "abcdefghij",
    evidence_content: str | None = None,
    evidence_start: int = 0,
    evidence_end: int | None = None,
    target_start: int = 0,
    target_end: int | None = None,
    warning: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    label: str = "Agree",
    endpoint: str = "https://example.test",
    requested_model: str = "requested-model",
    returned_model: str = "concrete-model",
    prompt_version: int = PROMPT_VERSION,
    prompt_policy: str = QUESTION_POLICY,
    adjudicated_severity: str | None = None,
    context_complete: bool = True,
    target_complete: bool = True,
) -> dict[str, Any]:
    source_bytes = source.encode("utf-8")
    evidence_end = len(source_bytes) if evidence_end is None else evidence_end
    target_end = len(source_bytes) if target_end is None else target_end
    evidence_lines = _span_lines(source, evidence_start, evidence_end)
    target_lines = _span_lines(source, target_start, target_end)
    if evidence_content is None:
        evidence_content = source_bytes[evidence_start:evidence_end].decode("utf-8")
    rule = score_rule(warning or {"min_score": 2}, error or {"min_score": 3})
    target = Target(
        "target",
        "unit",
        "sample.py",
        "python",
        "sample",
        target_start,
        target_end,
        target_lines[0],
        target_lines[1],
        Kind.FUNCTION,
    )
    state = {
        "documents": [
            {
                "path": "sample.py",
                "language": "python",
                "start_byte": evidence_start,
                "end_byte": evidence_end,
                "start_line": evidence_lines[0],
                "end_line": evidence_lines[1],
                "content": evidence_content,
            }
        ],
        "coverage": {"file_complete": False},
    }
    prompt = {"version": prompt_version, "policy": prompt_policy}
    check = Check(case_id, target, "renamed-rule", rule)
    try:
        question = prompt_binder(prompt_version, prompt_policy).bind(rule.question, target, prompt_policy)
    except ValueError:
        question = check.question()
    hashes = {
        "question": _hash_bytes(encode(question)),
        "evidence": _hash_bytes(encode(state)),
        "source_documents": {"sample.py": _hash_text(source)},
        "rule": _hash_bytes(encode(rule.model_dump(mode="json"))),
        "report": _hash_bytes(encode(rule.report.model_dump(mode="json"))),
        "prompt": _hash_bytes(encode(prompt)),
        "endpoint": _hash_text(endpoint),
        "requested_model": _hash_text(requested_model),
        "returned_model": _hash_text(returned_model),
    }
    document: dict[str, Any] = {
        "version": 1,
        "case_id": case_id,
        "rule_id": "renamed-rule",
        "rule": rule.model_dump(mode="json"),
        "target": _target_document(
            start_byte=target_start,
            end_byte=target_end,
            start_line=target_lines[0],
            end_line=target_lines[1],
        ),
        "answer": {
            "type": "score",
            "score": 2.0,
            "confidence": 0.9,
            "probabilities": {"0": 0.0, "1": 0.0, "2": 0.3, "3": 0.3},
        },
        "context_complete": context_complete,
        "target_complete": target_complete,
        "split": "replay",
        "label": label,
        "explanation": "The target obscures a transition.",
        "provenance": {"source": "offline-review", "annotator": "team"},
        "evidence": {"state": state, "source_documents": {"sample.py": source}},
        "prompt": prompt,
        "endpoint": endpoint,
        "requested_model": requested_model,
        "returned_model": returned_model,
        "hashes": hashes,
        "comparability": {
            "question": hashes["question"],
            "evidence": hashes["evidence"],
            "prompt": {"version": prompt_version, "identity": hashes["prompt"]},
            "endpoint": endpoint,
            "model": returned_model,
        },
    }
    if adjudicated_severity is not None:
        document["adjudicated_severity"] = adjudicated_severity
    return document


def _noul_case_with_capture(disposition: dict[str, str]) -> dict[str, Any]:
    document = case_document()
    rule = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "noul", "instructions": "Is this operation defective?"},
        "report": {
            "message": "Review this operation.",
            "levels": {
                "warning": {"min_probability": 0.85},
                "error": {"min_probability": 0.99},
            },
        },
    })
    target = Target("target", "unit", "sample.py", "python", "sample", 0, 10, 1, 1, Kind.FUNCTION)
    answer = {"type": "noul", "noul": 0.99}
    question = prompt_binder(document["prompt"]["version"], document["prompt"]["policy"]).bind(
        rule.question, target, document["prompt"]["policy"]
    )
    document.update({
        "rule": rule.model_dump(mode="json"),
        "answer": answer,
        "question_wire": question,
    })
    document["hashes"].update({
        "question": _hash_bytes(encode(question)),
        "rule": _hash_bytes(encode(document["rule"])),
        "report": _hash_bytes(encode(rule.report.model_dump(mode="json"))),
    })
    document["comparability"]["question"] = document["hashes"]["question"]
    document["capture"] = {
        "phase": "final",
        "question_wire": question,
        "evidence": document["evidence"],
        "answer": answer,
        "disposition": disposition,
        "context_complete": document["context_complete"],
        "target_complete": document["target_complete"],
        "returned_model": document["returned_model"],
        "assessment": {"status": disposition["status"], "reason": disposition["reason"]},
    }
    return document


def _ordinary_not_applicable_choice_case() -> CalibrationCase:
    document = case_document()
    rule = Rule.model_validate({
        "applies_to": ["function"],
        "question": {
            "type": "choice",
            "instructions": "Select the applicable outcome.",
            "criteria": {
                "defect": "A defect is present.",
                "clean": "No defect is present.",
                "not_applicable": "The rule does not apply.",
            },
        },
        "report": {
            "message": "Review this operation.",
            "choices": ["defect"],
            "not_applicable_choices": ["not_applicable"],
            "levels": {
                "warning": {"min_probability": 0.5},
                "error": {"min_probability": 0.95},
            },
        },
    })
    target = Target("target", "unit", "sample.py", "python", "sample", 0, 10, 1, 1, Kind.FUNCTION)
    answer = {
        "type": "choice",
        "choice": "not_applicable",
        "confidence": 1.0,
        "probabilities": {"defect": 0.2, "clean": 0.1, "not_applicable": 0.7},
    }
    question = prompt_binder(document["prompt"]["version"], document["prompt"]["policy"]).bind(
        rule.question, target, document["prompt"]["policy"]
    )
    document.update({"rule": rule.model_dump(mode="json"), "answer": answer, "question_wire": question})
    document["hashes"].update({
        "question": _hash_bytes(encode(question)),
        "rule": _hash_bytes(encode(document["rule"])),
        "report": _hash_bytes(encode(rule.report.model_dump(mode="json"))),
    })
    document["comparability"]["question"] = document["hashes"]["question"]
    document["capture"] = {
        "phase": "final",
        "question_wire": question,
        "evidence": document["evidence"],
        "answer": answer,
        "disposition": {"status": "not_applicable", "reason": "rule_not_applicable"},
        "context_complete": document["context_complete"],
        "target_complete": document["target_complete"],
        "returned_model": document["returned_model"],
        "assessment": {"status": "not_applicable", "reason": "rule_not_applicable"},
    }
    return CalibrationCase.model_validate(document)


def _set_evidence_spans(document: dict[str, Any], source: str, spans: list[tuple[int, int]]) -> None:
    documents = []
    for start, end in spans:
        start_line, end_line = _span_lines(source, start, end)
        documents.append({
            "path": "sample.py",
            "language": "python",
            "start_byte": start,
            "end_byte": end,
            "start_line": start_line,
            "end_line": end_line,
            "content": source.encode()[start:end].decode(),
        })
    document["evidence"]["state"]["documents"] = documents
    document["hashes"]["evidence"] = _hash_bytes(encode(document["evidence"]["state"]))
    document["comparability"]["evidence"] = document["hashes"]["evidence"]


def test_jsonl_case_contract_preserves_canonical_material_and_replays_without_a_client() -> None:
    document = case_document(adjudicated_severity="warning")
    cases = load_cases(StringIO(json.dumps(document) + "\n"))
    case = cases[0]
    assert case.rule_id == "renamed-rule"
    assert case.split == "replay"
    assert case.provenance["annotator"] == "team"
    assert case.evidence.source_documents["sample.py"] == "abcdefghij"
    assert case.adjudicated_severity == "warning"
    report = replay_cases(cases).as_dict()
    assert report["rules"]["renamed-rule"]["replay"]["confirmed_recall"] == {
        "count": 1,
        "denominator": 1,
        "fraction": 1.0,
    }


def test_rule_not_applicable_capture_must_match_production_noul_assessment() -> None:
    document = _noul_case_with_capture({"status": "not_applicable", "reason": "rule_not_applicable"})

    with pytest.raises(ValueError, match="final disposition"):
        CalibrationCase.model_validate(document)


def test_ordinary_not_applicable_capture_uses_recomputed_policy_assessment() -> None:
    case = _ordinary_not_applicable_choice_case()
    assert replay_case(case).assessment.status == "not_applicable"
    override = case.rule.report.model_dump(mode="python")
    override["levels"]["warning"]["min_probability"] = 0.8

    replayed = replay_case(case, override)

    assert replayed.assessment.status == "unknown"
    assert replayed.assessment.reason == "low_choice_probability"


def test_model_routed_not_applicable_capture_uses_canonical_production_route() -> None:
    document = _noul_case_with_capture({
        "status": "not_applicable",
        "reason": "model_routed_not_applicable",
    })
    target = Target("target", "unit", "sample.py", "python", "sample", 0, 10, 1, 1, Kind.FUNCTION)
    rule = Rule.model_validate(document["rule"])
    check = Check(document["case_id"], target, "renamed-rule", rule)
    limits = EnrichmentConfig()
    families = allowed_families(check, limits)
    questions = routing_questions(check, families)
    probabilities = dict.fromkeys(DISPOSITIONS, 0.0)
    probabilities["not_applicable"] = 1.0
    answers = {
        "disposition": {
            "type": "choice",
            "choice": "not_applicable",
            "confidence": 1.0,
            "probabilities": probabilities,
        },
        **{name: {"type": "noul", "noul": 0.1} for name in families},
    }
    document["capture"]["review"] = {
        "routing_config": {
            "allowed_families": list(families),
            "min_route_confidence": limits.min_route_confidence,
            "min_route_probability": limits.min_route_probability,
            "min_evidence_probability": limits.min_evidence_probability,
        },
        "predictions": [
            {
                "phase": "route",
                "question_wires": {name: check.auxiliary(question) for name, question in questions.items()},
                "answers": answers,
            }
        ],
    }

    case = CalibrationCase.model_validate(document)
    assert case.capture is not None
    assert case.capture.disposition.reason == "model_routed_not_applicable"
    assert replay_case(case).assessment == Assessment("not_applicable", "model_routed_not_applicable")


def test_fabricated_hashes_and_bare_hash_format_fail() -> None:
    document = case_document()
    document["hashes"]["question"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="hashes.question"):
        CalibrationCase.model_validate(document)

    document = case_document()
    document["hashes"]["source_documents"]["sample.py"] = "source-hash"
    with pytest.raises(ValidationError, match="source_documents"):
        CalibrationCase.model_validate(document)


@pytest.mark.parametrize(
    "answer_mutation",
    [
        lambda answer: answer.update({"future_field": "must fail"}),
        lambda answer: answer.pop("confidence"),
    ],
)
def test_jsonl_rejects_unknown_or_missing_answer_fields_before_provider_validation(answer_mutation: Any) -> None:
    document = case_document()
    answer_mutation(document["answer"])
    with pytest.raises(ValueError, match="answer fields"):
        load_cases(StringIO(json.dumps(document)))


def test_schema_version_prompt_and_source_material_are_strict() -> None:
    wrong_version = case_document()
    wrong_version["version"] = 2
    with pytest.raises(ValueError):
        load_cases(StringIO(json.dumps(wrong_version)))

    unknown_case_field = case_document()
    unknown_case_field["unreviewed"] = True
    with pytest.raises(ValueError, match="extra"):
        load_cases(StringIO(json.dumps(unknown_case_field)))

    bad_prompt = case_document(prompt_version=PROMPT_VERSION + 1)
    with pytest.raises(ValueError, match="prompt.version"):
        load_cases(StringIO(json.dumps(bad_prompt)))

    missing_source = case_document()
    missing_source["evidence"]["source_documents"] = {}
    with pytest.raises(ValueError, match="source"):
        load_cases(StringIO(json.dumps(missing_source)))


def test_reporting_policy_hash_changes_do_not_poison_comparability() -> None:
    original = CalibrationCase.model_validate(case_document())
    changed_report = CalibrationCase.model_validate(case_document(warning={"min_score": 1}, error={"min_score": 3}))
    assert original.hashes.report != changed_report.hashes.report
    report = replay_cases([original, changed_report])
    assert not report.non_comparable
    assert report.reports["renamed-rule"]["replay"].total == 2

    override = {"message": "Changed policy", "levels": {"warning": {"min_score": 1}, "error": {"min_score": 3}}}
    replayed = replay_case(original, override)
    assert replayed.assessment.status == "warning"
    assert replayed.comparable
    assert replayed.effective_hashes.report != original.hashes.report


def test_actual_identity_mismatch_is_excluded_with_detailed_records_and_fields() -> None:
    original = CalibrationCase.model_validate(case_document())
    changed_evidence = CalibrationCase.model_validate(case_document(source="abcdefghik"))
    second_original = CalibrationCase.model_validate(case_document(case_id="case-2"))
    changed_endpoint = CalibrationCase.model_validate(
        case_document(case_id="case-2", endpoint="https://other.example.test")
    )
    report = replay_cases([original, changed_evidence, second_original, changed_endpoint]).as_dict()

    assert [notice["case_id"] for notice in report["non_comparable"]] == ["case-1", "case-2"]
    assert report["non_comparable"][0]["fields"] == ["evidence"]
    assert report["non_comparable"][1]["fields"] == ["endpoint"]
    conflict = report["non_comparable"][0]["conflicts"][0]
    assert conflict["field"] == "evidence"
    assert {item["record_index"] for item in conflict["records"]} == {0, 1}
    assert report["rules"]["renamed-rule"]["replay"]["total"] == 0
    assert [record["comparability_mismatches"][0]["field"] for record in report["records"]] == [
        "evidence",
        "evidence",
        "endpoint",
        "endpoint",
    ]


def test_changed_question_with_updated_identity_is_not_paired() -> None:
    original = CalibrationCase.model_validate(case_document())
    changed = case_document()
    changed["rule"]["question"]["instructions"] = "A different question"
    changed_rule = Rule.model_validate(changed["rule"])
    changed_target = Target("target", "unit", "sample.py", "python", "sample", 0, 10, 1, 1, Kind.FUNCTION)
    changed_question = Check("case-1", changed_target, "renamed-rule", changed_rule).question()
    changed["hashes"]["question"] = _hash_bytes(encode(changed_question))
    changed["hashes"]["rule"] = _hash_bytes(encode(changed_rule.model_dump(mode="json")))
    changed["hashes"]["report"] = _hash_bytes(encode(changed_rule.report.model_dump(mode="json")))
    changed["comparability"]["question"] = changed["hashes"]["question"]
    changed_case = CalibrationCase.model_validate(changed)

    assert replay_cases([original, changed_case]).non_comparable


def test_output_retains_provenance_identity_and_assessment_outcome() -> None:
    case = CalibrationCase.model_validate(case_document(adjudicated_severity="warning", target_complete=True))
    record = replay_cases([case]).as_dict()["records"][0]

    assert record["label"] == "Agree"
    assert record["explanation"] == "The target obscures a transition."
    assert record["provenance"]["annotator"] == "team"
    assert record["hashes"]["question"].startswith("sha256:")
    assert record["comparability"]["endpoint"] == "https://example.test"
    assert record["status"] == "warning"
    assert record["finding"]["severity"] == "warning"
    assert record["tentative_finding"] is None
    assert record["outcome"]["status"] == record["status"]
    assert record["rule"]["question"]["type"] == "score"
    assert record["answer"]["type"] == "score"
    assert record["context_complete"] is True
    assert record["target_complete"] is True


def test_current_question_binding_delegates_to_the_generic_protocol_binder() -> None:
    rule = score_rule({"min_score": 2}, {"min_score": 3})
    check = score_check(rule)

    assert check.question() == bind_question(rule.question, check.target, QUESTION_POLICY)


@pytest.mark.parametrize("policy", [QUESTION_POLICY_V4, QUESTION_POLICY_V4_EARLY])
def test_supported_historical_prompt_material_is_reconstructed_and_hashed(policy: str) -> None:
    document = case_document(prompt_version=4, prompt_policy=policy)
    case = CalibrationCase.model_validate(document)
    target = case.target
    historical_wire = prompt_binder(4, policy).bind(case.rule.question, target, policy)
    current_wire = Check(case.case_id, target, case.rule_id, case.rule).question()

    assert case.hashes.question == _hash_bytes(encode(historical_wire))
    assert case.hashes.question != _hash_bytes(encode(current_wire))
    assert replay_case(case).comparable


def test_supported_prompt_identities_are_non_comparable_not_invalid() -> None:
    latest = CalibrationCase.model_validate(case_document(prompt_version=4, prompt_policy=QUESTION_POLICY_V4))
    early = CalibrationCase.model_validate(case_document(prompt_version=4, prompt_policy=QUESTION_POLICY_V4_EARLY))

    report = replay_cases([latest, early]).as_dict()

    assert report["non_comparable"][0]["case_id"] == "case-1"
    assert report["non_comparable"][0]["fields"] == ["question", "prompt"]


def test_unknown_prompt_policy_cannot_reconstruct_a_canonical_wire() -> None:
    document = case_document(prompt_version=4, prompt_policy="not-a-recorded-policy")

    with pytest.raises(ValueError, match="prompt.policy"):
        CalibrationCase.model_validate(document)


@pytest.mark.parametrize(
    ("case_factory", "message"),
    [
        (lambda: case_document(label="Disagree", adjudicated_severity="warning"), "Disagree"),
        (lambda: case_document(label="Disagree", adjudicated_severity="error"), "Disagree"),
    ],
)
def test_disagree_cannot_carry_a_finding_severity(case_factory: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CalibrationCase.model_validate(case_factory())


def test_partial_severity_is_allowed_and_remains_separate() -> None:
    partial = CalibrationCase.model_validate(case_document(label="Partial", adjudicated_severity="warning"))
    disagree_no_severity = CalibrationCase.model_validate(
        case_document(case_id="disagree-no-severity", label="Disagree")
    )

    report = replay_cases([partial, disagree_no_severity]).as_dict()["rules"]["renamed-rule"]["replay"]

    assert report["partial_confirmed"] == {"count": 1, "denominator": 1, "fraction": 1.0}
    assert report["label_counts"]["Partial"] == 1


def test_severity_metrics_keep_misses_and_wrong_severity_in_the_denominator() -> None:
    confirmed_warning = CalibrationCase.model_validate(
        case_document(case_id="confirmed-warning", adjudicated_severity="warning")
    )
    confirmed_error = case_document(
        case_id="confirmed-error",
        warning={"min_score": 2},
        error={"min_score": 2},
        adjudicated_severity="error",
    )
    wrong_severity = CalibrationCase.model_validate(
        case_document(
            case_id="wrong-severity",
            warning={"min_score": 2},
            error={"min_score": 2},
            adjudicated_severity="warning",
        )
    )
    none = case_document(case_id="miss", adjudicated_severity="error")
    none["answer"]["score"] = 1.0
    tentative = case_document(
        case_id="tentative",
        warning={"min_score": 2, "min_confidence": 0.7},
        error={"min_score": 3, "min_confidence": 0.8},
        adjudicated_severity="warning",
    )
    tentative["answer"]["confidence"] = 0.4

    report = replay_cases([
        confirmed_warning,
        CalibrationCase.model_validate(confirmed_error),
        wrong_severity,
        CalibrationCase.model_validate(none),
        CalibrationCase.model_validate(tentative),
    ]).as_dict()["rules"]["renamed-rule"]["replay"]

    assert report["confirmed_severity_agreement"] == {"count": 2, "denominator": 5, "fraction": 0.4}
    assert report["review_list_severity_agreement"] == {"count": 3, "denominator": 5, "fraction": 0.6}
    assert report["severity_confusion"] == {
        "warning": {
            "none": 0,
            "confirmed_warning": 1,
            "confirmed_error": 1,
            "tentative_warning": 1,
            "tentative_error": 0,
        },
        "error": {
            "none": 1,
            "confirmed_warning": 0,
            "confirmed_error": 1,
            "tentative_warning": 0,
            "tentative_error": 0,
        },
    }


def test_multibyte_utf8_target_and_evidence_spans_are_validated_as_bytes() -> None:
    case = CalibrationCase.model_validate(case_document(source="α\nβ"))

    assert case.target.end_byte == len("α\nβ".encode())


@pytest.mark.parametrize("source,target_start,target_end", [("abc\n", 4, 4), ("", 0, 0)])
def test_eof_and_empty_spans_use_context_builder_line_convention(
    source: str,
    target_start: int,
    target_end: int,
) -> None:
    case = CalibrationCase.model_validate(
        case_document(source=source, target_start=target_start, target_end=target_end)
    )

    assert case.target.start_line == case.target.end_line


def test_target_path_must_have_complete_source_material() -> None:
    document = case_document()
    document["target"]["path"] = "other.py"

    with pytest.raises(ValueError, match="target.path"):
        CalibrationCase.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda document: document["evidence"]["state"]["documents"][0].update({"end_byte": 11}), "byte offsets"),
        (
            lambda document: document["evidence"]["state"]["documents"][0].update({"start_byte": 1, "end_byte": 3}),
            "UTF-8 boundaries",
        ),
        (
            lambda document: document["evidence"]["state"]["documents"][0].update({"end_line": 2}),
            "line numbers",
        ),
    ],
)
def test_evidence_bounds_boundaries_and_lines_are_strict(mutation: Any, message: str) -> None:
    document = case_document(source="éx")
    mutation(document)

    with pytest.raises(ValueError, match=message):
        CalibrationCase.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda document: document["target"].update({"end_byte": 4}), "byte span"),
        (lambda document: document["target"].update({"start_byte": 1}), "UTF-8 boundaries"),
        (lambda document: document["target"].update({"end_line": 2}), "line numbers"),
    ],
)
def test_target_bounds_boundaries_and_lines_are_strict(mutation: Any, message: str) -> None:
    document = case_document(source="éx")
    mutation(document)

    with pytest.raises(ValueError, match=message):
        CalibrationCase.model_validate(document)


def test_target_coverage_is_required_only_when_completeness_claims_it() -> None:
    incomplete = case_document(target_start=2, target_end=8, evidence_end=2, context_complete=False)
    incomplete["target_complete"] = False
    assert CalibrationCase.model_validate(incomplete).target_complete is False

    claimed_context = dict(incomplete)
    claimed_context["context_complete"] = True
    with pytest.raises(ValueError, match="target_complete"):
        CalibrationCase.model_validate(claimed_context)

    claimed_context["target_complete"] = True
    with pytest.raises(ValueError, match="covered"):
        CalibrationCase.model_validate(claimed_context)

    claimed_target = dict(incomplete)
    claimed_target["target_complete"] = True
    with pytest.raises(ValueError, match="covered"):
        CalibrationCase.model_validate(claimed_target)


@pytest.mark.parametrize("context_complete", [False, True])
def test_adjacent_evidence_spans_use_production_coverage_union(context_complete: bool) -> None:
    document = case_document(
        target_start=2,
        target_end=8,
        context_complete=context_complete,
        target_complete=True,
    )
    _set_evidence_spans(document, "abcdefghij", [(0, 5), (5, 10)])

    assert CalibrationCase.model_validate(document).target_complete is True


def test_actual_evidence_gap_is_not_target_coverage() -> None:
    document = case_document(
        target_start=2,
        target_end=8,
        context_complete=False,
        target_complete=True,
    )
    _set_evidence_spans(document, "abcdefghij", [(0, 4), (6, 10)])

    with pytest.raises(ValueError, match="covered"):
        CalibrationCase.model_validate(document)
