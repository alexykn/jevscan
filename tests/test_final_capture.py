import json
import os

import pytest

from jevscan.cli.calibrate import main as calibrate_main
from jevscan.cli.import_calibration import main as import_main
from jevscan.core.assessment import Assessment
from jevscan.core.capture import FinalJudgmentRecorder
from jevscan.core.config import EnrichmentConfig
from jevscan.core.context import Evidence
from jevscan.core.enrichment_routing import DISPOSITIONS, allowed_families, routing_questions
from jevscan.core.evaluation import Judgment, TargetResults
from jevscan.core.models import Target
from jevscan.core.calibration_capture_validation import has_canonical_not_applicable_route
from jevscan.core.protocol import Check, NoulAnswer, PromptRegistry, encode
from jevscan.core.semantic_calibration import FinalCaptureMaterial, load_cases


def test_final_capture_preserves_final_evidence_review_and_imports(tmp_path, basic_rule, unit):
    source = "def work():\n    return 1\n"
    target = Target.from_unit(unit)
    state = {
        "documents": [
            {
                "path": "sample.py",
                "language": "python",
                "start_byte": 0,
                "end_byte": len(source.encode("utf-8")),
                "start_line": 1,
                "end_line": 2,
                "content": source,
            }
        ],
        "coverage": {"file_complete": True},
    }
    check = Check("final-case", target, "cohesion", basic_rule)
    sibling_rule = basic_rule.model_copy(
        update={"question": basic_rule.question.model_copy(update={"instructions": "A sibling rule question."})}
    )
    rubric = PromptRegistry.from_rules({"cohesion": basic_rule, "sibling": sibling_rule})
    judgment = Judgment(
        check,
        NoulAnswer(type="noul", noul=0.99),
        {"context_complete": True, "target_complete": True},
        "jev-1.13.0",
        True,
        Evidence(state, encode(state)),
        {"phase": "reassess", "cached": True},
        {
            "phase": "reassess",
            "cached": True,
            "initial_cached": True,
            "final_status": "error",
            "predictions": [
                {
                    "phase": "route",
                    "question_wires": {"disposition": {"type": "choice", "instructions": "route"}},
                    "cached": True,
                }
            ],
        },
        rubric.state_bytes(state),
        check.question(),
    )
    event = TargetResults(target, judgments={"cohesion": judgment}).event()
    assert "initial_evidence_state" not in json.dumps(event)
    assert source not in json.dumps(event)
    capture_path = tmp_path / "final.jsonl"
    recorder = FinalJudgmentRecorder(
        capture_path,
        "jev-1.13.0",
        "https://api.typesafe.ai",
        {"source": "test", "snapshot": "test-snapshot"},
    )
    recorder.record(judgment, Assessment("error"), {"sample.py": source})
    recorder.mark_complete(True, "complete")
    recorder.close()

    labels = tmp_path / "labels.json"
    case_id = json.loads(capture_path.read_text().splitlines()[0])["case_id"]
    labels.write_text(
        json.dumps({
            "cases": [
                {
                    "case_id": case_id,
                    "label": "Agree",
                    "explanation": "The final reassessment is adjudicated as a defect.",
                }
            ]
        })
    )
    output = tmp_path / "cases.jsonl"
    assert import_main([str(capture_path), "--labels", str(labels), "-o", str(output)]) == 0
    cases = load_cases(output)
    assert len(cases) == 1
    case = cases[0]
    assert case.capture is not None
    assert case.capture.phase == "final"
    assert case.capture.review["initial_cached"] is True
    assert case.capture.review["predictions"][0]["question_wires"]["disposition"]["type"] == "choice"
    assert case.evidence.source_documents["sample.py"] == source
    assert len(case.evidence.state["jevscan_prompt"]["rubrics"]) == 2
    assert case.question_wire == check.question()


def test_incomplete_capture_requires_explicit_import_acknowledgment(tmp_path, unit):
    capture_path = tmp_path / "incomplete.jsonl"
    recorder = FinalJudgmentRecorder(capture_path, "jev-1.13.0", "https://api.typesafe.ai", {})
    recorder.record_skip(Target.from_unit(unit), "cohesion", "scan failed after an earlier file")
    recorder.close()
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"cases": []}))
    output = tmp_path / "cases.jsonl"
    assert import_main([str(capture_path), "--labels", str(labels), "-o", str(output)]) == 2


@pytest.mark.parametrize("alias_kind", ["same_path", "symlink", "hardlink"])
def test_import_rejects_capture_alias_without_overwriting_input(tmp_path, alias_kind):
    capture_path = tmp_path / "capture.jsonl"
    capture_path.write_text("capture must survive\n")
    labels = tmp_path / "labels.json"
    labels.write_text("{}")
    output = tmp_path / "cases.jsonl"
    if alias_kind == "same_path":
        output = capture_path
    elif alias_kind == "symlink":
        try:
            output.symlink_to(capture_path)
        except OSError as exc:
            pytest.skip(f"symlinks are unavailable: {exc}")
    else:
        try:
            os.link(capture_path, output)
        except OSError as exc:
            pytest.skip(f"hard links are unavailable: {exc}")

    original = capture_path.read_bytes()
    assert import_main([str(capture_path), "--labels", str(labels), "-o", str(output)]) == 2
    assert capture_path.read_bytes() == original


@pytest.mark.parametrize("protected_name", ["labels.json", "capture.meta.json"])
def test_import_rejects_output_aliasing_protected_sidecar_inputs(tmp_path, protected_name):
    capture_path = tmp_path / "capture.jsonl"
    capture_path.write_text("capture must survive\n")
    metadata_path = capture_path.with_suffix(".meta.json")
    metadata_path.write_text("{}")
    labels = tmp_path / "labels.json"
    labels.write_text("{}")
    output = tmp_path / protected_name
    original = output.read_bytes()

    assert import_main([str(capture_path), "--labels", str(labels), "-o", str(output)]) == 2
    assert output.read_bytes() == original


def test_replay_rejects_output_alias_without_overwriting_input(tmp_path):
    cases = tmp_path / "cases.jsonl"
    cases.write_text("not a calibration case\n")
    original = cases.read_bytes()

    assert calibrate_main([str(cases), "--output", str(cases)]) == 2
    assert cases.read_bytes() == original


def test_routed_not_applicable_requires_canonical_route_wire(tmp_path, basic_rule, unit):
    source = "def work():\n    return 1\n"
    target = Target.from_unit(unit)
    state = {
        "documents": [
            {
                "path": "sample.py",
                "language": "python",
                "start_byte": 0,
                "end_byte": len(source.encode()),
                "start_line": 1,
                "end_line": 2,
                "content": source,
            }
        ],
        "coverage": {"file_complete": True},
    }
    check = Check("route-case", target, "cohesion", basic_rule)
    judgment = Judgment(
        check,
        NoulAnswer(type="noul", noul=0.1),
        {"context_complete": True, "target_complete": True},
        "jev-1.13.0",
        True,
        Evidence(state, encode(state)),
        {},
        {},
    )
    capture_path = tmp_path / "route.jsonl"
    recorder = FinalJudgmentRecorder(capture_path, "jev-1.13.0", "https://api.typesafe.ai", {})
    recorder.record(judgment, Assessment("ok"), {"sample.py": source})
    recorder.mark_complete(True, "complete")
    recorder.close()
    row = json.loads(capture_path.read_text().splitlines()[0])
    row["disposition"] = {"status": "not_applicable", "reason": "model_routed_not_applicable"}
    row["review"] = {
        "final_status": "not_applicable",
        "predictions": [
            {
                "phase": "route",
                "question_wires": {"disposition": {"type": "noul", "instructions": {}}},
                "answers": {"disposition": {"type": "noul", "noul": 1.0}},
            }
        ],
    }
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    capture_path.write_text(line)
    metadata_path = capture_path.with_suffix(".meta.json")
    metadata = json.loads(metadata_path.read_text())
    metadata["capture_sha256"] = "sha256:" + __import__("hashlib").sha256(line.encode()).hexdigest()
    metadata_path.write_text(json.dumps(metadata))
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps({
            "cases": [
                {
                    "case_id": row["case_id"],
                    "label": "Disagree",
                    "explanation": "route tamper",
                }
            ]
        })
    )
    assert import_main([str(capture_path), "--labels", str(labels), "-o", str(tmp_path / "out.jsonl")]) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        {"min_route_confidence": -0.1},
        {"min_route_probability": float("nan")},
        {"allowed_families": []},
    ],
)
def test_routed_not_applicable_rejects_invalid_routing_thresholds(basic_rule, unit, mutation):
    source = "def work():\n    return 1\n"
    target = Target.from_unit(unit)
    state = {
        "documents": [
            {
                "path": "sample.py",
                "language": "python",
                "start_byte": 0,
                "end_byte": len(source.encode()),
                "start_line": 1,
                "end_line": 2,
                "content": source,
            }
        ],
        "coverage": {"file_complete": True},
    }
    check = Check("route-case", target, "cohesion", basic_rule)
    families = allowed_families(check, EnrichmentConfig())
    questions = routing_questions(check, families)
    answers = {
        "disposition": {
            "type": "choice",
            "choice": "not_applicable",
            "confidence": 0.99,
            "probabilities": {key: (0.99 if key == "not_applicable" else 0.01 / 3) for key in DISPOSITIONS},
        },
        **{name: {"type": "noul", "noul": 0.1} for name in families},
    }
    review_config = {
        "allowed_families": list(families),
        "min_route_confidence": 0.5,
        "min_route_probability": 0.7,
        "min_evidence_probability": 0.6,
        **mutation,
    }
    review = {
        "routing_config": review_config,
        "predictions": [
            {
                "phase": "route",
                "question_wires": {name: check.auxiliary(questions[name]) for name in questions},
                "answers": answers,
            }
        ],
    }
    evidence = {
        "state": state,
        "source_documents": {"sample.py": source},
    }
    capture = FinalCaptureMaterial.model_validate({
        "phase": "final",
        "question_wire": check.question(),
        "evidence": evidence,
        "answer": {"type": "noul", "noul": 0.1},
        "disposition": {"status": "not_applicable", "reason": "model_routed_not_applicable"},
        "context_complete": True,
        "target_complete": True,
        "returned_model": "jev-test",
        "assessment": {"status": "not_applicable", "reason": "model_routed_not_applicable"},
        "review": review,
    })
    with pytest.raises(ValueError, match=r"min_route_|invalid routing families"):
        has_canonical_not_applicable_route(capture, check)
