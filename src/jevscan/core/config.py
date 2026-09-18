"""Safe YAML loading, a validated rule schema, and explicit configuration precedence."""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Self

import yaml
import yaml.resolver
from pydantic import Field, ValidationError, field_validator, model_validator

from jevscan.core.rules import Rule, StrictModel

MAX_CONFIG_BYTES = 1_048_576
CONFIG_NAMES = ("jevscan.yaml", "jevscan.yml")


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
    # Estimates, not a claim that TypeSafe publishes this tokenizer or these quotas.
    max_context_tokens: int = Field(default=28_000, ge=1024)
    max_total_tokens: int = Field(default=56_000, ge=1024)
    token_reserve: int = Field(default=512, ge=0)
    bytes_per_token: float = Field(default=3.0, ge=1, le=8)
    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    max_questions: int = Field(default=64, ge=1, le=256)
    oversized_context: Literal["reduce", "skip"] = "reduce"

    @model_validator(mode="after")
    def coherent_budgets(self) -> Self:
        if self.max_total_tokens < self.max_context_tokens:
            raise ValueError("max_total_tokens must be at least max_context_tokens")
        if self.token_reserve >= self.max_context_tokens:
            raise ValueError("token_reserve must be smaller than max_context_tokens")
        return self


class CacheConfig(StrictModel):
    enabled: bool = True
    path: str = ".jevscan-cache/results.sqlite3"
    ttl_seconds: int = Field(default=86_400, ge=0)

    @field_validator("path")
    @classmethod
    def project_relative_path(cls, value: str) -> str:
        path = Path(value)
        if not value.strip() or path.is_absolute() or ".." in path.parts or path == Path():
            raise ValueError("cache.path must be a nonempty file path inside the project")
        return value


class Config(StrictModel):
    version: Literal[3] = 3
    scan: ScanConfig = Field(default_factory=ScanConfig)
    jev: JevConfig = Field(default_factory=JevConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    rules: dict[str, Rule]

    @field_validator("rules")
    @classmethod
    def valid_rule_ids(cls, value: dict[str, Rule]) -> dict[str, Rule]:
        for key in value:
            if not key or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in key):
                raise ValueError("rule IDs may contain only lowercase letters, digits, hyphens, and underscores")
        return value


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: Config
    root: Path
    source: str


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently discarding earlier rules."""


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


def default_yaml() -> str:
    return files("jevscan").joinpath("data/default.yaml").read_text(encoding="utf-8")


def _decode_yaml(text: str, source: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{source}: config exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        value = yaml.load(text, Loader=UniqueLoader)  # noqa: S506 -- subclass of SafeLoader, no object constructors
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: expected a YAML mapping")
    return value


def _merge(base: dict[str, Any], overrides: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > 32:
        raise ConfigError("config nesting is excessive or contains recursive YAML aliases")
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
            raise ConfigError(f"both jevscan.yaml and jevscan.yml exist in {directory}")
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


def load_config(targets: list[Path], explicit: Path | None = None, cwd: Path | None = None) -> LoadedConfig:
    cwd = (cwd or Path.cwd()).resolve()
    selected = explicit.resolve() if explicit else find_config(cwd)
    if selected is None:
        found = {p for target in targets if (p := find_config(target if target.is_dir() else target.parent))}
        if len(found) > 1:
            raise ConfigError("targets belong to different configs; scan separately or supply --config")
        selected = next(iter(found), None)
    if selected:
        try:
            with selected.open("rb") as stream:
                raw = stream.read(MAX_CONFIG_BYTES + 1)
            document = _decode_yaml(raw.decode("utf-8"), str(selected))
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"cannot read {selected}: {exc}") from exc
        extension = document.pop("extends", None)
        if extension is not None and extension != "default":
            raise ConfigError("extends must be 'default'; arbitrary config imports are not supported")
        if extension:
            document = _merge(_decode_yaml(default_yaml(), "packaged default"), document)
        root, source = selected.parent, str(selected)
    else:
        document = _decode_yaml(default_yaml(), "packaged default")
        start = targets[0] if targets and targets[0].is_dir() else (targets[0].parent if targets else cwd)
        root, source = _project_root(start.resolve()), "packaged default"
    if document.get("version", 3) != 3:
        raise ConfigError("configuration version 3 is required; see docs/CONFIGURATION.md for migration")
    try:
        config = Config.model_validate(document)
    except (ValidationError, RecursionError) as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return LoadedConfig(config=config, root=root, source=source)
