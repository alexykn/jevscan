"""Safe YAML loading, a validated rule schema, and explicit configuration precedence."""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
import yaml.resolver
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jevscan.core.models import Kind

MAX_CONFIG_BYTES = 1_048_576
CONFIG_NAMES = ("jevscan.yaml", "jevscan.yml")
LANGUAGES = frozenset({"python", "rust", "perl", "typescript", "javascript"})


class ConfigError(ValueError):
    """Invalid configuration or ambiguous configuration discovery."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class NoulQuestion(StrictModel):
    type: Literal["noul"]
    instructions: str = Field(min_length=1)


class ChoiceQuestion(StrictModel):
    type: Literal["choice"]
    instructions: str = Field(min_length=1)
    criteria: dict[str, str] = Field(min_length=2, max_length=64)

    @field_validator("criteria")
    @classmethod
    def meaningful_labels(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not description.strip() for key, description in value.items()):
            raise ValueError("choice labels and descriptions must be nonempty")
        return value


class ScoreQuestion(StrictModel):
    type: Literal["score"]
    instructions: str = Field(min_length=1)
    criteria: list[str] = Field(min_length=2, max_length=64)


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


class ReportThreshold(StrictModel):
    min_probability: float | None = Field(default=None, ge=0, le=1)
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    min_score: float | None = None
    max_score: float | None = None


class ReportLevels(StrictModel):
    warning: ReportThreshold
    error: ReportThreshold


class ReportPolicy(StrictModel):
    message: str = Field(min_length=1)
    levels: ReportLevels
    choices: list[str] | None = None
    expected: bool = True


class Rule(StrictModel):
    enabled: bool = True
    applies_to: list[Kind] = Field(min_length=1)
    languages: list[str] = Field(default_factory=lambda: sorted(LANGUAGES))
    require_body: bool = False
    question: Question
    report: ReportPolicy

    @field_validator("languages")
    @classmethod
    def known_languages(cls, value: list[str]) -> list[str]:
        if not value or set(value) - LANGUAGES:
            raise ValueError(f"languages must be drawn from {sorted(LANGUAGES)}")
        return value

    @model_validator(mode="after")
    def compatible_report(self) -> Self:
        question, report = self.question, self.report
        warning, error = report.levels.warning, report.levels.error

        for name, level in (("warning", warning), ("error", error)):
            if isinstance(question, ScoreQuestion):
                if (level.min_score is None) == (level.max_score is None):
                    raise ValueError(f"{name} score level requires exactly one of min_score or max_score")
                threshold = level.min_score if level.min_score is not None else level.max_score
                assert threshold is not None
                if not 0 <= threshold <= len(question.criteria) - 1:
                    raise ValueError(f"{name} score threshold is outside the rubric")
                if level.min_probability is not None or report.choices is not None or not report.expected:
                    raise ValueError("score reports cannot use min_probability, choices, or expected=false")
            elif isinstance(question, ChoiceQuestion):
                if not report.choices or set(report.choices) - question.criteria.keys():
                    raise ValueError("choice reports require choices present in question.criteria")
                if level.min_probability is None:
                    raise ValueError(f"{name} choice level requires min_probability")
                if level.min_score is not None or level.max_score is not None or not report.expected:
                    raise ValueError("choice reports cannot use score thresholds or expected=false")
            else:
                if level.min_probability is None:
                    raise ValueError(f"{name} noul level requires min_probability")
                if any(
                    value is not None
                    for value in (level.min_confidence, level.min_score, level.max_score, report.choices)
                ):
                    raise ValueError("noul reports use min_probability, not confidence, score, or choices")

        self._validate_level_order(warning, error)
        return self

    @staticmethod
    def _validate_level_order(warning: ReportThreshold, error: ReportThreshold) -> None:
        if (warning.min_score is None) != (error.min_score is None):
            raise ValueError("warning and error score levels must use the same threshold direction")
        if (warning.max_score is None) != (error.max_score is None):
            raise ValueError("warning and error score levels must use the same threshold direction")
        if warning.min_probability is not None and error.min_probability is not None:
            if warning.min_probability > error.min_probability:
                raise ValueError("warning min_probability cannot exceed error min_probability")
        if warning.min_confidence is not None:
            if error.min_confidence is None or warning.min_confidence > error.min_confidence:
                raise ValueError("error min_confidence must be at least as strict as warning min_confidence")
        if warning.min_score is not None and error.min_score is not None and warning.min_score > error.min_score:
            raise ValueError("warning min_score cannot exceed error min_score")
        if warning.max_score is not None and error.max_score is not None and warning.max_score < error.max_score:
            raise ValueError("warning max_score cannot be below error max_score")


class ScanConfig(StrictModel):
    include: list[str] = Field(default_factory=lambda: [
        "*.py", "*.pyi", "*.rs", "*.pl", "*.pm", "*.t", "*.ts", "*.tsx", "*.mts", "*.cts",
        "*.js", "*.jsx", "*.mjs", "*.cjs",
    ])
    exclude: list[str] = Field(default_factory=lambda: [
        ".git/", ".venv/", "venv/", "node_modules/", "target/", "dist/", "build/",
        "__pycache__/", ".jevscan-cache/", "vendor/", "*.min.js",
    ])
    respect_gitignore: bool = True
    max_file_bytes: int = Field(default=2_000_000, ge=1)
    max_units_per_file: int = Field(default=10_000, ge=1)
    # These are explicit resource limits, never code-quality thresholds.
    max_unit_bytes: int = Field(default=32_000, ge=256)
    context_bytes: int = Field(default=6_000, ge=0)
    context_members: int = Field(default=32, ge=0)
    batch_size: int = Field(default=8, ge=1, le=256)
    jobs: int = Field(default=0, ge=0, le=256)
    queue_size: int = Field(default=64, ge=1)


class JevConfig(StrictModel):
    model: str = Field(default="jev-latest", min_length=1)
    concurrency: int = Field(default=16, ge=1, le=1024)
    requests_per_minute: float = Field(default=600, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)
    retries: int = Field(default=3, ge=0, le=10)
    max_retry_delay: float = Field(default=60, ge=0)
    max_request_bytes: int = Field(default=60_000, ge=1024)
    max_questions: int = Field(default=64, ge=1, le=256)



class CacheConfig(StrictModel):
    enabled: bool = True
    path: str = ".jevscan-cache/results.sqlite3"
    ttl_seconds: int = Field(default=86_400, ge=0)

    @field_validator("path")
    @classmethod
    def project_relative_path(cls, value: str) -> str:
        path = Path(value)
        if not value.strip() or path.is_absolute() or ".." in path.parts or path == Path("."):
            raise ValueError("cache.path must be a nonempty file path inside the project")
        return value


class Config(StrictModel):
    version: Literal[2] = 2
    scan: ScanConfig = Field(default_factory=ScanConfig)
    jev: JevConfig = Field(default_factory=JevConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
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
    try:
        config = Config.model_validate(document)
    except (ValidationError, RecursionError) as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return LoadedConfig(config=config, root=root, source=source)
