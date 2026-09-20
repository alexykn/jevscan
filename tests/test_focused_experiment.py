"""Offline contract tests for the focused-question experiment scaffold."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from calibration.focused_experiment import (
    FOCUSED_MANIFEST,
    candidate_documents,
    freeze_candidates,
    load_manifest,
    plan_manifest,
    source_snapshot,
    write_candidate_config,
)
from jevscan.core.config import load_config
from jevscan.core.rules import ChoiceQuestion, NoulQuestion, ScoreQuestion

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
        assert sum(groups[(rule, group)][0]["label"] == "positive" for group in rule_groups) == 3
        assert sum(groups[(rule, group)][0]["label"] == "negative" for group in rule_groups) == 3
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
    assert documents["FOCUS_JEV04_JOINT"]["question"]["type"] == "choice"
    assert documents["FOCUS_JEV04_DECOMPOSED_GUARANTEE"]["question"]["type"] == "noul"
    assert documents["FOCUS_JEV04_DECOMPOSED_PRESERVATION"]["question"]["type"] == "noul"
    assert isinstance(packaged["JEV01"].question, NoulQuestion)
    assert isinstance(packaged["JEV02"].question, ScoreQuestion)
    assert isinstance(packaged["JEV04"].question, ChoiceQuestion)


def test_production_planner_binds_exact_targets_and_batches_same_evidence(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    with pytest.raises(ValueError, match="frozen"):
        plan_manifest(CORPUS / "MANIFEST.yaml", phase="heldout", config_path=config_path, freeze_path=None)


def test_production_planner_binds_exact_targets_and_batches_same_evidence_after_freeze(tmp_path: Path) -> None:
    config_path = write_candidate_config(tmp_path / "focused.yaml")
    development = plan_manifest(CORPUS / "MANIFEST.yaml", phase="development", config_path=config_path)
    development_path = tmp_path / "development.json"
    development_path.write_text(json.dumps(development), encoding="utf-8")
    freeze_path = tmp_path / "freeze.json"
    freeze_candidates(CORPUS / "MANIFEST.yaml", development_path, ["jev04-focused-joint"], freeze_path)
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
    assert plan["reserved_cost"] * (1 + plan["localization_overhead"]) <= plan["cumulative_cap"]
    assert any(batch["question_count"] > 1 for batch in plan["batches"])


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
