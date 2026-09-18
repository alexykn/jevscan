from pathlib import Path
from typing import Any

import pytest
import yaml

from jevscan.core.config import ConfigError, default_yaml, load_config, resolved_yaml


def write_config(path: Path, document: dict) -> Path:
    target = path / "jevscan.yaml"
    target.write_text(yaml.safe_dump(document, sort_keys=False))
    return target


@pytest.mark.parametrize("text", ["", "# Project defaults\n", "{}\n", "version: 4\nrules: []\n"])
def test_packaged_default_and_empty_project_are_additive(tmp_path: Path, text: str) -> None:
    original = load_config([tmp_path], cwd=tmp_path)
    assert original.source == "packaged default"
    assert list(original.config.rules) == [f"JEV{i:02}" for i in range(1, 10)]
    (tmp_path / "jevscan.yaml").write_text(text)
    child = tmp_path / "src" / "nested"
    child.mkdir(parents=True)
    loaded = load_config([child], cwd=child)
    assert loaded.root == tmp_path
    assert loaded.config == original.config
    assert len(loaded.config.selected_rules()) == 9


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
    assert len(config.rules) == 11
    assert config.rules["JEV02"].report.levels.warning.min_score == 1.2
    assert config.rules["JEV02"].report.levels.error.min_score == 2
    assert set(config.selected_rules()) == ({f"JEV{i:02}" for i in range(1, 9)} | {"TEAM01"})
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
    write_config(tmp_path, {"rules": [{"name": "JEV01", "report": {"uncertain_range": None}}]})
    config = load_config([tmp_path], cwd=tmp_path).config
    assert config.rules["JEV01"].report.uncertain_range is None
    assert config.rules["JEV01"].report.levels.warning.min_probability == 0.5
    (tmp_path / "jevscan.yaml").write_text(resolved_yaml(config))
    assert load_config([tmp_path], cwd=tmp_path).config == config


def test_default_has_no_personal_document_reference() -> None:
    assert "AGENTS.md" not in default_yaml()


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


@pytest.mark.parametrize(
    "patch",
    [
        {"enrichment": {"max_calls_per_file": 0}},
        {"enrichment": {"max_candidates": 1, "max_evidence": 2}},
        {"enrichment": {"min_relevance": 0.5}},
        {"enrichment": {"min_evidence_probability": 0.5}},
        {"rules": [{"name": "JEV01", "report": {"uncertain_range": [0.7, 0.4]}}]},
        {"rules": [{"name": "JEV04", "report": {"not_applicable_choices": ["demonstrably_redundant"]}}]},
        {"rules": [{"name": "JEV04", "enrich_on": ["invented"]}]},
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
