"""Bounded YAML loading, additive named rules, and explicit rule selection."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Self

import yaml
import yaml.resolver
from pydantic import Field, ValidationError, field_validator, model_validator

from jevscan.core.rules import Rule, StrictModel
from jevscan.core.validation import require

MAX_CONFIG_BYTES = 1_048_576
CONFIG_NAMES = ("jevscan.yaml", "jevscan.yml")
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class ConfigError(ValueError):
    """Invalid configuration or ambiguous configuration discovery."""


class ScanConfig(StrictModel):
    include: list[str] = Field(
        default_factory=lambda: [
            "*.py",
            "*.pyi",
            "*.rs",
            "*.pl",
            "*.pm",
            "*.t",
            "*.ts",
            "*.tsx",
            "*.mts",
            "*.cts",
            "*.js",
            "*.jsx",
            "*.mjs",
            "*.cjs",
        ]
    )
    exclude: list[str] = Field(
        default_factory=lambda: [
            ".git/",
            ".venv/",
            "venv/",
            "node_modules/",
            "target/",
            "dist/",
            "build/",
            "__pycache__/",
            ".jevscan-cache/",
            "vendor/",
            "*.min.js",
        ]
    )
    respect_gitignore: bool = True
    max_file_bytes: int = Field(default=2_000_000, ge=1)
    max_units_per_file: int = Field(default=10_000, ge=1)
    max_full_file_lines: int | None = Field(default=3000, ge=100)
    batch_size: int = Field(default=8, ge=1, le=256)
    jobs: int = Field(default=0, ge=0, le=256)
    queue_size: int = Field(default=8, ge=1)


class JevConfig(StrictModel):
    model: str = Field(default="jev-latest", min_length=1)
    concurrency: int = Field(default=16, ge=1, le=1024)
    requests_per_minute: float = Field(default=600, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)
    retries: int = Field(default=3, ge=0, le=10)
    max_retry_delay: float = Field(default=60, ge=0)


class EvaluationConfig(StrictModel):
    # Conservative local planning thresholds below Jev 1.13's published 32k/64k ceilings.
    # Set null only to remove the extra local margin; known provider ceilings still apply.
    max_context_tokens: int | None = Field(default=28_000, ge=1024)
    max_total_tokens: int | None = Field(default=56_000, ge=1024)
    token_reserve: int = Field(default=512, ge=0)
    bytes_per_token: float = Field(default=3.0, ge=1, le=8)
    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    max_questions: int = Field(default=128, ge=1, le=256)
    oversized_context: Literal["reduce", "skip"] = "reduce"

    @model_validator(mode="after")
    def coherent_budgets(self) -> Self:
        require(
            self.max_context_tokens is None
            or self.max_total_tokens is None
            or self.max_total_tokens >= self.max_context_tokens,
            "max_total_tokens must be at least max_context_tokens",
        )
        require(
            self.max_context_tokens is None or self.token_reserve < self.max_context_tokens,
            "token_reserve must be smaller than max_context_tokens",
        )
        return self


class CompactionConfig(StrictModel):
    """Recovery policy, not an advertised provider context window."""

    context_tokens: int = Field(default=24_000, ge=1024)
    max_rounds: int = Field(default=3, ge=1, le=8)
    max_candidates: int = Field(default=32, ge=1, le=128)
    max_calls_per_file: int = Field(default=12, ge=0, le=256)
    semantic: bool = True


class EnrichmentConfig(StrictModel):
    """One bounded evidence-enrichment pass; limits apply even to cache hits."""

    enabled: bool = True
    mode: Literal["off", "targeted", "full"] = "targeted"
    max_checks_per_file: int = Field(default=12, ge=1, le=128)
    max_calls_per_file: int = Field(default=36, ge=1, le=512)
    max_candidates: int = Field(default=12, ge=1, le=64)
    max_evidence: int = Field(default=3, ge=1, le=16)
    min_route_probability: float = Field(default=0.70, gt=0.5, le=1)
    min_route_confidence: float = Field(default=0.50, ge=0, le=1)
    min_evidence_probability: float = Field(default=0.60, gt=0.5, le=1)
    min_relevance: float = Field(default=0.65, gt=0.5, le=1)
    max_source_files: int = Field(default=1000, ge=1)
    max_source_bytes: int = Field(default=16_777_216, ge=1024)

    @model_validator(mode="after")
    def coherent_limits(self) -> Self:
        require(self.max_evidence <= self.max_candidates, "max_evidence cannot exceed max_candidates")
        return self


class BudgetConfig(StrictModel):
    """Optional hard limits for paid live inference. Null means unlimited."""

    max_requests: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_cost: float | None = Field(default=None, gt=0)
    input_cost_per_million: float = Field(default=0.042, ge=0)


class CacheConfig(StrictModel):
    enabled: bool = True
    path: str = ".jevscan-cache/results.sqlite3"
    ttl_seconds: int = Field(default=86_400, ge=0)

    @field_validator("path")
    @classmethod
    def project_relative_path(cls, value: str) -> str:
        path = Path(value)
        require(
            bool(value.strip()) and not path.is_absolute() and ".." not in path.parts and path != Path(),
            "cache.path must be a nonempty file path inside the project",
        )
        return value


class Ruleset(StrictModel):
    enabled: bool = True
    description: str = ""


class LintConfig(StrictModel):
    select: list[str] = Field(default_factory=lambda: ["ALL"])
    ignore: list[str] = Field(default_factory=list)


class Config(StrictModel):
    """Resolved configuration. Rule names are the keys; selection does not mutate rules."""

    version: Literal[4] = 4
    scan: ScanConfig = Field(default_factory=ScanConfig)
    jev: JevConfig = Field(default_factory=JevConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    lint: LintConfig = Field(default_factory=LintConfig)
    rulesets: dict[str, Ruleset] = Field(default_factory=lambda: {"project": Ruleset()})
    rules: dict[str, Rule]

    @model_validator(mode="after")
    def valid_catalogue(self) -> Self:
        _validate_catalogue(self)
        return self

    def selected_rules(self) -> dict[str, Rule]:
        selected = _selected_rule_names(self, set(self.lint.select))
        ignored = _selected_rule_names(self, set(self.lint.ignore))
        return {name: rule for name, rule in self.rules.items() if name in selected - ignored and _rule_enabled(self, rule)}


def _validate_catalogue(config: Config) -> None:
    names = (*config.rules, *config.rulesets)
    invalid = next((name for name in names if not IDENTIFIER.fullmatch(name) or name == "ALL"), None)
    require(invalid is None, f"invalid or reserved rule/ruleset name: {invalid!r}")
    require(not (config.rules.keys() & config.rulesets.keys()), "rule and ruleset names must not overlap")

    unknown_ruleset = next(
        ((name, rule.ruleset) for name, rule in config.rules.items() if rule.ruleset not in config.rulesets),
        None,
    )
    require(
        unknown_ruleset is None,
        f"{unknown_ruleset[0]}: unknown ruleset {unknown_ruleset[1]!r}" if unknown_ruleset else "",
    )
    known = {*config.rules, *config.rulesets, "ALL"}
    unknown = set(config.lint.select + config.lint.ignore) - known
    require(not unknown, f"unknown rule/ruleset selectors: {', '.join(sorted(unknown))}")


def _selected_rule_names(config: Config, selectors: set[str]) -> set[str]:
    return {name for name, rule in config.rules.items() if {name, rule.ruleset, "ALL"} & selectors}


def _rule_enabled(config: Config, rule: Rule) -> bool:
    return all((rule.enabled, config.rulesets[rule.ruleset].enabled))


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: Config
    root: Path
    source: str


def default_yaml() -> str:
    return files("jevscan").joinpath("data/default.yaml").read_text(encoding="utf-8")


def initial_yaml() -> str:
    return "# Built-in rules are included automatically. See --show-config and --list-rules.\nversion: 4\n\nlint:\n  ignore: []\n"


def resolved_yaml(config: Config) -> str:
    document = config.model_dump(mode="json")
    document["rules"] = [{"name": name, **rule} for name, rule in document["rules"].items()]
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys before configuration is merged or validated."""


def _unique_mapping(loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConfigError("YAML mapping keys must be strings")
        if key in result:
            raise ConfigError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _decode_yaml(text: str, source: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{source}: config exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        document = yaml.load(text, Loader=UniqueLoader)  # noqa: S506 -- SafeLoader, no object constructors
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if document is None:
        document = {}  # An empty project adds nothing; packaged rules remain active.
    if not isinstance(document, dict):
        raise ConfigError(f"{source}: expected a YAML mapping")
    if type(document.get("version", 4)) is not int or document.get("version", 4) != 4:
        raise ConfigError("configuration version 4 is required; see docs/CONFIGURATION.md for migration")
    document["rules"] = _named_rules(document.get("rules", []), source)
    return document


def _named_rule(entry: Any, source: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(entry, dict):
        raise ConfigError(f"{source}: each rule must be a mapping")
    name = entry.get("name")
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name == "ALL":
        raise ConfigError(f"{source}: each rule needs a valid name")
    return name, {key: value for key, value in entry.items() if key != "name"}


def _named_rules(entries: Any, source: str) -> dict[str, Any]:
    if not isinstance(entries, list):
        raise ConfigError(f"{source}: rules must be a list of mappings with a name")
    rules: dict[str, Any] = {}
    for entry in entries:
        name, rule = _named_rule(entry, source)
        if name in rules:
            raise ConfigError(f"{source}: duplicate rule name {name!r}")
        rules[name] = rule
    return rules

def validate_config_path(path: Path) -> None:
    if path.suffix not in {".yaml", ".yml"}:
        raise ConfigError("configuration must be YAML (.yaml or .yml)")


def _read_project(path: Path) -> dict[str, Any]:
    validate_config_path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        return _decode_yaml(raw.decode("utf-8"), str(path))
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _merge(base: dict[str, Any], overrides: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > 32:
        raise ConfigError("config nesting exceeds 32 levels")
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value, depth + 1)
        else:
            result[key] = value
    return result


def find_config(start: Path) -> Path | None:
    """Find the nearest config, without inheriting one from outside a Git worktree."""
    start = start.resolve()
    for directory in (start, *start.parents):
        matches = [directory / name for name in CONFIG_NAMES if (directory / name).is_file()]
        if len(matches) > 1:
            raise ConfigError(f"both jevscan.yaml and jevscan.yml exist in {directory}; keep only one")
        if matches:
            return matches[0]
        if (directory / ".git").exists():
            break
    return None


def _project_root(start: Path) -> Path:
    for directory in (start, *start.parents):
        if any((directory / name).exists() for name in (".git", "pyproject.toml", "Cargo.toml", "package.json")):
            return directory
    return start


def _target_config(targets: list[Path]) -> Path | None:
    found = {path for target in targets if (path := find_config(target if target.is_dir() else target.parent))}
    if len(found) > 1:
        raise ConfigError("targets belong to different configs; scan separately or supply --config")
    return next(iter(found), None)


def _selected_config(targets: list[Path], explicit: Path | None, cwd: Path) -> Path | None:
    if explicit is not None:
        return explicit.resolve()
    return find_config(cwd) or _target_config(targets)


def _root_and_source(targets: list[Path], selected: Path | None, cwd: Path) -> tuple[Path, str]:
    if selected is not None:
        return selected.parent, str(selected)
    start = targets[0] if targets and targets[0].is_dir() else (targets[0].parent if targets else cwd)
    return _project_root(start.resolve()), "packaged default"


def _resolved_document(selected: Path | None) -> dict[str, Any]:
    document = _decode_yaml(default_yaml(), "packaged default")
    return _merge(document, _read_project(selected)) if selected is not None else document


def load_config(targets: list[Path], explicit: Path | None = None, cwd: Path | None = None) -> LoadedConfig:
    working_directory = (cwd or Path.cwd()).resolve()
    selected = _selected_config(targets, explicit, working_directory)
    document = _resolved_document(selected)
    root, source = _root_and_source(targets, selected, working_directory)
    try:
        config = Config.model_validate(document)
    except (ValidationError, RecursionError) as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return LoadedConfig(config=config, root=root, source=source)
