"""Validate captured enrichment routing against the production routing contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from pydantic import TypeAdapter

from jevscan.core.config import EnrichmentConfig
from jevscan.core.enrichment_routing import EVIDENCE_FAMILIES, routing_decision, routing_questions
from jevscan.core.protocol import Answer, Check, validate_answer

if TYPE_CHECKING:
    from jevscan.core.calibration_cases import FinalCaptureMaterial

_ANSWER_ADAPTER = TypeAdapter(Answer)


def _routing_config(capture: FinalCaptureMaterial) -> tuple[tuple[str, ...], EnrichmentConfig]:
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
    return families, limits


def _route_predictions(capture: FinalCaptureMaterial) -> list[Mapping[str, Any]]:
    return [
        prediction
        for prediction in capture.review.get("predictions", [])
        if isinstance(prediction, Mapping) and prediction.get("phase") == "route"
    ]


def _prediction_material(prediction: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    wires = prediction.get("question_wires")
    answers = prediction.get("answers")
    if not isinstance(wires, Mapping) or not isinstance(answers, Mapping):
        raise TypeError("final not-applicable capture has incomplete route prediction")
    return wires, answers


def _captured_answer(
    name: str,
    raw_wire: Any,
    raw_answers: Mapping[str, Any],
    check: Check,
    questions: Mapping[str, Any],
) -> Answer:
    if name not in questions or raw_wire != check.auxiliary(questions[name]):
        raise ValueError("final not-applicable capture has a non-canonical route wire")
    if name not in raw_answers:
        raise ValueError("final not-applicable capture route answer is missing")
    answer = _ANSWER_ADAPTER.validate_python(raw_answers[name])
    validate_answer(answer, questions[name], name)
    return answer


def _prediction_answers(
    prediction: Mapping[str, Any],
    check: Check,
    questions: Mapping[str, Any],
) -> dict[str, Answer]:
    wires, raw_answers = _prediction_material(prediction)
    return {name: _captured_answer(name, raw_wire, raw_answers, check, questions) for name, raw_wire in wires.items()}


def _captured_route_answers(
    capture: FinalCaptureMaterial,
    check: Check,
    questions: Mapping[str, Any],
) -> dict[str, Answer]:
    answers: dict[str, Answer] = {}
    for prediction in _route_predictions(capture):
        answers.update(_prediction_answers(prediction, check, questions))
    if set(answers) != set(questions):
        raise ValueError("final not-applicable capture does not contain the complete route decision")
    return answers


def has_canonical_not_applicable_route(capture: FinalCaptureMaterial, check: Check) -> bool:
    families, limits = _routing_config(capture)
    questions = routing_questions(check, families)
    answers = _captured_route_answers(capture, check, questions)
    decision = routing_decision(
        answers,
        families,
        min_route_confidence=limits.min_route_confidence,
        min_route_probability=limits.min_route_probability,
        min_evidence_probability=limits.min_evidence_probability,
    )
    return decision.disposition == "not_applicable"
