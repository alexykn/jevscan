import json
import stat
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest
from test_semantic_calibration import case_document

from jevscan.cli.calibrate import main
from jevscan.core.calibration_selection import SelectionObjective, candidate_policies, policy_hash, select_policies
from jevscan.core.models import Kind, Target
from jevscan.core.protocol import QUESTION_POLICY_V4, encode, prompt_binder
from jevscan.core.rules import Rule
from jevscan.core.semantic_calibration import CalibrationCase, load_cases, replay_cases


def _cases(
    labels: list[str],
    *,
    split: str = "development",
    warning: int = 2,
    error: int = 3,
    scores: list[float] | None = None,
    source_group: str = "development-source",
    requested_model: str = "requested-model",
    returned_model: str = "concrete-model",
    prompt_version: int | None = None,
    prompt_policy: str | None = None,
):
    documents = []
    for index, label in enumerate(labels):
        document = case_document(
            f"selection-{index}",
            label=label,
            warning={"min_score": warning},
            error={"min_score": error},
            requested_model=requested_model,
            returned_model=returned_model,
            **({"prompt_version": prompt_version} if prompt_version is not None else {}),
            **({"prompt_policy": prompt_policy} if prompt_policy is not None else {}),
        )
        document["rule_id"] = "custom-rule"
        document["split"] = split
        document["provenance"]["source_group"] = source_group
        document["answer"]["score"] = float(scores[index] if scores is not None else warning)
        documents.append(document)
    return load_cases(StringIO("\n".join(json.dumps(document) for document in documents)))


def _score_rule(warning: int = 2, error: int = 3) -> Rule:
    return Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "score", "instructions": "How tangled?", "criteria": ["0", "1", "2", "3"]},
        "report": {
            "message": "Review",
            "levels": {"warning": {"min_score": warning}, "error": {"min_score": error}},
        },
    })


def _with_case_id(case: CalibrationCase, case_id: str) -> CalibrationCase:
    return case.model_copy(update={"case_id": case_id})


def _typed_case(
    case_id: str,
    rule_id: str,
    question: dict,
    report: dict,
    answer: dict,
    label: str,
) -> dict:
    document = case_document(case_id, label=label)
    document["rule_id"] = rule_id
    document["split"] = "development"
    document["provenance"]["source_group"] = "typed-source"
    document["rule"] = {"applies_to": ["function"], "question": question, "report": report}
    document["answer"] = answer
    rule = Rule.model_validate(document["rule"])
    target_doc = document["target"]
    target = Target(
        target_doc["id"],
        target_doc["scope"],
        target_doc["path"],
        target_doc["language"],
        target_doc["qualified_name"],
        target_doc["start_byte"],
        target_doc["end_byte"],
        target_doc["start_line"],
        target_doc["end_line"],
        Kind(target_doc["kind"]),
        target_doc.get("display_name", ""),
    )
    document["rule"] = rule.model_dump(mode="json")
    bound = prompt_binder(document["prompt"]["version"], document["prompt"]["policy"]).bind(
        rule.question, target, document["prompt"]["policy"]
    )
    document["hashes"]["question"] = f"sha256:{sha256(encode(bound)).hexdigest()}"
    document["hashes"]["rule"] = f"sha256:{sha256(encode(rule.model_dump(mode='json'))).hexdigest()}"
    document["hashes"]["report"] = f"sha256:{sha256(encode(rule.report.model_dump(mode='json'))).hexdigest()}"
    document["comparability"]["question"] = document["hashes"]["question"]
    return document


def test_selection_rejects_mixed_returned_models_across_distinct_cases() -> None:
    first = _cases(["Agree"], requested_model="jev-latest", returned_model="jev-1.13.0")[0]
    second = _with_case_id(
        _cases(["Disagree"], requested_model="jev-latest", returned_model="jev-1.14.0")[0],
        "selection-1",
    )

    audit = select_policies([first, second])
    result = audit.rules["custom-rule"]

    assert result.selected_policy is None
    assert result.reason == "development_model_mismatch"
    assert audit.compatibility.as_dict()["model_identity"] == "returned_model"
    assert audit.compatibility.as_dict()["development"]["status"] == "mismatch"
    assert result.compatibility is not None
    assert {item["field"] for item in result.compatibility.mismatches} == {"returned_model"}


def test_selection_rejects_prompt_version_and_policy_mismatch() -> None:
    first = _cases(["Agree"], returned_model="jev-1.13.0")[0]
    second = _with_case_id(
        _cases(
            ["Disagree"],
            returned_model="jev-1.13.0",
            prompt_version=4,
            prompt_policy=QUESTION_POLICY_V4,
        )[0],
        "selection-1",
    )

    result = select_policies([first, second]).rules["custom-rule"]

    assert result.selected_policy is None
    assert result.reason == "development_prompt_mismatch"
    assert result.compatibility is not None
    assert {item["field"] for item in result.compatibility.mismatches} == {
        "prompt.version",
        "prompt.policy",
    }


def test_selection_pins_returned_model_and_allows_requested_aliases_to_differ() -> None:
    first = _cases(["Agree"], requested_model="jev-latest", returned_model="jev-1.13.0")[0]
    second = _with_case_id(
        _cases(["Disagree"], requested_model="jev-1.13.0", returned_model="jev-1.13.0")[0],
        "selection-1",
    )

    audit = select_policies([first, second])

    assert audit.rules["custom-rule"].selected_policy is not None
    assert audit.compatibility.development.status == "compatible"
    assert audit.compatibility.development.authority is not None
    assert audit.compatibility.development.authority.returned_model == "jev-1.13.0"


def test_incompatible_model_opt_in_is_explicitly_audited_and_replay_remains_permissive() -> None:
    first = _cases(["Agree"], returned_model="jev-1.13.0")[0]
    second = _with_case_id(_cases(["Disagree"], returned_model="jev-1.14.0")[0], "selection-1")

    replay = replay_cases([first, second])
    audit = select_policies([first, second], allow_incompatible_model_prompt=True)

    assert all(record.comparable for record in replay.records)
    assert not replay.non_comparable
    assert audit.rules["custom-rule"].selected_policy is not None
    assert audit.compatibility.allow_incompatible_model_prompt
    assert audit.compatibility.development.status == "mismatch"
    assert audit.as_dict()["compatibility"]["mode"] == "allow_incompatible_model_prompt"


def test_cli_exposes_incompatible_model_prompt_opt_in(tmp_path: Path) -> None:
    first = _cases(["Agree"], returned_model="jev-1.13.0")[0]
    second = _with_case_id(_cases(["Disagree"], returned_model="jev-1.14.0")[0], "selection-1")
    cases_path = tmp_path / "cases.jsonl"
    audit_path = tmp_path / "audit.json"
    cases_path.write_text("\n".join(json.dumps(case.model_dump(mode="json")) for case in (first, second)) + "\n")

    assert (
        main([
            str(cases_path),
            "--select",
            "--allow-incompatible-model-prompt",
            "--output",
            str(audit_path),
        ])
        == 0
    )
    assert json.loads(audit_path.read_text())["compatibility"]["mode"] == "allow_incompatible_model_prompt"


def test_heldout_must_match_frozen_development_compatibility_authority() -> None:
    development = _cases(["Agree", "Disagree"], returned_model="jev-1.13.0")
    heldout = _with_case_id(
        _cases(["Agree"], split="heldout", source_group="heldout-source", returned_model="jev-1.14.0")[0],
        "heldout-1",
    )

    result = select_policies(development + [heldout], heldout_split="heldout").rules["custom-rule"]

    assert result.selected_policy is not None
    assert result.heldout is not None
    assert result.heldout["compatibility"]["status"] == "mismatch"
    assert result.heldout["compatibility"]["rejected_case_ids"] == ["heldout-1"]
    assert result.heldout["rules"] == {}


def test_optional_review_list_recall_constraint_is_group_normalized_and_audited() -> None:
    retained = _cases(["Agree"], scores=[3], source_group="positive-a")[0]
    missed = _with_case_id(_cases(["Agree"], scores=[1], source_group="positive-b")[0], "selection-1")
    negative = _with_case_id(_cases(["Disagree"], scores=[1], source_group="negative")[0], "selection-2")

    audit = select_policies(
        [retained, missed, negative],
        objective={"min_review_list_recall": 1.0, "max_candidates": 1},
    )
    result = audit.rules["custom-rule"]

    assert result.reason == "no_candidate_meets_review_list_recall"
    assert result.selected_policy is not None
    assert result.selected_development is not None
    assert result.selected_development.support["review_list_recall"] == pytest.approx(0.5)
    assert audit.as_dict()["objective"]["min_review_list_recall"] == 1.0


@pytest.mark.parametrize("value", [-0.1, 1.1, True, "1"])
def test_review_list_recall_constraint_requires_a_finite_fraction(value) -> None:
    with pytest.raises((TypeError, ValueError), match="min_review_list_recall"):
        SelectionObjective.from_mapping({"min_review_list_recall": value})


def test_selection_freezes_development_winner_before_heldout() -> None:
    cases = _cases(["Agree", "Disagree", "Disagree"], scores=[3, 2, 2])
    heldout = _cases(["Agree"], split="heldout", source_group="heldout-source")
    audit = select_policies(cases + heldout, heldout_split="heldout")
    result = audit.rules["custom-rule"]

    assert result.reason == "selected"
    assert result.selected_policy is not None
    assert result.selected_policy.levels.warning.min_score == 3
    assert result.heldout is not None
    assert result.heldout["split"] == "heldout"
    assert result.selected_policy.levels.warning.min_score == 3


def test_joint_score_threshold_move_is_in_search_domain() -> None:
    cases = _cases(["Agree", "Disagree"], warning=1, error=2, scores=[3, 2])
    result = select_policies(cases).rules["custom-rule"]

    assert result.selected_policy is not None
    assert result.selected_policy.levels.error.min_score == 2
    assert result.selected_policy.levels.warning.min_score is not None
    assert result.selected_policy.levels.warning.min_score <= 2


def test_joint_probability_threshold_move_is_in_search_domain() -> None:
    question = {"type": "choice", "instructions": "Which?", "criteria": {"bad": "Bad", "good": "Good"}}
    report = {
        "message": "Review",
        "choices": ["bad"],
        "levels": {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.8}},
    }
    documents = [
        _typed_case(
            "prob-agree",
            "prob-rule",
            question,
            report,
            {"type": "choice", "choice": "bad", "confidence": 0.9, "probabilities": {"bad": 1.0, "good": 0.0}},
            "Agree",
        ),
        _typed_case(
            "prob-disagree",
            "prob-rule",
            question,
            report,
            {"type": "choice", "choice": "bad", "confidence": 0.9, "probabilities": {"bad": 0.9, "good": 0.1}},
            "Disagree",
        ),
    ]
    cases = load_cases(StringIO("\n".join(json.dumps(document) for document in documents)))
    result = select_policies(cases).rules["prob-rule"]

    assert result.selected_policy is not None
    assert result.selected_policy.levels.error.min_probability == 0.8
    assert result.selected_policy.levels.warning.min_probability is not None
    assert result.selected_policy.levels.warning.min_probability <= 0.8


def test_one_sided_labels_retain_baseline_with_reason() -> None:
    cases = _cases(["Agree", "Agree"])
    audit = select_policies(cases)
    result = audit.rules["custom-rule"]

    assert result.reason == "insufficient_labeled_support"
    assert result.selected_policy is not None
    assert policy_hash(result.selected_policy) == policy_hash(_score_rule().report)


def test_default_objective_makes_tradeoffs_explicit() -> None:
    utility = SelectionObjective.default().utility

    assert utility["Agree"]["confirmed"] > utility["Agree"]["tentative"] > utility["Agree"]["none"]
    assert utility["Disagree"]["confirmed"] < utility["Disagree"]["tentative"] < utility["Disagree"]["none"]
    assert utility["Partial"] != utility["Agree"]
    assert utility["Partial"]["confirmed"] == -1
    assert utility["Partial"]["tentative"] == 1


def test_partial_hard_warning_is_not_a_default_threshold_gain() -> None:
    question = {"type": "score", "instructions": "How tangled?", "criteria": ["0", "1", "2", "3"]}
    report = {
        "message": "Review",
        "levels": {
            "warning": {"min_score": 2, "min_confidence": 0.5},
            "error": {"min_score": 3, "min_confidence": 0.8},
        },
    }
    documents = [
        _typed_case(
            "partial-gain-agree",
            "partial-gain",
            question,
            report,
            {
                "type": "score",
                "score": 2.0,
                "confidence": 0.4,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0},
            },
            "Agree",
        ),
        _typed_case(
            "partial-gain-partial",
            "partial-gain",
            question,
            report,
            {
                "type": "score",
                "score": 1.0,
                "confidence": 0.9,
                "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0},
            },
            "Partial",
        ),
        _typed_case(
            "partial-gain-disagree",
            "partial-gain",
            question,
            report,
            {
                "type": "score",
                "score": 0.0,
                "confidence": 0.9,
                "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0},
            },
            "Disagree",
        ),
    ]
    cases = load_cases(StringIO("\n".join(json.dumps(document) for document in documents)))
    result = select_policies(cases).rules["partial-gain"]

    assert result.selected_policy is not None
    assert result.selected_policy.levels.warning.min_score == 2
    assert result.selected_policy.levels.error.min_score == 3


def test_candidate_generation_supports_all_question_types_and_score_directions() -> None:
    noul = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "noul", "instructions": "Is it bad?"},
        "report": {
            "message": "Review",
            "levels": {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.8}},
        },
    })
    choice = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "choice", "instructions": "Which?", "criteria": {"bad": "Bad", "good": "Good"}},
        "report": {
            "message": "Review",
            "choices": ["bad"],
            "levels": {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.8}},
        },
    })
    reverse_score = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "score", "instructions": "How clear?", "criteria": ["0", "1", "2", "3"]},
        "report": {
            "message": "Review",
            "levels": {"warning": {"max_score": 1}, "error": {"max_score": 0}},
        },
    })
    error_confidence = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "score", "instructions": "How sure?", "criteria": ["0", "1", "2", "3"]},
        "report": {
            "message": "Review",
            "levels": {
                "warning": {"min_score": 1},
                "error": {"min_score": 2, "min_confidence": 0.8},
            },
        },
    })

    assert policy_hash(noul.report) in {policy_hash(policy) for policy in candidate_policies(noul, [])}
    assert policy_hash(choice.report) in {policy_hash(policy) for policy in candidate_policies(choice, [])}
    reverse_candidates = candidate_policies(reverse_score, [])
    assert policy_hash(reverse_score.report) in {policy_hash(policy) for policy in reverse_candidates}
    assert all(policy.levels.warning.min_score is None for policy in reverse_candidates)
    assert all(policy.levels.warning.max_score is not None for policy in reverse_candidates)
    confidence_candidates = candidate_policies(error_confidence, [])
    assert len(confidence_candidates) > 1
    assert all(policy.levels.warning.min_confidence is None for policy in confidence_candidates)
    mass = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "score", "instructions": "How tangled?", "criteria": ["0", "1", "2", "3"]},
        "report": {
            "message": "Review",
            "levels": {
                "warning": {"score_levels": [0, 1, 2, 3], "min_probability": 0.5},
                "error": {"score_levels": [1, 3], "min_probability": 0.8},
            },
        },
    })
    mass_candidates = candidate_policies(mass, [], max_candidates=1)
    assert len(mass_candidates) == 1
    assert policy_hash(mass_candidates[0]) == policy_hash(mass.report)


def test_choice_search_balances_probability_and_confidence_axes() -> None:
    choice = Rule.model_validate({
        "applies_to": ["function"],
        "question": {"type": "choice", "instructions": "Which?", "criteria": {"bad": "Bad", "good": "Good"}},
        "report": {
            "message": "Review",
            "choices": ["bad"],
            "levels": {
                "warning": {"min_probability": 0.5, "min_confidence": 0.5},
                "error": {"min_probability": 0.8, "min_confidence": 0.8},
            },
        },
    })
    candidates = candidate_policies(choice, [], max_candidates=40)
    probability_values = {
        (policy.levels.warning.min_probability, policy.levels.error.min_probability) for policy in candidates
    }
    confidence_values = {
        (policy.levels.warning.min_confidence, policy.levels.error.min_confidence) for policy in candidates
    }

    assert any(pair != (0.5, 0.8) for pair in probability_values)
    assert any(pair != (0.5, 0.8) for pair in confidence_values)
    assert any(warning == 0.8 for warning, _ in probability_values)
    assert any(warning == 0.8 for warning, _ in confidence_values)


def test_choice_search_changes_each_threshold_dimension_under_cap() -> None:
    question = {"type": "choice", "instructions": "Which?", "criteria": {"bad": "Bad", "good": "Good"}}
    report = {
        "message": "Review",
        "choices": ["bad"],
        "levels": {
            "warning": {"min_probability": 0.5, "min_confidence": 0.5},
            "error": {"min_probability": 0.8, "min_confidence": 0.8},
        },
    }
    documents = [
        _typed_case(
            f"choice-{index}",
            "choice-rule",
            question,
            report,
            {
                "type": "choice",
                "choice": "bad" if index % 2 == 0 else "good",
                "confidence": index / 100,
                "probabilities": {
                    "bad": index / 100,
                    "good": 1 - index / 100,
                },
            },
            "Agree" if index % 2 == 0 else "Disagree",
        )
        for index in range(1, 101)
    ]
    cases = load_cases(StringIO("\n".join(json.dumps(document) for document in documents)))
    candidates = candidate_policies(cases[0].rule, cases, max_candidates=64)
    warning_probability = {policy.levels.warning.min_probability for policy in candidates}
    warning_confidence = {policy.levels.warning.min_confidence for policy in candidates}

    assert len(warning_probability) > 1
    assert len(warning_confidence) > 1


def test_conflicting_development_baselines_are_order_independent() -> None:
    first = _cases(["Agree"], warning=1, error=2)[0]
    second = _cases(["Disagree"], warning=2, error=3)[0]
    forward = select_policies([first, second]).rules["custom-rule"]
    reverse = select_policies([second, first]).rules["custom-rule"]

    assert forward.selected_policy is None
    assert reverse.selected_policy is None
    assert forward.reason == reverse.reason == "development_baseline_policy_mismatch"
    assert forward.baseline_policy is not None
    assert reverse.baseline_policy is not None
    assert policy_hash(forward.baseline_policy) == policy_hash(reverse.baseline_policy)


def test_selection_replays_labeled_noul_choice_and_score_answers() -> None:
    noul_question = {"type": "noul", "instructions": "Is it unsafe?"}
    noul_report = {
        "message": "Review",
        "levels": {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.8}},
    }
    choice_question = {
        "type": "choice",
        "instructions": "Which?",
        "criteria": {"bad": "Bad", "good": "Good", "unknown": "Unknown"},
    }
    choice_report = {
        "message": "Review",
        "choices": ["bad"],
        "uncertain_choices": ["unknown"],
        "levels": {"warning": {"min_probability": 0.5}, "error": {"min_probability": 0.8}},
    }
    score_question = {"type": "score", "instructions": "How tangled?", "criteria": ["0", "1", "2", "3"]}
    score_report = {
        "message": "Review",
        "levels": {
            "warning": {"score_levels": [2, 3], "min_probability": 0.5},
            "error": {"score_levels": [3], "min_probability": 0.8},
        },
    }
    documents = [
        _typed_case("noul-agree", "noul-rule", noul_question, noul_report, {"type": "noul", "noul": 0.9}, "Agree"),
        _typed_case(
            "noul-disagree", "noul-rule", noul_question, noul_report, {"type": "noul", "noul": 0.1}, "Disagree"
        ),
        _typed_case(
            "choice-agree",
            "choice-rule",
            choice_question,
            choice_report,
            {
                "type": "choice",
                "choice": "bad",
                "confidence": 0.9,
                "probabilities": {"bad": 0.9, "good": 0.05, "unknown": 0.05},
            },
            "Agree",
        ),
        _typed_case(
            "choice-disagree",
            "choice-rule",
            choice_question,
            choice_report,
            {
                "type": "choice",
                "choice": "good",
                "confidence": 0.9,
                "probabilities": {"bad": 0.05, "good": 0.9, "unknown": 0.05},
            },
            "Disagree",
        ),
        _typed_case(
            "score-agree",
            "score-rule",
            score_question,
            score_report,
            {
                "type": "score",
                "score": 3.0,
                "confidence": 0.9,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.9},
            },
            "Agree",
        ),
        _typed_case(
            "score-disagree",
            "score-rule",
            score_question,
            score_report,
            {
                "type": "score",
                "score": 1.0,
                "confidence": 0.9,
                "probabilities": {"0": 0.4, "1": 0.4, "2": 0.1, "3": 0.1},
            },
            "Disagree",
        ),
    ]
    cases = load_cases(StringIO("\n".join(json.dumps(document) for document in documents)))
    audit = select_policies(cases)

    for rule_id in ("noul-rule", "choice-rule", "score-rule"):
        result = audit.rules[rule_id]
        assert result.selected_policy is not None
        assert result.development is not None
        assert result.development.label_counts["Agree"] == 1
        assert result.development.label_counts["Disagree"] == 1


def test_candidate_cap_is_deterministic_and_audited() -> None:
    cases = _cases(["Agree", "Disagree"], scores=[3, 2])
    objective = {"max_candidates": 2}
    first = select_policies(cases, objective=objective)
    second = select_policies(cases, objective=objective)

    first_result = first.rules["custom-rule"]
    second_result = second.rules["custom-rule"]
    assert first_result.search == second_result.search
    assert first_result.search is not None
    assert first_result.search["limit"] == 2
    assert first_result.search["generated"] >= 2
    assert first_result.search["truncated"]
    assert first_result.search["strategy"] == "balanced_coordinate_interleave"
    assert [candidate.policy_hash for candidate in first_result.candidates] == [
        candidate.policy_hash for candidate in second_result.candidates
    ]


def test_source_group_is_authoritative_for_split_leakage() -> None:
    development = _cases(["Agree"], source_group="same-owner")
    heldout = _cases(["Disagree"], split="heldout", source_group="same-owner")
    development[0].provenance["source_hash"] = "old"
    heldout[0].provenance["source_hash"] = "new"

    try:
        select_policies(development + heldout, heldout_split="heldout")
    except ValueError as exc:
        assert "support groups overlap" in str(exc)
    else:
        raise AssertionError("source-group leakage must be rejected")


def test_missing_explicit_support_group_retains_baseline() -> None:
    cases = _cases(["Agree", "Disagree"], scores=[3, 2])
    for case in cases:
        case.provenance.pop("source_group", None)
    result = select_policies(cases).rules["custom-rule"]

    assert result.reason == "missing_support_groups"
    assert result.selected_policy is not None
    assert policy_hash(result.selected_policy) == policy_hash(_score_rule().report)


def test_heldout_semantic_mismatch_is_not_used_for_selection() -> None:
    development = _cases(["Agree", "Disagree", "Disagree"], scores=[3, 2, 2])
    heldout_document = _typed_case(
        "different-heldout",
        "custom-rule",
        {"type": "score", "instructions": "Different polarity", "criteria": ["0", "1", "2", "3"]},
        {"message": "Review", "levels": {"warning": {"min_score": 2}, "error": {"min_score": 3}}},
        {"type": "score", "score": 2.0, "confidence": 0.9, "probabilities": {"0": 0.0, "1": 0.0, "2": 0.9, "3": 0.1}},
        "Agree",
    )
    heldout_document["split"] = "heldout"
    heldout_document["provenance"]["source_group"] = "heldout-owner"
    heldout = load_cases(StringIO(json.dumps(heldout_document)))

    result = select_policies(development + heldout, heldout_split="heldout").rules["custom-rule"]
    assert result.selected_policy is not None
    assert result.selected_policy.levels.warning.min_score == 3
    assert result.rule_mismatches


def test_cli_applies_only_selected_project_rule_and_preserves_yaml(tmp_path: Path) -> None:
    cases = _cases(["Agree", "Disagree", "Disagree"], scores=[3, 2, 2])
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text("\n".join(json.dumps(case.model_dump(mode="json")) for case in cases) + "\n")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        """version: 4
rules:
  # keep this comment
  - name: custom-rule
    ruleset: project
    applies_to: [function]
    question:
      type: score
      instructions: How tangled?
      criteria: ["0", "1", "2", "3"]
    report:
      message: Review
      levels:
        warning: {min_score: 2}
        error: {min_score: 3}
  - name: packaged
    ruleset: JEV
    applies_to: [function]
    question:
      type: score
      instructions: How tangled?
      criteria: ["0", "1", "2", "3"]
    report:
      message: Keep
      levels:
        warning: {min_score: 2}
        error: {min_score: 3}
"""
    )
    rules_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    selected_path = tmp_path / "selected.yaml"
    audit_path = tmp_path / "audit.json"

    assert (
        main([
            str(cases_path),
            "--select",
            "--rules",
            str(rules_path),
            "--selected-policy",
            str(selected_path),
            "--apply",
            "--output",
            str(audit_path),
        ])
        == 0
    )

    updated = rules_path.read_text()
    assert "# keep this comment" in updated
    assert "warning: {min_score: 3.0}" in updated
    assert "name: packaged" in updated
    assert "message: Keep" in updated
    assert stat.S_IMODE(rules_path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
    assert json.loads(audit_path.read_text())["writeback"]["applied"]


def test_cli_rejects_selection_output_aliasing_cases(tmp_path: Path) -> None:
    cases = _cases(["Agree", "Disagree"])
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text("\n".join(json.dumps(case.model_dump(mode="json")) for case in cases) + "\n")
    before = cases_path.read_bytes()

    assert main([str(cases_path), "--select", "--selected-policy", str(cases_path)]) == 2
    assert cases_path.read_bytes() == before


def test_cli_apply_baseline_is_byte_for_byte_noop(tmp_path: Path) -> None:
    cases = _cases(["Agree"])
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text("\n".join(json.dumps(case.model_dump(mode="json")) for case in cases) + "\n")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        """version: 4
rules:
  - name: custom-rule
    question:
      type: score
      instructions: How tangled?
      criteria: ["0", "1", "2", "3"]
    applies_to: [function]
    report:
      message: Review
      levels:
        warning: {min_score: 2}
        error: {min_score: 3}
"""
    )
    before = rules_path.read_bytes()
    audit_path = tmp_path / "audit.json"

    assert (
        main([
            str(cases_path),
            "--select",
            "--rules",
            str(rules_path),
            "--apply",
            "--output",
            str(audit_path),
        ])
        == 0
    )
    assert rules_path.read_bytes() == before
    assert not json.loads(audit_path.read_text())["writeback"]["applied"]


def test_cli_invalid_supplied_rule_never_writes(tmp_path: Path) -> None:
    cases = _cases(["Agree", "Disagree"])
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text("\n".join(json.dumps(case.model_dump(mode="json")) for case in cases) + "\n")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        """version: 4
rules:
  - name: custom-rule
    question:
      type: score
      instructions: How tangled?
      criteria: ["0", "1", "2", "3"]
    applies_to: [function]
    report:
      message: Review
      levels:
        warning: {min_score: 2, unexpected: true}
        error: {min_score: 3}
"""
    )
    before = rules_path.read_bytes()

    assert main([str(cases_path), "--select", "--rules", str(rules_path), "--apply"]) == 2
    assert rules_path.read_bytes() == before


def test_cli_rejects_symlink_rules_path(tmp_path: Path) -> None:
    cases = _cases(["Agree", "Disagree"])
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text("\n".join(json.dumps(case.model_dump(mode="json")) for case in cases) + "\n")
    target = tmp_path / "real-rules.yaml"
    target.write_text(
        """version: 4
rules:
  - name: custom-rule
    question:
      type: score
      instructions: How tangled?
      criteria: ["0", "1", "2", "3"]
    applies_to: [function]
    report:
      message: Review
      levels:
        warning: {min_score: 2}
        error: {min_score: 3}
"""
    )
    symlink = tmp_path / "rules.yaml"
    symlink.symlink_to(target)

    assert main([str(cases_path), "--select", "--rules", str(symlink), "--apply"]) == 2
    assert target.read_text().startswith("version: 4")


def test_heldout_only_cases_do_not_export_a_policy() -> None:
    cases = _cases(["Agree"], split="heldout")
    result = select_policies(cases, heldout_split="heldout").rules["custom-rule"]

    assert result.selected_policy is None
    assert result.reason == "no_cases"


def test_supplied_baseline_can_report_heldout_without_development() -> None:
    cases = _cases(["Agree"], split="heldout")
    result = select_policies(
        cases,
        heldout_split="heldout",
        rules={"custom-rule": _score_rule()},
    ).rules["custom-rule"]

    assert result.selected_policy is not None
    assert result.reason == "no_development_cases"
    assert result.heldout is not None


def test_ungrouped_heldout_cases_are_rejected() -> None:
    development = _cases(["Agree", "Disagree"], source_group="development-source")
    heldout = _cases(["Agree"], split="heldout", source_group="heldout-source")
    heldout[0].provenance.pop("source_group", None)

    with pytest.raises(ValueError, match="heldout cases require explicit support groups"):
        select_policies(development + heldout, heldout_split="heldout")
