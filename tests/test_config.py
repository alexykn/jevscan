import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from jevscan.core.config import ConfigError, default_yaml, load_config, resolved_yaml
from jevscan.core.rules import ChoiceQuestion, NoulQuestion


def write_config(path: Path, document: dict) -> Path:
    target = path / "jevscan.yaml"
    target.write_text(yaml.safe_dump(document, sort_keys=False))
    return target


@pytest.mark.parametrize("text", ["", "# Project defaults\n", "{}\n", "version: 4\nrules: []\n"])
def test_packaged_default_and_empty_project_are_additive(tmp_path: Path, text: str) -> None:
    original = load_config([tmp_path], cwd=tmp_path)
    assert original.source == "packaged default"
    expected_names = [f"JEV{i:02}" for i in range(1, 17)]
    assert list(original.config.rules) == expected_names
    (tmp_path / "jevscan.yaml").write_text(text)
    child = tmp_path / "src" / "nested"
    child.mkdir(parents=True)
    loaded = load_config([child], cwd=child)
    assert loaded.root == tmp_path
    assert loaded.config == original.config
    assert list(loaded.config.selected_rules()) == expected_names


def test_rule_overrides_custom_sets_and_selection_are_independent(tmp_path, basic_rule):
    document: dict[str, Any] = {
        "rulesets": {"TEAM": {"description": "Team rules"}},
        "rules": [
            {"name": "JEV02", "report": {"levels": {"warning": {"min_score": 1.2}}}},
            {"name": "TEAM01", **basic_rule.model_dump(mode="json", exclude_none=True), "ruleset": "TEAM"},
            {
                "name": "TEAM02",
                **basic_rule.model_dump(mode="json", exclude_none=True),
                "ruleset": "TEAM",
                "enabled": False,
            },
        ],
        "lint": {"ignore": ["JEV09"]},
    }
    write_config(tmp_path, document)
    config = load_config([tmp_path], cwd=tmp_path).config
    assert len(config.rules) == 18
    assert config.rules["JEV02"].report.levels.warning.min_score == 1.2
    assert config.rules["JEV02"].report.levels.error.min_score == 2
    assert set(config.selected_rules()) == ({f"JEV{i:02}" for i in range(1, 17)} - {"JEV09"} | {"TEAM01"})
    # Ignore a whole built-in set, without deleting its definitions.
    document["lint"] = {"ignore": ["JEV"]}
    write_config(tmp_path, document)
    config = load_config([tmp_path], cwd=tmp_path).config
    assert set(config.selected_rules()) == {"TEAM01"}
    # Explicit set-level disable wins over explicit selection; custom and built-in behave alike.
    document["rulesets"] = {"TEAM": {"description": "Team rules", "enabled": False}}
    document["lint"] = {"select": ["TEAM01", "JEV02"]}
    write_config(tmp_path, document)
    assert set(load_config([tmp_path], cwd=tmp_path).config.selected_rules()) == {"JEV02"}


def test_resolved_yaml_round_trips_selection_and_full_rule_contract(tmp_path, basic_rule):
    write_config(
        tmp_path,
        {
            "rules": [{"name": "CUSTOM01", **basic_rule.model_dump(mode="json", exclude_none=True)}],
            "lint": {"select": ["CUSTOM01"]},
        },
    )
    config = load_config([tmp_path], cwd=tmp_path).config
    text = resolved_yaml(config)
    assert isinstance(yaml.safe_load(text)["rules"], list)
    (tmp_path / "jevscan.yaml").write_text(text)
    assert load_config([tmp_path], cwd=tmp_path).config == config


@pytest.mark.parametrize(
    "text",
    [
        "version: 4\nversion: 4",
        "version: 3",
        "version: 4.0",
        "version: true",
        "unknown: true",
        "extends: default",
        "[]",
        "false",
        "rules: {}",
        "rules: [7]",
        "rules:\n- title: no name",
        "rules:\n- name: bad.name",
        "rules:\n- name: JEV01\n- name: JEV01",
        "rules:\n- name: MISSPELLED\n  enabled: false",
        "rules:\n- name: JEV02\n  report:\n    levels:\n      error:\n        min_score: 999",
        "rules:\n- name: JEV04\n  report:\n    choices: [invented]",
        "rules:\n- name: JEV01\n  report:\n    levels:\n      warning:\n        min_probability: 0.99",
        "jev:\n  base_url: https://untrusted.example",
        "lint:\n  ignore: [JVE01]",
        "lint:\n  select: [unknown-group]",
        "rulesets:\n  JEV01:\n    enabled: false",
        "rules:\n- name: JEV01\n  ruleset: absent",
        "rulesets:\n  ALL:\n    enabled: true",
        "rules:\n- name: JEV01\n  enabled: false\n  enabled: true",
        "7: value",
        "!!python/object/apply:builtins.print [unsafe]",
        "rules: [",
        "enrichment: &recursive\n  child: *recursive",
    ],
)
def test_invalid_config_fails_at_boundary(tmp_path: Path, text: str) -> None:
    (tmp_path / "jevscan.yaml").write_text(text)
    with pytest.raises(ConfigError):
        load_config([tmp_path], cwd=tmp_path)


def test_git_boundary_and_explicit_override(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"lint": {"select": []}})
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert load_config([repo], cwd=repo).source == "packaged default"
    assert not load_config([repo], explicit=path, cwd=repo).config.selected_rules()


@pytest.mark.parametrize("name", ["jevscan.yaml", "jevscan.yml"])
def test_yaml_discovery_explicit_override_and_ambiguity(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("version: 4\nlint:\n  ignore: [JEV09]\n")
    implicit = load_config([tmp_path], cwd=tmp_path)
    explicit = load_config([tmp_path], explicit=path, cwd=tmp_path)
    assert implicit == explicit
    assert "JEV09" not in implicit.config.selected_rules()
    other = "jevscan.yml" if name == "jevscan.yaml" else "jevscan.yaml"
    (tmp_path / other).write_text("version: 4\n")
    with pytest.raises(ConfigError, match="both jevscan.yaml and jevscan.yml"):
        load_config([tmp_path], cwd=tmp_path)
    # An explicit path deliberately disambiguates discovery.
    assert load_config([tmp_path], explicit=path, cwd=tmp_path) == explicit


def test_previous_yaml_schema_needs_named_rule_migration(tmp_path: Path) -> None:
    path = tmp_path / "jevscan.yaml"
    path.write_text("version: 3\nrules: {}\n")
    with pytest.raises(ConfigError, match="version 4.*migration"):
        load_config([tmp_path], cwd=tmp_path)


def test_yaml_optional_field_can_be_cleared_without_deleting_other_settings(tmp_path):
    baseline = load_config([tmp_path], cwd=tmp_path).config.rules["JEV01"].report
    write_config(tmp_path, {"rules": [{"name": "JEV01", "report": {"uncertain_range": None}}]})
    config = load_config([tmp_path], cwd=tmp_path).config
    assert config.rules["JEV01"].report.uncertain_range is None
    assert config.rules["JEV01"].report.levels == baseline.levels
    (tmp_path / "jevscan.yaml").write_text(resolved_yaml(config))
    assert load_config([tmp_path], cwd=tmp_path).config == config


def test_default_context_sensitive_choices_require_visible_phenomena(tmp_path: Path) -> None:
    config = load_config([tmp_path], cwd=tmp_path).config
    expectations = {
        "JEV04": ("repeated check is visible", "A concrete repeated validation is visible"),
        "JEV05": ("fallback behavior is visible", "A concrete fallback"),
        "JEV06": ("helper decomposition is visibly present", "Helper decomposition is visibly present"),
    }
    for name, (instruction_phrase, criterion_phrase) in expectations.items():
        question = config.rules[name].question
        assert isinstance(question, ChoiceQuestion)
        assert instruction_phrase in question.instructions
        assert criterion_phrase in question.criteria["insufficient_context"]


def test_all_builtin_nouls_declare_native_true_false_criteria(tmp_path: Path) -> None:
    config = load_config([tmp_path], cwd=tmp_path).config
    for rule in config.rules.values():
        if isinstance(rule.question, NoulQuestion):
            assert rule.question.criteria is not None
            assert set(rule.question.criteria.model_dump()) == {"true", "false"}


def test_default_has_no_personal_document_reference() -> None:
    assert "AGENTS.md" not in default_yaml()


def test_readme_yaml_rule_examples_load(tmp_path: Path) -> None:
    examples = re.findall(r"```yaml\n(.*?)```", Path("README.md").read_text(), re.DOTALL)
    assert len(examples) >= 6
    for index, example in enumerate(examples):
        document = yaml.safe_load(example)
        if not isinstance(document, dict) or "rules" not in document:
            continue
        directory = tmp_path / str(index)
        directory.mkdir()
        (directory / "jevscan.yaml").write_text(example)
        load_config([directory], cwd=directory)


@pytest.mark.parametrize("value", ["/tmp/cache.sqlite3", "../cache.sqlite3", "", "."])
def test_cache_paths_cannot_escape_the_project(value: str) -> None:
    from pydantic import ValidationError

    from jevscan.core.config import CacheConfig

    with pytest.raises(ValidationError):
        CacheConfig(path=value)


@pytest.mark.parametrize(
    "patch",
    [
        {"target": "module"},
        {"target": "tree"},
        {"target": "file", "context": "file"},
        {"target": "file", "context": "owner", "applies_to": []},
    ],
)
def test_target_contracts_reject_unsupported_or_ambiguous_scopes(basic_rule, patch: dict) -> None:
    from pydantic import ValidationError

    from jevscan.core.rules import Rule

    with pytest.raises(ValidationError):
        Rule.model_validate({**basic_rule.model_dump(), **patch})


def test_packaged_file_rules(tmp_path):
    config = load_config([tmp_path], cwd=tmp_path).config
    assert {name for name, rule in config.rules.items() if rule.target == "file"} == {"JEV07", "JEV09"}


def test_packaged_partial_transition_rule_uses_static_admission_and_uncertainty(tmp_path):
    rule = load_config([tmp_path], cwd=tmp_path).config.rules["JEV10"]
    assert isinstance(rule.question, ChoiceQuestion)
    assert rule.applicability is not None
    assert rule.applicability.requires_any == ["state_transition_candidate"]
    assert rule.question.criteria.keys() == {
        "unaccounted_partial_transition",
        "accounted_transition",
        "insufficient_context",
    }
    assert rule.report.choices == ["unaccounted_partial_transition"]
    assert rule.report.uncertain_choices == ["insufficient_context"]
    assert rule.targeted_enrichment is not None
    assert rule.targeted_enrichment.when_choices == ["insufficient_context"]
    assert rule.report.levels.warning.min_probability == 0.6
    assert rule.report.levels.warning.min_confidence == 0.5
    assert rule.report.levels.error.min_probability == 0.92
    assert rule.report.levels.error.min_confidence == 0.7
    exclusion = "Do not classify an explicitly progressive or restartable workflow as unaccounted_partial_transition"
    assert exclusion in rule.question.instructions
    assert exclusion in rule.question.criteria["accounted_transition"]


def test_packaged_terminal_reentry_contract(tmp_path):
    rule = load_config([tmp_path], cwd=tmp_path).config.rules["JEV11"]
    assert isinstance(rule.question, ChoiceQuestion)
    assert rule.title == "terminal-state-reentry"
    assert rule.ruleset == "JEV"
    assert rule.target == "unit"
    assert rule.context == "owner"
    assert rule.applies_to == ["function", "method", "closure"]
    assert rule.require_body is True
    assert rule.enrich_on == ["missing_evidence", "reduced_context"]
    assert rule.enrichment_families == ["callers", "callees", "tests", "enclosing_context"]
    assert rule.targeted_enrichment is not None
    assert rule.targeted_enrichment.when_choices == ["insufficient_context"]
    assert set(rule.question.criteria) == {
        "terminal_reentry",
        "valid_restart_or_reuse",
        "allowed_terminal_operation",
        "no_terminal_reentry",
        "insufficient_context",
    }
    assert rule.report.choices == ["terminal_reentry"]
    assert rule.report.uncertain_choices == ["insufficient_context"]
    assert rule.report.levels.warning.min_probability == 0.45
    assert rule.report.levels.warning.min_confidence == 0.50
    assert rule.report.levels.error.min_probability == 0.92
    assert rule.report.levels.error.min_confidence == 0.70
    assert rule.report.blocks_exit is True
    assert "blocks_exit" not in rule.report.model_dump(mode="json")


@pytest.mark.parametrize(
    (
        "name",
        "title",
        "criteria",
        "enrichment_families",
        "choices",
        "not_applicable_choices",
        "uncertain_choices",
        "warning",
        "error",
    ),
    [
        (
            "JEV12",
            "hidden-caller-relevant-effect",
            {
                "hidden_caller_relevant_effect",
                "explicit_effect_contract",
                "non_observable_memoization",
                "observability_only",
                "not_applicable",
                "insufficient_context",
            },
            ["callers", "callees", "tests", "enclosing_context"],
            ["hidden_caller_relevant_effect"],
            ["not_applicable"],
            ["insufficient_context"],
            (0.49, 0.38),
            (0.92, 0.7),
        ),
        (
            "JEV13",
            "hidden-caller-relevant-prerequisite",
            {
                "hidden_caller_relevant_prerequisite",
                "explicit_phase_contract",
                "encoded_phase_state",
                "explicit_local_rejection",
                "ordinary_lifecycle_pairing",
                "not_applicable",
                "insufficient_context",
            },
            ["callers", "callees", "tests", "enclosing_context"],
            ["hidden_caller_relevant_prerequisite"],
            ["not_applicable"],
            ["insufficient_context"],
            (0.6, 0.5),
            (0.92, 0.7),
        ),
        (
            "JEV14",
            "stale-derived-representation",
            {"derived_incoherence", "coherent_or_intentional", "insufficient_context"},
            ["callers", "callees", "tests", "enclosing_context"],
            ["derived_incoherence"],
            [],
            ["insufficient_context"],
            (0.4, 0.25),
            (0.92, 0.7),
        ),
        (
            "JEV15",
            "unsafe-retry-after-source-established-unknown-completion",
            {
                "unsafe_retry",
                "safe_retry",
                "explicitly_nonretryable",
                "not_applicable",
                "insufficient_context",
            },
            ["callers", "callees", "tests"],
            ["unsafe_retry"],
            ["not_applicable"],
            ["insufficient_context"],
            (0.46, 0.33),
            (0.92, 0.7),
        ),
        (
            "JEV16",
            "untruthful-success-signal",
            {
                "false_success",
                "truthful_success",
                "explicit_partial_success",
                "not_applicable",
                "insufficient_context",
            },
            ["callers", "callees", "tests", "enclosing_context"],
            ["false_success"],
            ["not_applicable"],
            ["insufficient_context"],
            (0.59, 0.49),
            (0.92, 0.7),
        ),
    ],
)
def test_packaged_new_rule_contracts(
    tmp_path,
    name,
    title,
    criteria,
    enrichment_families,
    choices,
    not_applicable_choices,
    uncertain_choices,
    warning,
    error,
):
    rule = load_config([tmp_path], cwd=tmp_path).config.rules[name]
    assert rule.title == title
    assert rule.ruleset == "JEV"
    assert rule.target == "unit"
    assert rule.context == "owner"
    assert rule.applies_to == ["function", "method", "closure"]
    assert rule.require_body is True
    assert rule.enrich_on == ["missing_evidence", "reduced_context"]
    assert rule.enrichment_families == enrichment_families
    assert isinstance(rule.question, ChoiceQuestion)
    assert set(rule.question.criteria) == criteria
    assert rule.report.choices == choices
    assert rule.report.not_applicable_choices == not_applicable_choices
    assert rule.report.uncertain_choices == uncertain_choices
    assert rule.report.levels.warning.min_probability == warning[0]
    assert rule.report.levels.warning.min_confidence == warning[1]
    assert rule.report.levels.error.min_probability == error[0]
    assert rule.report.levels.error.min_confidence == error[1]
    assert rule.report.blocks_exit is False
    assert rule.report.model_dump(mode="json")["blocks_exit"] is False


@pytest.mark.parametrize(
    "patch",
    [
        {"compaction": {"max_rounds": 0}},
        {"compaction": {"max_calls_per_file": -1}},
        {"compaction": {"context_tokens": 100}},
        {"enrichment": {"max_calls_per_file": 0}},
        {"enrichment": {"max_candidates": 1, "max_evidence": 2}},
        {"enrichment": {"min_relevance": 0.5}},
        {"enrichment": {"min_evidence_probability": 0.5}},
        {"rules": [{"name": "JEV01", "report": {"uncertain_range": [0.7, 0.4]}}]},
        {"rules": [{"name": "JEV04", "report": {"not_applicable_choices": ["demonstrably_redundant"]}}]},
        {"rules": [{"name": "JEV04", "enrich_on": ["invented"]}]},
        {"rules": [{"name": "JEV04", "enrichment_families": []}]},
    ],
)
def test_enrichment_policy_is_validated_at_configuration_boundary(tmp_path, patch):
    write_config(tmp_path, patch)
    with pytest.raises(ConfigError):
        load_config([tmp_path], cwd=tmp_path)


@pytest.mark.parametrize("triggers", [[], ["missing_evidence"], ["low_confidence", "reduced_context"]])
def test_project_can_replace_enrichment_admission(tmp_path, triggers):
    write_config(tmp_path, {"rules": [{"name": "JEV04", "enrich_on": triggers}]})
    assert load_config([tmp_path], cwd=tmp_path).config.rules["JEV04"].enrich_on == triggers


def test_default_enrichment_is_targeted_and_rules_declare_evidence_families(tmp_path: Path) -> None:
    config = load_config([tmp_path], cwd=tmp_path).config
    assert config.enrichment.mode == "targeted"
    assert config.rules["JEV04"].enrichment_families == ["callers", "callees"]
    assert config.rules["JEV06"].enrichment_families == ["callees", "enclosing_context"]
    assert config.rules["JEV04"].context == "owner"
    assert config.rules["JEV08"].context == "owner"
    assert config.scan.max_full_file_lines == 3000
    assert config.budget.max_requests == 1500
    assert config.budget.max_input_tokens == 5_000_000
