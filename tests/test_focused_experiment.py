"""Offline contract tests for the focused-question experiment scaffold."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from calibration.focused_experiment import (
    _PAIR_TASK_MARKER,
    EVOLUTION_CANDIDATES_CONFIG,
    FOCUSED_MANIFEST,
    MODEL,
    PROJECT_ROOT,
    _capture_items,
    _metrics_for_records,
    _offline_composed_metrics,
    _pair_binding,
    _record_pair_metadata,
    candidate_documents,
    candidate_rule_ids,
    capture_manifest,
    compose_jev04_pair,
    compose_noul_and,
    evolution_candidate_documents,
    freeze_candidates,
    load_manifest,
    plan_manifest,
    replay_metrics,
    source_snapshot,
    write_candidate_config,
)
from calibration.validation_candidates import ExtractionLimits, FallbackReason
from jevscan.core.config import load_config
from jevscan.core.rules import ChoiceQuestion, NoulQuestion, ScoreQuestion
from jevscan.core.semantic_calibration import load_cases

pytestmark = [pytest.mark.parser, pytest.mark.usefixtures("grammar_runtime")]

CORPUS = FOCUSED_MANIFEST.parent
SOURCE_ROOT = CORPUS / "sources"


def _manifest() -> dict:
    return yaml.safe_load((CORPUS / "MANIFEST.yaml").read_text(encoding="utf-8"))


def test_focused_source_snapshot_and_manifest_contract() -> None:
    manifest = _manifest()
    assert manifest["source_snapshot"]["algorithm"] == "sha256"
    assert manifest["source_snapshot"]["digest"] == source_snapshot(SOURCE_ROOT)
    assert manifest["source_contract"]["labels_are_outside_source_root"] is True
    assert manifest["source_contract"]["live_model_calls"] == "none"
    assert "Partial" in manifest["source_contract"]["label_vocabulary"]


def test_evolution_candidate_config_is_nonproduction_and_exact() -> None:
    loaded = load_config(
        [EVOLUTION_CANDIDATES_CONFIG.parent],
        explicit=EVOLUTION_CANDIDATES_CONFIG,
        cwd=PROJECT_ROOT,
    ).config
    expected = {
        "EXP_JEV01_RESPONSIBILITIES",
        "EXP_JEV01_INTERLEAVING",
        "EXP_JEV03_CONCRETE_MECHANICS",
        "EXP_JEV05_CORRECTED",
        "EXP_JEV08_CHOICE",
        "EXP_PARTIAL_STATE_TRANSITION",
        "EXP_JEV04_PAIR_GUARANTEE",
        "EXP_JEV04_PAIR_PRESERVATION",
    }
    assert set(loaded.selected_rules()) == expected
    assert not loaded.rulesets["JEV"].enabled
    assert all(loaded.rules[name].ruleset == "experiment-candidates" for name in expected)
    assert not expected & {"JEV02", "JEV06", "JEV07", "JEV09"}


def test_evolution_candidate_questions_have_fresh_exact_contracts() -> None:
    documents = evolution_candidate_documents()
    assert set(documents) == {
        "EXP_JEV01_RESPONSIBILITIES",
        "EXP_JEV01_INTERLEAVING",
        "EXP_JEV03_CONCRETE_MECHANICS",
        "EXP_JEV05_CORRECTED",
        "EXP_JEV08_CHOICE",
        "EXP_PARTIAL_STATE_TRANSITION",
        "EXP_JEV04_PAIR_GUARANTEE",
        "EXP_JEV04_PAIR_PRESERVATION",
    }
    questions = {name: document["question"] for name, document in documents.items()}
    assert len({json.dumps(question, sort_keys=True) for question in questions.values()}) == len(questions)
    assert questions["EXP_JEV01_RESPONSIBILITIES"]["type"] == "noul"
    assert questions["EXP_JEV01_INTERLEAVING"]["type"] == "noul"
    assert "independently meaningful responsibilities" in questions["EXP_JEV01_RESPONSIBILITIES"]["instructions"]
    assert (
        "interleave two or more independently meaningful policies"
        in questions["EXP_JEV01_INTERLEAVING"]["instructions"]
    )
    assert questions["EXP_JEV03_CONCRETE_MECHANICS"]["instructions"] == (
        "Does the target mix high-level orchestration with substantial inline concrete mechanics that obscures its "
        "primary operation? Count substantial inline byte or token scanning, decoding, AST traversal or normalization, "
        "descriptor or selector ownership, nonblocking I/O, retry or backoff, and rollback or resource ownership. "
        "Calls or helpers, ordinary validation, metadata, cache or index bookkeeping, small cleanup, and a single "
        "cohesive parser or resource boundary are insufficient."
    )
    assert questions["EXP_JEV05_CORRECTED"]["instructions"] == (
        "Select exactly one outcome for a concrete failure or rejected state visible in the target. Choose concealed_bug "
        "only when that failure or rejected state is converted into an ordinary result, the target visibly establishes "
        "a required postcondition that the result violates, and no explicit non-success remains at that boundary. "
        "Choose legitimate_behavior when explicit non-success is preserved or the target shows that the fallback is "
        "allowed; an empty result, default, cancellation, or exhaustion is not a defect by itself. Choose "
        "insufficient_context when the supplied evidence does not establish the postcondition, whether the path is a "
        "failure, or whether the fallback is allowed. Do not infer the contract from names, comments, tests, callers, "
        "types, data shape, or API convention."
    )
    assert questions["EXP_JEV05_CORRECTED"]["criteria"] == {
        "concealed_bug": "A concrete failure or rejected state is converted into an ordinary result; the target visibly "
        "establishes a required postcondition that the result violates, and no explicit non-success remains at that "
        "boundary.",
        "legitimate_behavior": "Explicit non-success is preserved or the target shows that the fallback is allowed; an "
        "empty result, default, cancellation, or exhaustion is not a defect by itself.",
        "insufficient_context": "The supplied evidence does not establish the required postcondition, whether the path "
        "is a failure, or whether the fallback is allowed; do not infer any of those facts from names, comments, tests, "
        "callers, types, data shape, or API convention.",
    }
    assert questions["EXP_JEV08_CHOICE"]["instructions"] == (
        "Select exactly one outcome for the supplied owner. Choose unrelated_responsibilities only when at least two "
        "independently meaningful behaviors serve different external purposes and no shared lifecycle, domain invariant, "
        "store, resource, aggregate, facade, or interface boundary explains their ownership. Choose cohesive_owner when "
        "one such boundary exists, including a sequential workflow. Choose insufficient_context when the enclosing or "
        "caller design determines the classification; method count alone is not evidence."
    )
    assert questions["EXP_JEV08_CHOICE"]["criteria"] == {
        "unrelated_responsibilities": "At least two independently meaningful behaviors serve different external "
        "purposes, and no shared lifecycle, domain invariant, store, resource, aggregate, facade, or interface boundary "
        "explains their ownership.",
        "cohesive_owner": "One shared lifecycle, domain invariant, store, resource, aggregate, facade, or interface "
        "boundary explains the behavior, including a sequential workflow.",
        "insufficient_context": "The enclosing or caller design is required to determine whether the behaviors serve "
        "different external purposes or share a cohesive boundary.",
    }
    assert questions["EXP_PARTIAL_STATE_TRANSITION"]["instructions"] == (
        "Select exactly one outcome for narrow temporal atomicity in the target. Choose unaccounted_partial_transition "
        "only when two or more semantically coupled externally observable state effects are visible, a reachable "
        "failure, cancellation, early return, or retry can leave only a subset visible or durable, and there is no "
        "atomic boundary, rollback, compensation, reconciliation, or explicitly valid intermediate or progressive "
        "contract. Exclude independent logging, metrics, or caches, local temporary construction, and a complete build "
        "followed by one publication. Choose accounted_transition when those effects are covered by such a boundary or "
        "contract, or no qualifying transition is visible. Choose insufficient_context when reachability or the local "
        "transition contract is unresolved."
    )
    assert questions["EXP_PARTIAL_STATE_TRANSITION"]["criteria"] == {
        "unaccounted_partial_transition": "Two or more semantically coupled externally observable state effects are "
        "visible; a reachable failure, cancellation, early return, or retry can leave only a subset visible or durable; "
        "and no atomic boundary, rollback, compensation, reconciliation, or explicitly valid intermediate or "
        "progressive contract covers that subset.",
        "accounted_transition": "The effects are covered by an atomic boundary, rollback, compensation, reconciliation, "
        "or explicitly valid intermediate or progressive contract, or the target shows no qualifying transition. "
        "Independent logging, metrics, caches, local temporary construction, and complete build-then-one-publication "
        "patterns are excluded.",
        "insufficient_context": "A qualifying partial transition may exist, but reachability or the local transition "
        "contract is unresolved.",
    }
    assert documents["EXP_PARTIAL_STATE_TRANSITION"]["report"]["levels"] == {
        "warning": {
            "min_probability": 0.6,
            "min_confidence": 0.5,
            "min_score": None,
            "max_score": None,
            "score_levels": None,
        },
        "error": {
            "min_probability": 0.92,
            "min_confidence": 0.7,
            "min_score": None,
            "max_score": None,
            "score_levels": None,
        },
    }
    assert questions["EXP_JEV04_PAIR_GUARANTEE"]["instructions"] == (
        "Judge only the bound earlier and later operations. Does the target visibly establish the same guarantee for the "
        "same checked value or state before the later operation? Answer true only when both validation operations and "
        "their equivalent guarantee or identity are present for the same checked value or state; identical text alone is "
        "insufficient. Answer false when that bound guarantee is not visible; do not infer it from names or comments, "
        "and do not assess other checks."
    )
    assert questions["EXP_JEV04_PAIR_PRESERVATION"]["instructions"] == (
        "Classify only the bound earlier and later operations. Choose preserved when both operations concern the same "
        "guarantee and same checked value or state, no mutation, reassignment, or alias is visible, and the mutable or "
        "aliasable checked subject or visible alias is not passed as an argument or receiver to an opaque or external "
        "call. Choose possible_invalidation only when mutation, reassignment, or alias is visible, or that mutable or "
        "aliasable subject or visible alias is passed as an argument or receiver to an opaque or external call. "
        "Unrelated opaque calls are not invalidation. Choose insufficient_context when a plausibly relevant boundary "
        "exists but reachability or its effect on mutable state is unresolved. An omitted body is not preservation when "
        "reachability is unresolved. An immutable captured primitive read-only remains preserved."
    )
    assert questions["EXP_JEV04_PAIR_PRESERVATION"]["criteria"] == {
        "preserved": "The bound operations concern the same guarantee and same checked value or state; no mutation, "
        "reassignment, or alias is visible; and no mutable or aliasable checked subject or visible alias is passed to an "
        "opaque or external call. An unrelated opaque call does not invalidate the guarantee, and an immutable captured "
        "primitive read-only remains preserved.",
        "possible_invalidation": "Mutation, reassignment, or alias is visible, or the mutable or aliasable checked "
        "subject or visible alias is passed as an argument or receiver to an opaque or external call before the later "
        "operation.",
        "insufficient_context": "A plausibly relevant boundary exists, but reachability or its effect on mutable state is "
        "unresolved. An omitted body is not preservation when that reachability remains unresolved.",
    }
    assert documents["EXP_PARTIAL_STATE_TRANSITION"]["applicability"] is None
    assert documents["EXP_JEV05_CORRECTED"]["applicability"] == {
        "requires_any": ["fallback_candidate"],
        "requires_all": [],
    }
    assert documents["EXP_JEV08_CHOICE"]["applicability"] == {
        "requires_any": ["executable_behavior"],
        "requires_all": [],
    }
    assert documents["EXP_JEV04_PAIR_GUARANTEE"]["applicability"] == {
        "requires_any": ["validation_candidate"],
        "requires_all": [],
    }


def test_evolution_static_children_share_evidence_and_do_not_send_labels() -> None:
    items, requests, _, _, _, _ = _capture_items(
        FOCUSED_MANIFEST,
        phase="development",
        config_path=EVOLUTION_CANDIDATES_CONFIG,
        freeze_path=None,
        candidates=["jev01-strict-and"],
    )
    assert len(items) == 24
    assert len(requests) == 12
    assert all(len(request.checks) == 2 for request in requests)
    assert all(
        {check.rule_id for check in request.checks}
        == {
            "EXP_JEV01_RESPONSIBILITIES",
            "EXP_JEV01_INTERLEAVING",
        }
        for request in requests
    )
    by_case: dict[str, list[Any]] = {}
    for item in items:
        by_case.setdefault(item.parent_case_id, []).append(item)
    assert all(
        len(case_items) == 2 and len({item.evidence.key for item in case_items}) == 1 for case_items in by_case.values()
    )
    request_material = b"\n".join(request.body for request in requests).lower()
    for label in (b"positive", b"negative", b"agree", b"disagree", b"partial"):
        assert label not in request_material


def test_evolution_composition_is_strict_and_deterministic() -> None:
    assert compose_noul_and([0.71, 0.71]) == "warning"
    assert compose_noul_and([0.9, 0.9]) == "error"
    assert compose_noul_and([0.9, 0.71]) == "warning"
    assert compose_noul_and([0.9, 0.70]) == "ok"
    assert compose_jev04_pair(0.71, "preserved", 0.60, 0.45) == "warning"
    assert compose_jev04_pair(0.90, "preserved", 0.92, 0.70) == "error"
    assert compose_jev04_pair(0.90, "preserved", 0.92, 0.69) == "warning"
    assert compose_jev04_pair(0.90, "possible_invalidation", 1.0, 1.0) == "ok"
    assert compose_jev04_pair(0.90, "insufficient_context", 1.0, 1.0) == "unknown"


def test_composed_metrics_do_not_promote_child_assessments() -> None:
    target = object()
    record = SimpleNamespace(
        signal=True,
        case=SimpleNamespace(
            case_id="child",
            rule_id="EXP_JEV01_RESPONSIBILITIES",
            label="Agree",
            target=target,
            provenance={"source_group": "group"},
        ),
        assessment=SimpleNamespace(
            finding=SimpleNamespace(severity="warning", target=target),
            tentative_finding=None,
        ),
    )
    metrics = _offline_composed_metrics([record], {"child": True})
    assert "severity" not in metrics
    assert "target_attribution" not in metrics


def test_evolution_pair_plan_preserves_exact_metadata_and_fallback() -> None:
    plan = plan_manifest(
        FOCUSED_MANIFEST,
        phase="development",
        config_path=EVOLUTION_CANDIDATES_CONFIG,
        candidates=["jev04-pair-corrected"],
    )
    assert plan["selected_candidates"] == ["jev04-pair-corrected"]
    assert plan["accounting"]["whole_target_fallbacks"] == 8
    assert {record["rule_id"] for record in plan["cases"]} == {
        "EXP_JEV04_PAIR_GUARANTEE",
        "EXP_JEV04_PAIR_PRESERVATION",
    }
    for record in plan["cases"]:
        binding = record["pair_binding"]
        assert binding["candidate"] == "jev04-pair-corrected"
        if binding["status"] == "whole_target_fallback":
            assert record["question_id"] is None
            assert binding["reason"]
        else:
            assert record["question_id"] is not None
            assert "intervening_bytes" not in json.dumps(binding)
            assert binding["pair"]["source_path"] == record["target"]["path"]
    _, requests, _, _, _, _ = _capture_items(
        FOCUSED_MANIFEST,
        phase="development",
        config_path=EVOLUTION_CANDIDATES_CONFIG,
        freeze_path=None,
        candidates=["jev04-pair-corrected"],
    )
    request_material = b"\n".join(request.body for request in requests).lower()
    for label in (b"positive", b"negative", b"agree", b"disagree", b"partial", b"ground truth"):
        assert label not in request_material


def test_focused_adjudications_match_the_frozen_source_and_manifest() -> None:
    manifest = _manifest()
    adjudications = yaml.safe_load((CORPUS / "ADJUDICATIONS.yaml").read_text(encoding="utf-8"))
    assert adjudications["source_snapshot_sha256"] == manifest["source_snapshot"]["digest"]

    expected = {entry["id"]: entry for entry in manifest["heldout"]}
    reviewed = {entry["case_id"]: entry for entry in adjudications["cases"]}
    assert reviewed.keys() == expected.keys()
    for case_id, review in reviewed.items():
        manifest_case = expected[case_id]
        expected_label = {"positive": "Agree", "negative": "Disagree", "Partial": "Partial"}[manifest_case["label"]]
        assert review["label"] == expected_label
        if manifest_case["rule"] == "JEV02":
            assert review["expected_score"] == manifest_case["expected_score"]
    assert {entry["case_id"] for entry in adjudications["corrections"]} == {
        "focused-jev01-h03",
        "focused-jev02-h02",
        "focused-jev04-h01",
    }


def test_focused_groups_have_isolated_split_and_required_support() -> None:
    manifest = _manifest()
    groups = {}
    for entry in manifest["heldout"]:
        groups.setdefault((entry["rule"], entry["group"]), []).append(entry)
    assert len(groups) == 18
    for rule in ("JEV01", "JEV02", "JEV04"):
        rule_groups = {group for current_rule, group in groups if current_rule == rule}
        assert len(rule_groups) == 6
        labels = {groups[(rule, group)][0]["label"] for group in rule_groups}
        assert labels == {"positive", "negative"}
        expected_positive = 2 if rule == "JEV02" else 3
        assert sum(groups[(rule, group)][0]["label"] == "positive" for group in rule_groups) == expected_positive
        expected_negative = 4 if rule == "JEV02" else 3
        assert sum(groups[(rule, group)][0]["label"] == "negative" for group in rule_groups) == expected_negative
    assert {entry["source_partition_in_expanded"] for entry in manifest["development_references"]} == {"heldout"}
    assert all(
        entry["development_only"] and not entry["counts_as_fresh_heldout"]
        for entry in manifest["development_references"]
    )
    assert {entry["id"] for entry in manifest["development_references"]} >= {"expanded:e046", "expanded:e048"}


def test_focused_labels_and_mapping_annotations_are_outside_source_root() -> None:
    manifest = _manifest()
    declared = {entry["source"].removeprefix("sources/") for entry in manifest["heldout"]}
    actual = {path.relative_to(SOURCE_ROOT).as_posix() for path in SOURCE_ROOT.rglob("*") if path.is_file()}
    assert declared == actual
    forbidden = ("positive", "negative", "ground truth", "provisional_label", "scenario_group")
    for path in SOURCE_ROOT.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8").lower()
            assert not any(token in text for token in forbidden), path
            assert not any(token in path.name.lower() for token in forbidden), path


def test_candidate_definitions_preserve_baselines_and_use_mixed_question_types() -> None:
    documents = candidate_documents()
    packaged = load_config([], cwd=Path(__file__).parents[1]).config.rules
    assert documents["FOCUS_JEV01_BASELINE"]["question"] == packaged["JEV01"].question.model_dump(mode="json")
    assert documents["FOCUS_JEV02_SCORE"]["question"] == packaged["JEV02"].question.model_dump(mode="json")
    assert documents["FOCUS_JEV04_BASELINE"]["question"] == packaged["JEV04"].question.model_dump(mode="json")
    assert documents["FOCUS_JEV02_PRESENCE"]["question"]["criteria"] == {
        "true": "The target contains a concrete control-flow structure that obscures an important execution transition.",
        "false": "The target does not show that traceability problem; local guards, cohesive lifecycles, and immutable captures do not qualify.",
    }
    assert documents["FOCUS_JEV04_JOINT"]["question"]["type"] == "choice"
    assert documents["FOCUS_JEV04_DECOMPOSED_GUARANTEE"]["question"]["type"] == "noul"
    assert documents["FOCUS_JEV04_DECOMPOSED_PRESERVATION"]["question"]["type"] == "noul"
    assert documents["FOCUS_JEV04_DECOMPOSED_GUARANTEE"]["question"]["criteria"] != packaged["JEV01"].question.criteria
    assert (
        documents["FOCUS_JEV04_DECOMPOSED_PRESERVATION"]["question"]["criteria"] != packaged["JEV01"].question.criteria
    )
    assert documents["FOCUS_JEV02_PRESENCE"]["report"] == packaged["JEV01"].report.model_dump(mode="json")
    assert isinstance(packaged["JEV01"].question, NoulQuestion)
    assert isinstance(packaged["JEV02"].question, ScoreQuestion)
    assert isinstance(packaged["JEV04"].question, ChoiceQuestion)


def test_pair_candidate_definitions_have_stable_ordinary_child_rules() -> None:
    documents = candidate_documents()

    assert documents["FOCUS_JEV04_PAIR_JOINT"]["question"]["type"] == "choice"
    assert documents["FOCUS_JEV04_PAIR_PRESERVATION"]["question"]["type"] == "choice"
    assert documents["FOCUS_JEV04_PAIR_DECOMPOSED_GUARANTEE"]["question"]["type"] == "noul"
    assert documents["FOCUS_JEV04_PAIR_DECOMPOSED_PRESERVATION"]["question"]["type"] == "noul"
    assert (
        documents["FOCUS_JEV04_PAIR_DECOMPOSED_GUARANTEE"]["report"]
        == documents["FOCUS_JEV04_DECOMPOSED_GUARANTEE"]["report"]
    )
    assert (
        documents["FOCUS_JEV04_PAIR_DECOMPOSED_PRESERVATION"]["report"]
        == documents["FOCUS_JEV04_DECOMPOSED_PRESERVATION"]["report"]
    )


def test_pair_preservation_emits_distinct_identity_and_packaged_choice_report() -> None:
    documents = candidate_documents()
    packaged = load_config([], cwd=PROJECT_ROOT).config.rules["JEV04"]
    preservation = documents["FOCUS_JEV04_PAIR_PRESERVATION"]
    joint = documents["FOCUS_JEV04_PAIR_JOINT"]

    assert candidate_rule_ids("jev04-pair-preservation") == ("FOCUS_JEV04_PAIR_PRESERVATION",)
    assert preservation["title"] == "focused-jev04-pair-preservation"
    assert preservation["question"]["type"] == "choice"
    assert preservation["question"]["criteria"] == packaged.question.model_dump(mode="json")["criteria"]
    assert preservation["report"] == packaged.report.model_dump(mode="json")
    assert preservation["question"]["instructions"] != joint["question"]["instructions"]
    for phrase in (
        "both checks test the same invariant for the same value or state",
        "A callback, await, or call matters only if it can mutate, alias, or replace",
        "A captured primitive immutable value that is never reassigned remains preserved",
        "Choose insufficient_context only when a missing boundary fact decides",
    ):
        assert phrase in preservation["question"]["instructions"]


def test_pair_plan_binds_quarry_and_summit_without_changing_target_or_evidence(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    baseline = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="development",
        config_path=config_path,
        candidates=["jev04-baseline"],
    )
    pair = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="development",
        config_path=config_path,
        candidates=["jev04-pair-joint"],
    )
    baseline_by_case = {record["case_id"]: record for record in baseline["cases"]}
    pair_by_case = {record["case_id"]: record for record in pair["cases"]}

    assert pair["selected_candidates"] == ["jev04-pair-joint"]
    assert len(pair["batches"]) == 8
    assert sum(record["question_id"] is not None for record in pair["cases"]) == 8
    assert pair["accounting"]["whole_target_fallbacks"] == 4
    assert {record["pair_binding"]["status"] for record in pair["cases"]} == {
        "bound",
        "whole_target_fallback",
    }
    for case_id, record in pair_by_case.items():
        assert record["target"] == baseline_by_case[case_id]["target"]
        assert record["evidence_sha256"] == baseline_by_case[case_id]["evidence_sha256"]
        binding = record["pair_binding"]
        assert "intervening_bytes" not in json.dumps(binding)
        if binding["status"] == "whole_target_fallback":
            assert record["question_id"] is None
            assert record["fallback"]["reason"]
            continue
        pair_metadata = binding["pair"]
        assert pair_metadata["source_path"] == record["target"]["path"]
        assert (
            pair_metadata["earlier"]["operation_span"]["start_byte"]
            < pair_metadata["later"]["operation_span"]["start_byte"]
        )
        assert pair_metadata["intervening_span"]["start_byte"] == pair_metadata["earlier"]["operation_span"]["end_byte"]
        assert pair_metadata["intervening_span"]["end_byte"] == pair_metadata["later"]["operation_span"]["start_byte"]

    quarry = pair_by_case["expanded:e046"]["pair_binding"]["pair"]
    assert quarry["earlier"]["operation_span"]["start_line"] == 3
    assert quarry["later"]["operation_span"]["start_line"] == 7
    summit = pair_by_case["expanded:e048"]["pair_binding"]["pair"]
    assert summit["callable_boundary"]["crossed"] is True
    assert summit["later"]["callable_boundary"]["owner_kind"] == "closure"


def test_pair_preservation_plan_keeps_exact_binding_and_records_fallback(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    baseline = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="development",
        config_path=config_path,
        candidates=["jev04-baseline"],
    )
    preservation = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="development",
        config_path=config_path,
        candidates=["jev04-pair-preservation"],
    )
    baseline_by_case = {record["case_id"]: record for record in baseline["cases"]}
    preservation_by_case = {record["case_id"]: record for record in preservation["cases"]}

    assert preservation["selected_candidates"] == ["jev04-pair-preservation"]
    assert preservation["accounting"]["whole_target_fallbacks"] == 4
    assert sum(record["question_id"] is not None for record in preservation["cases"]) == 8
    for case_id, record in preservation_by_case.items():
        assert record["rule_id"] == "FOCUS_JEV04_PAIR_PRESERVATION"
        assert record["target"] == baseline_by_case[case_id]["target"]
        assert record["evidence_sha256"] == baseline_by_case[case_id]["evidence_sha256"]
        binding = record["pair_binding"]
        assert binding["candidate"] == "jev04-pair-preservation"
        if binding["status"] == "whole_target_fallback":
            assert record["question_id"] is None
            assert binding["reason"]
        else:
            assert binding["status"] == "bound"
            assert record["question_id"] is not None
            pair = binding["pair"]
            assert "intervening_bytes" not in json.dumps(binding)
            assert pair["source_path"] == record["target"]["path"]
            assert pair["earlier"]["operation_span"]["start_byte"] < pair["later"]["operation_span"]["start_byte"]

    summit = preservation_by_case["expanded:e048"]["pair_binding"]["pair"]
    assert summit["callable_boundary"]["crossed"] is True
    assert summit["later"]["callable_boundary"]["owner_kind"] == "closure"


def test_pair_decomposed_plan_reuses_exact_pair_binding_for_both_children(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    plan = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="development",
        config_path=config_path,
        candidates=["jev04-pair-decomposed"],
    )
    by_case: dict[str, list[dict]] = {}
    for record in plan["cases"]:
        by_case.setdefault(record["case_id"], []).append(record)

    assert plan["selected_candidates"] == ["jev04-pair-decomposed"]
    assert all(len(records) == 2 for records in by_case.values())
    for records in by_case.values():
        bound = [record for record in records if record["pair_binding"]["status"] == "bound"]
        fallback = [record for record in records if record["pair_binding"]["status"] != "bound"]
        if fallback:
            assert len(fallback) == 2
            assert all(record["question_id"] is None for record in fallback)
            continue
        assert len(bound) == 2
        assert bound[0]["pair_binding"]["pair"] == bound[1]["pair_binding"]["pair"]


def test_pair_binding_records_zero_multiple_cap_and_unsupported_as_fallbacks() -> None:
    from jevscan.core.languages import language_for
    from jevscan.core.models import FileJob, Target
    from jevscan.core.parser import parse_source

    def binding(source: bytes, filename: str, limits: ExtractionLimits | None = None):
        path = Path(filename)
        spec = language_for(path)
        assert spec is not None
        parsed = parse_source(source, FileJob(f"/tmp/{filename}", filename, spec.grammar, spec.language))
        target = Target.from_unit(parsed.units[0])
        return _pair_binding(parsed, target, limits=limits or ExtractionLimits())

    zero = binding(b"def f(value):\n    if value:\n        return value\n", "zero.py")
    assert zero.reason == FallbackReason.NO_EXACT_PREDICATE_GROUP.value
    multiple = binding(
        b"def f(value):\n    if value:\n        return 1\n    if value:\n        return 2\n    if value:\n        return 3\n",
        "multiple.py",
    )
    assert multiple.reason == FallbackReason.MULTIPLE_PAIRS.value
    capped = binding(
        b"def f(value):\n    if value:\n        return 1\n    if value:\n        return 2\n",
        "capped.py",
        ExtractionLimits(max_occurrences=1),
    )
    assert capped.reason == FallbackReason.OCCURRENCE_CAP.value
    unsupported = binding(
        b"def f(value):\n    while value:\n        return 1\n    while value:\n        return 2\n",
        "unsupported.py",
    )
    assert unsupported.reason == FallbackReason.UNSUPPORTED_VALIDATION_SHAPE.value


def test_production_planner_binds_exact_targets_and_batches_same_evidence(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    with pytest.raises(ValueError, match="frozen"):
        plan_manifest(CORPUS / "MANIFEST.yaml", phase="heldout", config_path=config_path, freeze_path=None)


def test_heldout_plan_contains_only_frozen_candidates(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    development = plan_manifest(CORPUS / "MANIFEST.yaml", phase="development", config_path=config_path)
    development_path = tmp_path / "development.json"
    development_path.write_text(json.dumps(development), encoding="utf-8")
    freeze_path = tmp_path / "freeze.json"
    freeze_candidates(CORPUS / "MANIFEST.yaml", development_path, ["jev04-focused-joint"], freeze_path)
    heldout = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="heldout",
        config_path=config_path,
        freeze_path=freeze_path,
    )
    assert {record["candidate"] for record in heldout["cases"]} == {"jev04-focused-joint"}


def test_heldout_candidate_filter_cannot_escape_freeze(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    development = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="development",
        config_path=config_path,
        candidates=["jev04-pair-joint"],
    )
    development_path = tmp_path / "development.json"
    development_path.write_text(json.dumps(development), encoding="utf-8")
    freeze_path = tmp_path / "freeze.json"
    freeze_candidates(CORPUS / "MANIFEST.yaml", development_path, ["jev04-pair-joint"], freeze_path)

    with pytest.raises(ValueError, match="not frozen"):
        plan_manifest(
            CORPUS / "MANIFEST.yaml",
            phase="heldout",
            config_path=config_path,
            freeze_path=freeze_path,
            candidates=["jev04-baseline"],
        )


def test_production_planner_binds_exact_targets_and_batches_same_evidence_after_freeze(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    development = plan_manifest(CORPUS / "MANIFEST.yaml", phase="development", config_path=config_path)
    development_path = tmp_path / "development.json"
    development_path.write_text(json.dumps(development), encoding="utf-8")
    freeze_path = tmp_path / "freeze.json"
    freeze_candidates(
        CORPUS / "MANIFEST.yaml",
        development_path,
        [
            "jev01-baseline",
            "jev01-focused",
            "jev02-baseline",
            "jev02-presence-gated",
            "jev04-baseline",
            "jev04-focused-joint",
            "jev04-focused-decomposed",
        ],
        freeze_path,
    )
    plan = plan_manifest(
        CORPUS / "MANIFEST.yaml",
        phase="heldout",
        config_path=config_path,
        freeze_path=freeze_path,
    )
    manifest = _manifest()
    expected_by_case = {entry["id"]: entry for entry in manifest["heldout"]}
    by_case = {}
    for record in plan["cases"]:
        by_case.setdefault(record["case_id"], []).append(record)
        expected = expected_by_case[record["case_id"]]
        assert record["target"]["scope"] == expected["target"]["scope"]
        assert record["target"]["qualified_name"] == expected["target"]["name"]
        assert record["target"]["kind"].value == expected["target"]["kind"]
        assert record["source_sha256"] == hashlib.sha256((CORPUS / expected["source"]).read_bytes()).hexdigest()
        source = (CORPUS / expected["source"]).read_bytes()
        start, end = record["target"]["start_byte"], record["target"]["end_byte"]
        assert record["target_sha256"] == hashlib.sha256(source[start:end]).hexdigest()
        assert record["validation_pair"] == expected.get("validation_pair")
    for entry in manifest["heldout"]:
        records = by_case[entry["id"]]
        assert len(records) == {"JEV01": 2, "JEV02": 3, "JEV04": 4}[entry["rule"]]
        assert len({record["evidence_sha256"] for record in records}) == 1
        assert all(record["actual_provider_input_tokens"] is None for record in records)
    assert plan["actual_provider_input_tokens"] is None
    assert plan["reserved_cost"] <= plan["cumulative_cap"]
    assert any(batch["question_count"] > 1 for batch in plan["batches"])
    assert all(
        batch["reserved_input_tokens"]
        == batch["attempts_reserved_input_tokens"] + batch["margin_reserved_input_tokens"]
        for batch in plan["batches"]
    )
    assert plan["reserved_input_tokens"] == sum(batch["reserved_input_tokens"] for batch in plan["batches"])


def test_development_references_preserve_score_and_optional_documents(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    plan = plan_manifest(CORPUS / "MANIFEST.yaml", phase="development", config_path=config_path)
    by_case = {}
    for record in plan["cases"]:
        by_case.setdefault(record["case_id"], []).append(record)
    assert {record["expected_score"] for record in by_case["expanded:e021"]} == {2}
    assert {record["expected_score"] for record in by_case["expanded:e022"]} == {0}
    assert all(
        record["source_commit"] == "b13bb159c62d688b6248773b2e858fa3c02cf98" for record in by_case["expanded:e048"]
    )
    additional = by_case["expanded:e048"][0]["additional_sources"]
    assert additional[0]["source"] == "examples/calibration/expanded/sources/summit.js"
    assert additional[0]["source_sha256"]


def test_source_hash_is_independent_of_manifest_annotations() -> None:
    manifest = load_manifest()
    assert manifest["source_snapshot"]["digest"] == source_snapshot(SOURCE_ROOT)


def _write_freeze(path: Path, candidates: list[str]) -> Path:
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "phase": "frozen",
            "manifest_sha256": hashlib.sha256(FOCUSED_MANIFEST.read_bytes()).hexdigest(),
            "candidates": candidates,
        }),
        encoding="utf-8",
    )
    return path


def _mock_answer(question: dict) -> dict:
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.8}
    if question["type"] == "score":
        return {
            "type": "score",
            "score": 2,
            "confidence": 0.8,
            "probabilities": {"0": 0.05, "1": 0.1, "2": 0.8, "3": 0.05},
        }
    labels = list(question["criteria"])
    return {
        "type": "choice",
        "choice": labels[0],
        "confidence": 0.8,
        "probabilities": {label: 0.8 if index == 0 else 0.1 for index, label in enumerate(labels)},
    }


def _evolution_manifest(tmp_path: Path) -> Path:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "evolution.py"
    source.write_text(
        "class Owner:\n    def action(self):\n        return None\n\ndef recover(value):\n    return value or None\n",
        encoding="utf-8",
    )
    entries = [
        {
            "id": "e01",
            "source": str(source),
            "source_group": "e01",
            "rule": "EXP_JEV01_RESPONSIBILITIES",
            "label": "negative",
            "target": {"scope": "unit", "name": "recover", "kind": "function"},
        },
        {
            "id": "e03",
            "source": str(source),
            "source_group": "e03",
            "rule": "EXP_JEV03_CONCRETE_MECHANICS",
            "label": "negative",
            "target": {"scope": "unit", "name": "recover", "kind": "function"},
        },
        {
            "id": "e05",
            "source": str(source),
            "source_group": "e05",
            "rule": "EXP_JEV05_CORRECTED",
            "label": "negative",
            "target": {"scope": "unit", "name": "recover", "kind": "function"},
        },
        {
            "id": "e08",
            "source": str(source),
            "source_group": "e08",
            "rule": "EXP_JEV08_CHOICE",
            "label": "negative",
            "target": {"scope": "unit", "name": "Owner", "kind": "class"},
        },
        {
            "id": "partial",
            "source": str(source),
            "source_group": "partial",
            "rule": "EXP_PARTIAL_STATE_TRANSITION",
            "label": "negative",
            "target": {"scope": "unit", "name": "recover", "kind": "function"},
        },
    ]
    manifest = {
        "schema_version": 1,
        "corpus": "candidate-contracts",
        "provenance": "test-only source for candidate contracts",
        "source_root": str(source_root),
        "source_snapshot": {"algorithm": "sha256", "digest": source_snapshot(source_root)},
        "source_contract": {"labels_are_outside_source_root": True, "live_model_calls": "none"},
        "development_references": entries,
    }
    path = tmp_path / "MANIFEST.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def test_generated_config_advertises_all_evolution_candidates(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "generated.yaml")
    loaded = load_config([config_path.parent], explicit=config_path, cwd=PROJECT_ROOT).config
    evolution_rules = {
        "EXP_JEV01_RESPONSIBILITIES",
        "EXP_JEV01_INTERLEAVING",
        "EXP_JEV03_CONCRETE_MECHANICS",
        "EXP_JEV05_CORRECTED",
        "EXP_JEV08_CHOICE",
        "EXP_PARTIAL_STATE_TRANSITION",
        "EXP_JEV04_PAIR_GUARANTEE",
        "EXP_JEV04_PAIR_PRESERVATION",
    }
    assert evolution_rules <= loaded.selected_rules().keys()
    assert loaded.rulesets["experiment-candidates"].enabled
    for candidate in (
        "jev01-strict-and",
        "jev03-concrete-mechanics",
        "jev05-corrected",
        "jev08-choice",
        "unaccounted-partial-state-transition",
        "jev04-pair-corrected",
    ):
        assert set(candidate_rule_ids(candidate)) <= loaded.selected_rules().keys()
    pair_plan = plan_manifest(
        FOCUSED_MANIFEST,
        phase="development",
        config_path=config_path,
        candidates=["jev04-pair-corrected"],
    )
    assert {record["rule_id"] for record in pair_plan["cases"]} == {
        "EXP_JEV04_PAIR_GUARANTEE",
        "EXP_JEV04_PAIR_PRESERVATION",
    }


def test_generated_config_plans_and_replays_each_single_evolution_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _evolution_manifest(tmp_path)
    config_path = write_candidate_config(tmp_path / "generated.yaml")
    candidates = [
        "jev03-concrete-mechanics",
        "jev05-corrected",
        "jev08-choice",
        "unaccounted-partial-state-transition",
    ]
    for candidate in candidates:
        plan = plan_manifest(manifest, phase="development", config_path=config_path, candidates=[candidate])
        assert len(plan["cases"]) == 1
        assert len(plan["batches"]) == 1
        assert plan["batches"][0]["question_count"] == 1

    requests: list[bytes] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    output = tmp_path / "evolution-cases.jsonl"
    capture_manifest(
        manifest,
        phase="development",
        config_path=config_path,
        candidates=candidates,
        output=output,
        ledger_path=tmp_path / "ledger.json",
        transport=httpx.MockTransport(transport),
    )
    replayed = replay_metrics(output)
    assert all(replayed[candidate]["cases"] == 1 for candidate in candidates)
    request_material = b"\n".join(requests).lower()
    for label in (b"positive", b"negative", b"agree", b"disagree", b"ground truth"):
        assert label not in request_material


def _capture_mock_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidates: list[str],
    *,
    manifest: Path = FOCUSED_MANIFEST,
) -> tuple[Path, list[dict]]:
    config_path = write_candidate_config(tmp_path / "generated.yaml")

    async def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "choice" and "preserved" in question["criteria"]:
                labels = list(question["criteria"])
                answers[name] = {
                    "type": "choice",
                    "choice": "preserved",
                    "confidence": 0.8,
                    "probabilities": {label: 0.8 if label == "preserved" else 0.1 for label in labels},
                }
            else:
                answers[name] = _mock_answer(question)
        return httpx.Response(
            200,
            json={"model": MODEL, "usage": {"input_tokens": 10, "output_tokens": 3}, "answers": answers},
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    output = tmp_path / "mock-cases.jsonl"
    capture_manifest(
        manifest,
        phase="development",
        config_path=config_path,
        candidates=candidates,
        output=output,
        ledger_path=tmp_path / "mock-ledger.json",
        transport=httpx.MockTransport(transport),
    )
    return output, [case.model_dump(mode="json") for case in load_cases(output)]


def test_replay_rejects_leaked_or_incomplete_jev01_child_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, documents = _capture_mock_cases(tmp_path, monkeypatch, ["jev01-strict-and"])
    victim = next(document for document in documents if document["rule_id"] == "EXP_JEV01_INTERLEAVING")
    incomplete = tmp_path / "incomplete.jsonl"
    incomplete.write_text(
        "\n".join(json.dumps(document) for document in documents if document is not victim) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="incomplete child bundle"):
        replay_metrics(incomplete)

    leaked = [json.loads(json.dumps(document)) for document in documents]
    next(document for document in leaked if document["case_id"] == victim["case_id"])["provenance"]["candidate"] = (
        "other-candidate"
    )
    leaked_path = tmp_path / "leaked.jsonl"
    leaked_path.write_text("\n".join(json.dumps(document) for document in leaked) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete child bundle"):
        replay_metrics(leaked_path)

    mismatched = [json.loads(json.dumps(document)) for document in documents]
    next(document for document in mismatched if document["case_id"] == victim["case_id"])["label"] = "Agree"
    mismatch_path = tmp_path / "mismatch.jsonl"
    mismatch_path.write_text("\n".join(json.dumps(document) for document in mismatched) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="metadata mismatch"):
        replay_metrics(mismatch_path)

    missing_parent = [json.loads(json.dumps(document)) for document in documents]
    missing_parent[0]["provenance"].pop("parent_case_id")
    missing_parent_path = tmp_path / "missing-parent.jsonl"
    missing_parent_path.write_text(
        "\n".join(json.dumps(document) for document in missing_parent) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing parent_case_id"):
        replay_metrics(missing_parent_path)
    assert output.is_file()


def test_replay_rejects_unexpected_composed_child_and_excludes_generic_single_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _evolution_manifest(tmp_path)
    output, documents = _capture_mock_cases(
        tmp_path,
        monkeypatch,
        ["jev01-strict-and", "jev03-concrete-mechanics"],
        manifest=manifest,
    )
    leaked = [json.loads(json.dumps(document)) for document in documents]
    jev03 = next(document for document in leaked if document["rule_id"] == "EXP_JEV03_CONCRETE_MECHANICS")
    jev03["provenance"]["candidate"] = "jev01-strict-and"
    leaked_path = tmp_path / "unexpected-child.jsonl"
    leaked_path.write_text("\n".join(json.dumps(document) for document in leaked) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected child rule records"):
        replay_metrics(leaked_path)

    single_leaked = [json.loads(json.dumps(document)) for document in documents]
    jev01 = next(document for document in single_leaked if document["rule_id"] == "EXP_JEV01_RESPONSIBILITIES")
    jev01["provenance"]["candidate"] = "jev03-concrete-mechanics"
    single_leaked_path = tmp_path / "unexpected-single-child.jsonl"
    single_leaked_path.write_text(
        "\n".join(json.dumps(document) for document in single_leaked) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected child rule records"):
        replay_metrics(single_leaked_path)

    generic = [json.loads(json.dumps(document)) for document in documents]
    generic_jev03 = next(document for document in generic if document["rule_id"] == "EXP_JEV03_CONCRETE_MECHANICS")
    generic_jev03["provenance"].pop("candidate")
    generic_path = tmp_path / "generic-single.jsonl"
    generic_path.write_text("\n".join(json.dumps(document) for document in generic) + "\n", encoding="utf-8")
    assert replay_metrics(generic_path)["jev03-concrete-mechanics"]["cases"] == 0
    assert output.is_file()


def test_pair_metadata_requires_the_canonical_location_shape() -> None:
    span = {"start_byte": 0, "end_byte": 1, "start_line": 1, "end_line": 1}
    pair = {
        "pair_id": "pair-1",
        "source_path": "source.py",
        "earlier": {
            "operation_span": span,
            "callable_boundary": {"owner_kind": "function", "depth": 0},
        },
        "later": {
            "operation_span": {**span, "start_byte": 2, "end_byte": 3},
            "callable_boundary": {"owner_kind": "function", "depth": 0},
        },
        "intervening_span": {**span, "start_byte": 1, "end_byte": 2},
        "callable_boundary": {"crossed": False},
    }
    binding = {"candidate": "jev04-pair-corrected", "pair": pair}
    task_binding = {"candidate": binding["candidate"], **pair}
    record = SimpleNamespace(
        case=SimpleNamespace(
            case_id="pair-case",
            target=SimpleNamespace(path="source.py", start_byte=0, end_byte=3, start_line=1, end_line=1),
            provenance={"pair_binding": binding},
            question_wire={"instructions": {"task": _PAIR_TASK_MARKER + json.dumps(task_binding)}},
        )
    )
    assert _record_pair_metadata(record) == pair

    pair["unexpected"] = True
    task_binding["unexpected"] = True
    with pytest.raises(ValueError, match="canonical shape"):
        _record_pair_metadata(record)

    pair.pop("unexpected")
    task_binding.pop("unexpected")
    pair["source_path"] = "other.py"
    task_binding["source_path"] = "other.py"
    with pytest.raises(ValueError, match="bound to the target span"):
        _record_pair_metadata(record)


def test_corrected_pair_replay_keeps_baseline_fallback_assessment_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, documents = _capture_mock_cases(tmp_path, monkeypatch, ["jev04-baseline", "jev04-pair-corrected"])
    metrics = replay_metrics(output)["jev04-pair-corrected"]
    assert metrics["child_findings_promoted"] == 0
    assert metrics["combined"]["complete"] is True
    assert metrics["combined"]["eligible_pair_cases"] == 8
    assert metrics["combined"]["whole_target_fallback_cases"] == 4
    assert metrics["combined"]["metrics"]["severity"]["confirmed"] == {"warning": 4}
    assert metrics["combined"]["metrics"]["target_attribution"] == {
        "exact": 4,
        "signals": 4,
        "fraction": 1.0,
    }
    compatibility_documents = [json.loads(json.dumps(document)) for document in documents]
    baseline = next(document for document in compatibility_documents if document["rule_id"] == "FOCUS_JEV04_BASELINE")
    baseline["provenance"].pop("candidate")
    compatibility_path = tmp_path / "baseline-compatibility.jsonl"
    compatibility_path.write_text(
        "\n".join(json.dumps(document) for document in compatibility_documents) + "\n",
        encoding="utf-8",
    )
    assert replay_metrics(compatibility_path)["jev04-baseline"]["cases"] == 12


def test_corrected_pair_replay_rejects_pair_metadata_mismatch_and_incomplete_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, documents = _capture_mock_cases(tmp_path, monkeypatch, ["jev04-pair-corrected"])
    pair_documents = [document for document in documents if document["rule_id"].startswith("EXP_JEV04_PAIR_")]
    victim = pair_documents[0]
    incomplete = tmp_path / "pair-incomplete.jsonl"
    incomplete.write_text(
        "\n".join(json.dumps(document) for document in documents if document is not victim) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="incomplete child bundle"):
        replay_metrics(incomplete)

    mismatched = [json.loads(json.dumps(document)) for document in documents]
    changed = next(document for document in mismatched if document["case_id"] == victim["case_id"])
    changed["provenance"]["pair_binding"]["pair"]["pair_id"] += "-mismatch"
    mismatch_path = tmp_path / "pair-mismatch.jsonl"
    mismatch_path.write_text("\n".join(json.dumps(document) for document in mismatched) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="question and provenance pair IDs disagree"):
        replay_metrics(mismatch_path)
    assert output.is_file()


def test_capture_batches_mixed_questions_and_deduplicates_shared_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    freeze_path = _write_freeze(tmp_path / "freeze.json", ["jev02-baseline", "jev02-presence-gated"])
    requests: list[dict] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-persisted")
    output = tmp_path / "cases.jsonl"
    summary = capture_manifest(
        FOCUSED_MANIFEST,
        phase="heldout",
        config_path=config_path,
        freeze_path=freeze_path,
        output=output,
        ledger_path=tmp_path / "ledger.json",
        transport=httpx.MockTransport(transport),
    )
    cases = load_cases(output)
    metrics = replay_metrics(output)
    receipts = json.loads((tmp_path / "cases.receipts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert summary["model"] == MODEL
    assert len(cases) == 18
    assert len({case.case_id for case in cases}) == len(cases)
    assert all(case.provenance["parent_case_id"].startswith("focused-jev02-") for case in cases)
    assert all(case.context_complete and case.target_complete for case in cases)
    for case in cases:
        source_path = Path(case.provenance["source_path"])
        assert case.evidence.source_documents[source_path.name] == source_path.read_bytes().decode("utf-8")
    assert metrics["jev02-baseline"]["cases"] == 6
    assert metrics["usage"]["reported_input_tokens"] == 60
    assert metrics["usage"]["reported_output_tokens"] == 18
    assert metrics["usage"]["parent_opportunities"] == 6
    assert metrics["usage"]["purchased_questions"] == 12
    assert len(requests) == 6
    assert all(
        {question["type"] for question in payload["questions"].values()} == {"score", "noul"} for payload in requests
    )
    assert receipts["question_count"] == 2
    assert receipts["parent_opportunity_count"] == 1
    assert {case.provenance["candidate"] for case in cases} == {
        "jev02-baseline",
        "jev02-presence-gated",
    }
    assert "test-key-not-persisted" not in (tmp_path / "ledger.json").read_text(encoding="utf-8")


def test_pair_capture_filter_binds_complete_source_and_preserves_pair_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    requests: list[dict] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    output = tmp_path / "pair.jsonl"
    summary = capture_manifest(
        FOCUSED_MANIFEST,
        phase="development",
        config_path=config_path,
        candidates=["jev04-pair-joint"],
        output=output,
        ledger_path=tmp_path / "ledger.json",
        transport=httpx.MockTransport(transport),
    )
    cases = load_cases(output)

    assert summary["selected_candidates"] == ["jev04-pair-joint"]
    assert (
        json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))["invocations"][0]["parent_opportunities"]
        == 12
    )
    assert len(cases) == 8
    assert len(requests) == 8
    assert {case.provenance["candidate"] for case in cases} == {"jev04-pair-joint"}
    for case in cases:
        binding = case.provenance["pair_binding"]
        assert binding["status"] == "bound"
        assert case.provenance["validation_pair"] == binding["pair"]
        assert case.target.scope == "unit"
        assert case.target.path in case.evidence.source_documents
        assert any(
            document["path"] == case.target.path
            and document["start_byte"] <= case.target.start_byte
            and document["end_byte"] >= case.target.end_byte
            for document in case.evidence.state["documents"]
        )
        assert case.question_wire is not None
        task = case.question_wire["instructions"]["task"]
        task_metadata = json.loads(
            task.split("Pair binding metadata (machine-readable location metadata only):\n", 1)[1]
        )
        assert task_metadata["candidate"] == "jev04-pair-joint"
        assert task_metadata["pair_id"] == binding["pair_id"]
        assert "intervening_bytes" not in task
        assert "raw_predicate" not in task
    extractor_path = Path(summary["extractor_outcomes_path"])
    assert extractor_path.is_file()
    assert len(extractor_path.read_text(encoding="utf-8").splitlines()) == 12


def test_pair_metrics_combine_only_deployed_baseline_fallback_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")

    async def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    output = tmp_path / "combined.jsonl"
    capture_manifest(
        FOCUSED_MANIFEST,
        phase="development",
        config_path=config_path,
        candidates=["jev04-baseline", "jev04-pair-joint"],
        output=output,
        ledger_path=tmp_path / "ledger.json",
        transport=httpx.MockTransport(transport),
    )
    combined = replay_metrics(output)["jev04-pair-joint"]["combined"]

    assert combined["eligible_pair_cases"] == 8
    assert combined["whole_target_fallback_cases"] == 4
    assert combined["fallback_records_available"] is True
    assert combined["missing_fallback_cases"] == []
    assert combined["complete"] is True
    assert combined["metrics"]["cases"] == 12


def test_pair_preservation_metrics_use_deployed_whole_target_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")

    async def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    output = tmp_path / "preservation-combined.jsonl"
    capture_manifest(
        FOCUSED_MANIFEST,
        phase="development",
        config_path=config_path,
        candidates=["jev04-baseline", "jev04-pair-preservation"],
        output=output,
        ledger_path=tmp_path / "ledger.json",
        transport=httpx.MockTransport(transport),
    )
    candidate_metrics = replay_metrics(output)["jev04-pair-preservation"]
    combined = candidate_metrics["combined"]

    assert candidate_metrics["composition"].startswith("pair-bound focused preservation signal")
    assert candidate_metrics["eligible"]["cases"] == 8
    assert combined["eligible_pair_cases"] == 8
    assert combined["whole_target_fallback_cases"] == 4
    assert combined["fallback_records_available"] is True
    assert combined["missing_fallback_cases"] == []
    assert combined["complete"] is True
    assert combined["metrics"]["cases"] == 12


def test_capture_preserves_missing_usage_and_retries_in_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    freeze_path = _write_freeze(tmp_path / "freeze.json", ["jev01-baseline"])
    attempts = 0

    async def transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    ledger_path = tmp_path / "ledger.json"
    output = tmp_path / "cases.jsonl"
    capture_manifest(
        FOCUSED_MANIFEST,
        phase="heldout",
        config_path=config_path,
        freeze_path=freeze_path,
        output=output,
        ledger_path=ledger_path,
        transport=httpx.MockTransport(transport),
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    first_receipt = ledger["invocations"][0]["receipts"][0]
    assert attempts == 7
    assert first_receipt["attempts"] == 2
    assert first_receipt["retry_attempts"] == 1
    assert first_receipt["reported_input_tokens"] is None
    assert first_receipt["reported_output_tokens"] is None
    assert first_receipt["attempts_reserved_input_tokens"] > first_receipt["request_body_reserved_input_tokens"]
    assert all(case.provenance["usage"]["reported_input_tokens"] is None for case in load_cases(output))


def test_capture_batches_choice_and_noul_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    freeze_path = _write_freeze(tmp_path / "freeze.json", ["jev04-baseline", "jev04-focused-decomposed"])
    requests: list[dict] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    output = tmp_path / "cases.jsonl"
    capture_manifest(
        FOCUSED_MANIFEST,
        phase="heldout",
        config_path=config_path,
        freeze_path=freeze_path,
        output=output,
        ledger_path=tmp_path / "ledger.json",
        transport=httpx.MockTransport(transport),
    )
    cases = load_cases(output)
    assert len(cases) == 18
    assert len(requests) == 6
    assert all(
        {question["type"] for question in payload["questions"].values()} == {"choice", "noul"} for payload in requests
    )
    assert {case.provenance["candidate"] for case in cases} == {
        "jev04-baseline",
        "jev04-focused-decomposed",
    }


def test_capture_rejects_wrong_model_and_cumulative_ledger_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    freeze_path = _write_freeze(tmp_path / "freeze.json", ["jev01-baseline"])
    calls = 0

    async def wrong_model(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-latest",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    ledger_path = tmp_path / "ledger.json"
    with pytest.raises(RuntimeError, match="pinned"):
        capture_manifest(
            FOCUSED_MANIFEST,
            phase="heldout",
            config_path=config_path,
            freeze_path=freeze_path,
            output=tmp_path / "wrong.jsonl",
            ledger_path=ledger_path,
            transport=httpx.MockTransport(wrong_model),
        )
    assert calls == 1
    failed = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert failed["invocations"][0]["status"] == "failed"
    failed["reserved_cost"] = 0.1
    ledger_path.write_text(json.dumps(failed), encoding="utf-8")
    with pytest.raises(RuntimeError, match="cumulative cap"):
        capture_manifest(
            FOCUSED_MANIFEST,
            phase="heldout",
            config_path=config_path,
            freeze_path=freeze_path,
            output=tmp_path / "capped.jsonl",
            ledger_path=ledger_path,
            transport=httpx.MockTransport(wrong_model),
        )


def test_capture_persists_completed_cases_before_mid_capture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    freeze_path = _write_freeze(tmp_path / "freeze.json", ["jev01-baseline"])
    calls = 0

    async def fail_after_one(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls > 1:
            return httpx.Response(
                200,
                json={
                    "model": "jev-latest",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
                },
            )
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "answers": {name: _mock_answer(question) for name, question in payload["questions"].items()},
            },
        )

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    output = tmp_path / "partial.jsonl"
    ledger_path = tmp_path / "ledger.json"
    with pytest.raises(RuntimeError, match="pinned"):
        capture_manifest(
            FOCUSED_MANIFEST,
            phase="heldout",
            config_path=config_path,
            freeze_path=freeze_path,
            output=output,
            ledger_path=ledger_path,
            transport=httpx.MockTransport(fail_after_one),
        )
    assert calls == 2
    assert len(load_cases(output)) == 1
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["invocations"][0]["status"] == "failed"
    assert ledger["invocations"][0]["cases"] == 1


def test_missing_finding_is_not_exact_target_attribution() -> None:
    target = object()
    record = SimpleNamespace(
        signal=True,
        case=SimpleNamespace(
            case_id="missing",
            rule_id="JEV01",
            label="Agree",
            target=target,
            provenance={"source_group": "g"},
        ),
        assessment=SimpleNamespace(finding=None, tentative_finding=None),
    )
    metrics = _metrics_for_records([record])
    assert metrics["target_attribution"] == {"exact": 0, "signals": 1, "fraction": 0.0}


def test_declared_source_hash_drift_is_rejected_before_planning(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["source_root"] = str(SOURCE_ROOT)
    manifest["source_snapshot"]["digest"] = source_snapshot(SOURCE_ROOT)
    manifest.pop("development_import", None)
    entry = dict(manifest["development_references"][0])
    entry["source"] = str(PROJECT_ROOT / entry["source"])
    entry["source_sha256"] = "sha256:" + "0" * 64
    manifest["development_references"] = [entry]
    manifest_path = tmp_path / "private.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    with pytest.raises(ValueError, match="source_sha256"):
        plan_manifest(manifest_path, phase="development", config_path=config_path)
