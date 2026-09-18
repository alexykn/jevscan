from pathlib import Path

import pytest

from jevscan.core.config import ConfigError, default_yaml, load_config


def test_packaged_default_is_complete(tmp_path: Path) -> None:
    loaded = load_config([tmp_path], cwd=tmp_path)
    assert len(loaded.config.rules) == 9
    assert loaded.source == "packaged default"
    assert all(rule.enabled for rule in loaded.config.rules.values())


def test_project_config_replaces_rules_and_is_found_upward(tmp_path: Path) -> None:
    (tmp_path / "jevscan.yaml").write_text("version: 1\nrules: {}\n")
    child = tmp_path / "src" / "nested"
    child.mkdir(parents=True)
    loaded = load_config([child], cwd=child)
    assert loaded.root == tmp_path
    assert loaded.config.rules == {}


def test_extending_default_is_explicit(tmp_path: Path) -> None:
    (tmp_path / "jevscan.yaml").write_text(
        "extends: default\nrules:\n  mixed-responsibilities:\n    report:\n      min_probability: 0.99\n"
        "  fragmented-ownership:\n    enabled: false\n"
    )
    loaded = load_config([tmp_path], cwd=tmp_path)
    assert len(loaded.config.rules) == 9
    assert loaded.config.rules["mixed-responsibilities"].report.min_probability == 0.99
    assert not loaded.config.rules["fragmented-ownership"].enabled


@pytest.mark.parametrize("text", [
    "version: 1\nrules: {}\nrules: {}\n",
    "version: 1\nrules: {}\nunknown: true\n",
    "extends: {bad: value}\nrules: {}\n",
    "!!python/object/apply:os.system ['echo invalid']",
    "extends: default\nrules:\n  unclear-control-flow:\n    report:\n      min_score: 999\n",
    "extends: default\nrules:\n  redundant-validation:\n    report:\n      choices: [invented]\n",
    "extends: default\njev:\n  base_url: https://untrusted.example\n",
])
def test_invalid_config_fails_at_boundary(tmp_path: Path, text: str) -> None:
    (tmp_path / "jevscan.yaml").write_text(text)
    with pytest.raises(ConfigError):
        load_config([tmp_path], cwd=tmp_path)


def test_git_boundary_and_explicit_override(tmp_path: Path) -> None:
    (tmp_path / "jevscan.yaml").write_text("rules: {}\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert load_config([repo], cwd=repo).source == "packaged default"
    assert load_config([repo], explicit=tmp_path / "jevscan.yaml", cwd=repo).config.rules == {}


def test_both_config_names_are_rejected(tmp_path: Path) -> None:
    for name in ("jevscan.yaml", "jevscan.yml"):
        (tmp_path / name).write_text("rules: {}\n")
    with pytest.raises(ConfigError, match="both"):
        load_config([tmp_path], cwd=tmp_path)


def test_default_has_no_personal_document_reference() -> None:
    assert "AGENTS.md" not in default_yaml()


@pytest.mark.parametrize("value", ["/tmp/cache.sqlite3", "../cache.sqlite3", "", "."])
def test_cache_paths_cannot_escape_the_project(value: str) -> None:
    from pydantic import ValidationError

    from jevscan.core.config import CacheConfig

    with pytest.raises(ValidationError):
        CacheConfig(path=value)
