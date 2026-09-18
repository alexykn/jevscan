import tomllib
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from jevscan.core.config import ConfigError, default_toml, load_config, resolved_toml


def write_config(path: Path, document: dict) -> Path:
    target = path / "jevscan.toml"
    target.write_text(tomli_w.dumps(document))
    return target


def test_packaged_default_and_empty_project_are_additive(tmp_path: Path) -> None:
    original = load_config([tmp_path], cwd=tmp_path)
    assert original.source == "packaged default"
    assert list(original.config.rules) == [f"JEV{i:02}" for i in range(1, 10)]
    write_config(tmp_path, {"version": 4, "rules": []})
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


def test_resolved_toml_round_trips_selection_and_full_rule_contract(tmp_path, basic_rule):
    write_config(
        tmp_path,
        {
            "rules": [{"name": "CUSTOM01", **basic_rule.model_dump(mode="json", exclude_none=True)}],
            "lint": {"select": ["CUSTOM01"]},
        },
    )
    config = load_config([tmp_path], cwd=tmp_path).config
    text = resolved_toml(config)
    assert isinstance(tomllib.loads(text)["rules"], list)
    (tmp_path / "jevscan.toml").write_text(text)
    assert load_config([tmp_path], cwd=tmp_path).config == config


@pytest.mark.parametrize(
    "text",
    [
        "version = 4\nversion = 4",
        "version = 3",
        "version = 4.0",
        "unknown = true",
        'extends = "default"',
        "rules = {}",
        "rules = [7]",
        '[[rules]]\ntitle="no name"',
        '[[rules]]\nname="bad.name"',
        '[[rules]]\nname="JEV01"\n[[rules]]\nname="JEV01"',
        '[[rules]]\nname="MISSPELLED"\nenabled=false',
        '[[rules]]\nname="JEV02"\n[rules.report.levels.error]\nmin_score=999',
        '[[rules]]\nname="JEV04"\n[rules.report]\nchoices=["invented"]',
        '[[rules]]\nname="JEV01"\n[rules.report.levels.warning]\nmin_probability=0.99',
        '[jev]\nbase_url="https://untrusted.example"',
        '[lint]\nignore=["JVE01"]',
        '[lint]\nselect=["unknown-group"]',
        "[rulesets.JEV01]\nenabled=false",
        '[[rules]]\nname="JEV01"\nruleset="absent"',
        "[rulesets.ALL]\nenabled=true",
    ],
)
def test_invalid_config_fails_at_boundary(tmp_path: Path, text: str) -> None:
    (tmp_path / "jevscan.toml").write_text(text)
    with pytest.raises(ConfigError):
        load_config([tmp_path], cwd=tmp_path)


def test_git_boundary_and_explicit_override(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"lint": {"select": []}})
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert load_config([repo], cwd=repo).source == "packaged default"
    assert not load_config([repo], explicit=path, cwd=repo).config.selected_rules()


def test_legacy_yaml_is_not_silently_ignored_or_reinterpreted(tmp_path: Path) -> None:
    legacy = tmp_path / "jevscan.yaml"
    legacy.write_text("version: 3\nrules: {}\n")
    with pytest.raises(ConfigError, match="YAML.*migration"):
        load_config([tmp_path], cwd=tmp_path)
    with pytest.raises(ConfigError, match="YAML.*migration"):
        load_config([tmp_path], explicit=legacy, cwd=tmp_path)
    write_config(tmp_path, {})
    with pytest.raises(ConfigError, match="multiple"):
        load_config([tmp_path], cwd=tmp_path)


def test_default_has_no_personal_document_reference() -> None:
    assert "AGENTS.md" not in default_toml()


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
