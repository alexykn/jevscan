from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
CALIBRATION = ROOT / "calibration" / "new-rule-finalists"
USEFULNESS = CALIBRATION / "usefulness-results.json"
COST = CALIBRATION / "cost-ledger.json"

RULES = [
    "EXPOSED_MUTABLE_AUTHORITY",
    "HIDDEN_CALLER_RELEVANT_EFFECT",
    "HIDDEN_CALLER_RELEVANT_PREREQUISITE",
    "NEW01_CONSUMER_CONFIRMED",
    "NEW09_UNSAFE_RETRY",
    "NEW11_FALSE_SUCCESS",
    "NEW04_AMBIENT_EFFECT_A",
    "NEW05_DERIVED_INCOHERENCE",
]


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        found.extend(value)
        for child in value.values():
            found.extend(_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_keys(child))
    return found


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def test_usefulness_schema_and_dispositions_are_complete() -> None:
    result = _load(USEFULNESS)
    assert result["schema_version"] == 1
    assert result["status"] == "complete_public_aggregate"
    assert result["scope"]["rules"] == RULES
    assert result["scope"]["anonymous_repository_slots"] == 5
    assert result["scope"]["files"] == {
        "per_slot": 3,
        "total": 15,
        "roles": ["candidate-rich", "control", "test"],
    }
    assert result["scope"]["production_scan"] == {
        "complete": True,
        "judgments": 1264,
        "requests": 41,
        "reportable_signals_reviewed": 4,
        "stratified_nonfindings_reviewed": 70,
        "reviewed_cases": 74,
        "review_blindly": True,
        "all_rule_and_slot_coverage": True,
    }
    assert result["method"]["retuning"] is False
    assert result["method"]["blind_source_review"] is True
    assert result["method"]["reportable_signals_reviewed_blindly"] is True
    assert result["method"]["stratified_nonfindings_reviewed_blindly"] is True

    rule_results = result["rule_results"]
    assert [item["rule"] for item in rule_results] == RULES
    assert sum(item["reviewed_cases"] for item in rule_results) == 74
    assert sum(item["signals"]["total"] for item in rule_results) == 4
    assert all(sum(item["labels"].values()) == item["reviewed_cases"] for item in rule_results)

    by_rule = {item["rule"]: item for item in rule_results}
    assert by_rule["EXPOSED_MUTABLE_AUTHORITY"]["disposition"] == "defer"
    assert by_rule["NEW01_CONSUMER_CONFIRMED"]["disposition"] == "defer"
    assert by_rule["NEW04_AMBIENT_EFFECT_A"]["disposition"] == "reject_this_release"
    assert by_rule["NEW04_AMBIENT_EFFECT_A"]["signals"]["all_confirmed_are_disagree"] is True
    assert set(result["decisions"]["promote_low_noise"]) == {
        "HIDDEN_CALLER_RELEVANT_EFFECT",
        "HIDDEN_CALLER_RELEVANT_PREREQUISITE",
        "NEW05_DERIVED_INCOHERENCE",
        "NEW09_UNSAFE_RETRY",
        "NEW11_FALSE_SUCCESS",
    }
    assert result["decisions"]["promoted_candidates_passed_development_selector"] is True
    assert result["decisions"]["promoted_candidates_passed_frozen_heldout"] is True
    assert result["guidance"]["reporting"] == "non_blocking_review_guidance"
    assert result["guidance"]["independent_disable"] is True
    assert result["guidance"]["independent_rollback"] is True
    assert result["guidance"]["production_defaults_changed"] is True
    assert result["guidance"]["real_positive_recall"] == "unverified_for_all_promoted_candidates"


def test_cumulative_cost_arithmetic_includes_failed_attempt_without_usage() -> None:
    ledger = _load(COST)
    phases = ledger["phases"]
    cumulative = ledger["cumulative"]
    budget = ledger["budget"]
    assert [phase["phase"] for phase in phases] == [
        "prior_baseline",
        "finalist_development",
        "support_extension",
        "heldout",
        "incomplete_usefulness_excluded",
        "rejected_exact_excluded",
        "complete_usefulness",
    ]

    assert sum(phase["requests"] for phase in phases) == cumulative["accepted_requests"]
    assert sum(phase["attempts"] for phase in phases) == cumulative["total_attempts"]
    for field in (
        "reported_input_tokens",
        "reported_output_tokens",
        "reserved_input_tokens",
        "reported_cost_usd",
        "reserved_cost_usd",
    ):
        assert sum((_decimal(phase[field]) for phase in phases), Decimal(0)) == _decimal(cumulative[field])

    rejected = phases[5]
    assert rejected["attempts"] == 1
    assert rejected["requests"] == 0
    assert rejected["failure"] == "HTTP400"
    assert rejected["durable_rows"] == 0
    assert rejected["usage_records"] == 0
    assert rejected["evidence_included"] is False

    assert _decimal(budget["recorded_reserved_cost_usd"]) == _decimal(cumulative["reserved_cost_usd"])
    assert _decimal(budget["recorded_reserved_cost_usd"]) + _decimal(
        budget["failed_attempt_authorized_cap_usd"]
    ) == _decimal(budget["conservative_committed_authorized_cost_usd"])
    assert _decimal(budget["task_budget_usd"]) - _decimal(
        budget["conservative_committed_authorized_cost_usd"]
    ) == _decimal(budget["remaining_against_task_budget_usd"])


def test_public_artifacts_have_no_private_fields_or_local_identifiers() -> None:
    artifacts = [USEFULNESS, CALIBRATION / "usefulness-results.md", COST]
    forbidden_keys = {
        "repository_identity",
        "repository_path",
        "target_identity",
        "target_path",
        "source_text",
        "source_path",
        "private_hash",
        "local_output_name",
    }
    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8")
        assert f"{chr(47)}Users{chr(47)}" not in text
        assert f"{chr(47)}home{chr(47)}" not in text
        assert f".{chr(100)}elta{chr(47)}" not in text
        assert "sha256" not in text.lower()
        if artifact.suffix == ".json":
            assert not (forbidden_keys & set(_keys(_load(artifact))))
