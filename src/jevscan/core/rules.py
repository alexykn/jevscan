"""Question and reporting contracts, validated once at the configuration boundary."""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from jevscan.core.models import Kind, SyntaxFact
from jevscan.core.validation import (
    require,
    require_disjoint,
    require_exactly_one,
    require_nonempty,
    require_none,
    require_subset,
    require_unique,
)

LANGUAGES = frozenset({"python", "rust", "perl", "typescript", "javascript"})
EnrichmentTrigger = Literal[
    "missing_evidence",
    "reduced_context",
    "applicability",
    "low_confidence",
    "low_choice_probability",
    "weak_defect_signal",
    "probability_ambiguous",
]
DEFAULT_ENRICHMENT_TRIGGERS: tuple[EnrichmentTrigger, ...] = ("missing_evidence", "reduced_context", "applicability")
EnrichmentFamily = Literal["callers", "callees", "tests", "enclosing_context"]
DEFAULT_ENRICHMENT_FAMILIES: tuple[EnrichmentFamily, ...] = ("callers", "callees", "tests", "enclosing_context")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class NoulCriteria(StrictModel):
    true: str = Field(min_length=1)
    false: str = Field(min_length=1)


class ApplicabilityPolicy(StrictModel):
    requires_any: list[SyntaxFact] = Field(default_factory=list)
    requires_all: list[SyntaxFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_requirement(self) -> Self:
        require(bool(self.requires_any or self.requires_all), "applicability requires at least one syntax fact")
        require_unique(self.requires_any, "applicability fact lists must not contain duplicates")
        require_unique(self.requires_all, "applicability fact lists must not contain duplicates")
        require_disjoint(
            (self.requires_any, self.requires_all),
            "a syntax fact cannot be both requires_any and requires_all",
        )
        return self


class TargetedEnrichmentPolicy(StrictModel):
    when_choices: list[str] = Field(default_factory=list)
    when_reasons: list[EnrichmentTrigger] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_trigger(self) -> Self:
        require(bool(self.when_choices or self.when_reasons), "targeted enrichment requires a choice or assessment reason trigger")
        require_unique(self.when_choices, "targeted enrichment trigger lists must not contain duplicates")
        require_unique(self.when_reasons, "targeted enrichment trigger lists must not contain duplicates")
        return self


class NoulQuestion(StrictModel):
    type: Literal["noul"]
    instructions: str = Field(min_length=1)
    criteria: NoulCriteria | None = None


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

    @field_validator("criteria")
    @classmethod
    def meaningful_criteria(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("score criteria must be nonempty")
        return value


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


class ReportThreshold(StrictModel):
    min_probability: float | None = Field(default=None, ge=0, le=1)
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    min_score: float | None = None
    max_score: float | None = None
    score_levels: list[StrictInt] | None = None


class ReportLevels(StrictModel):
    warning: ReportThreshold
    error: ReportThreshold


class ReportPolicy(StrictModel):
    message: str = Field(min_length=1)
    levels: ReportLevels
    blocks_exit: bool = Field(default=True, exclude_if=lambda value: value is True)
    choices: list[str] | None = None
    expected: bool = True
    uncertain_choices: list[str] = Field(default_factory=list)
    not_applicable_choices: list[str] = Field(default_factory=list)
    # Inclusive lower, exclusive upper boundary, applied before severity gates.
    uncertain_range: tuple[float, float] | None = None


class Rule(StrictModel):
    title: str = ""
    ruleset: str = "project"
    enabled: bool = True
    target: Literal["unit", "file"] = "unit"
    context: Literal["unit", "owner", "file"] = "owner"
    applies_to: list[Kind] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: sorted(LANGUAGES))
    require_body: bool = False
    require_members: bool = False
    applicability: ApplicabilityPolicy | None = None
    targeted_enrichment: TargetedEnrichmentPolicy | None = None
    enrich: bool = True
    enrich_on: list[EnrichmentTrigger] = Field(default_factory=lambda: list(DEFAULT_ENRICHMENT_TRIGGERS))
    enrichment_families: list[EnrichmentFamily] = Field(default_factory=lambda: list(DEFAULT_ENRICHMENT_FAMILIES))
    question: Question
    report: ReportPolicy

    @model_validator(mode="before")
    @classmethod
    def default_context(cls, value: Any) -> Any:
        if isinstance(value, dict) and "context" not in value:
            return {**value, "context": "file" if value.get("target") == "file" else "owner"}
        return value

    @field_validator("languages")
    @classmethod
    def known_languages(cls, value: list[str]) -> list[str]:
        if not value or set(value) - LANGUAGES:
            raise ValueError(f"languages must be drawn from {sorted(LANGUAGES)}")
        return value

    @model_validator(mode="after")
    def compatible_report(self) -> Self:
        _validate_target(self)
        _QUESTION_VALIDATORS[type(self.question)](self.question, self.report)
        _validate_targeted_enrichment(self)
        require(
            isinstance(self.question, NoulQuestion) or self.report.uncertain_range is None,
            "uncertain_range is only valid for noul questions",
        )
        _validate_level_order(self.report.levels)
        return self


def _validate_target(rule: Rule) -> None:
    if rule.target == "unit":
        require_nonempty(rule.applies_to, "unit rules require nonempty applies_to")
        return
    require(
        not (rule.applies_to or rule.require_body or rule.require_members) and rule.context == "file",
        "file rules require context=file, no applies_to, and require_body=false",
    )


def _validate_score_mass_level(level: ReportThreshold, criteria_count: int) -> None:
    levels = level.score_levels
    assert levels is not None
    require_nonempty(levels, "score mass levels must be nonempty")
    require_unique(levels, "score mass levels must not contain duplicates")
    require(levels == sorted(levels), "score mass levels must be ordered")
    require(all(0 <= score < criteria_count for score in levels), "score mass level is outside the rubric")
    require(level.min_probability is not None, "score mass levels require min_probability")
    require_none((level.min_score, level.max_score), "score mass levels cannot use score thresholds")


def _validate_score_scalar_level(level: ReportThreshold, criteria_count: int) -> None:
    require_exactly_one(
        (level.min_score, level.max_score),
        "score levels require exactly one of min_score or max_score",
    )
    threshold = level.min_score if level.min_score is not None else level.max_score
    assert threshold is not None
    require(0 <= threshold < criteria_count, "score threshold is outside the rubric")
    require(level.min_probability is None, "score levels cannot use min_probability")
    require(level.score_levels is None, "score scalar levels cannot use score_levels")


def _validate_score(question: Question, report: ReportPolicy) -> None:
    assert isinstance(question, ScoreQuestion)
    require(
        report.choices is None
        and not report.uncertain_choices
        and not report.not_applicable_choices
        and report.expected,
        "score reports cannot use choices, uncertain_choices, or expected=false",
    )
    warning, error = report.levels.warning, report.levels.error
    mass_mode = warning.score_levels is not None
    require(
        mass_mode == (error.score_levels is not None),
        "score warning and error levels must use the same threshold mode",
    )
    validator = _validate_score_mass_level if mass_mode else _validate_score_scalar_level
    validator(warning, len(question.criteria))
    validator(error, len(question.criteria))
    if mass_mode:
        assert warning.score_levels is not None and error.score_levels is not None
        require(
            set(error.score_levels) <= set(warning.score_levels),
            "score error levels must be a subset of warning levels",
        )


def _validate_choice(question: Question, report: ReportPolicy) -> None:
    assert isinstance(question, ChoiceQuestion)
    allowed = question.criteria.keys()
    require_nonempty(report.choices or (), "choice reports require choices present in question.criteria")
    require_subset(report.choices or (), allowed, "choice reports require choices present in question.criteria")
    categories = (report.choices or (), report.uncertain_choices, report.not_applicable_choices)
    for category in categories:
        require_subset(category, allowed, "report choice labels must be present in question.criteria")
    require_disjoint(categories, "defect, uncertain, and not-applicable choices must be disjoint")
    require(report.expected, "choice reports cannot use expected=false")
    for level in (report.levels.warning, report.levels.error):
        require(
            level.min_probability is not None,
            "choice levels require min_probability and cannot use score thresholds",
        )
        require_none(
            (level.min_score, level.max_score, level.score_levels),
            "choice levels require min_probability and cannot use score thresholds",
        )


def _validate_noul(_question: Question, report: ReportPolicy) -> None:
    require(
        report.choices is None and not report.uncertain_choices and not report.not_applicable_choices,
        "noul reports cannot use choices or uncertain_choices",
    )
    if report.uncertain_range is not None:
        lower, upper = report.uncertain_range
        require(
            0 <= lower <= 0.5 < upper <= 1,
            "uncertain_range must enclose 0.5 with ordered boundaries in [0, 1]",
        )
    for level in (report.levels.warning, report.levels.error):
        require(level.min_probability is not None, "noul levels require min_probability")
        require_none(
            (level.min_confidence, level.min_score, level.max_score, level.score_levels),
            "noul levels cannot use confidence or score thresholds",
        )


def _validate_targeted_enrichment(rule: Rule) -> None:
    policy = rule.targeted_enrichment
    if policy is None:
        return
    require_nonempty(rule.enrichment_families, "targeted enrichment requires at least one evidence family")
    require(
        not policy.when_choices or isinstance(rule.question, ChoiceQuestion),
        "targeted enrichment choice triggers require a choice question",
    )
    allowed_choices = rule.question.criteria.keys() if isinstance(rule.question, ChoiceQuestion) else ()
    require_subset(
        policy.when_choices,
        allowed_choices,
        "targeted enrichment choices must be present in question.criteria",
    )
    require_subset(
        policy.when_reasons,
        rule.enrich_on,
        "targeted enrichment reasons must also be present in enrich_on",
    )


def _ordered_at_least(warning: ReportThreshold, error: ReportThreshold, field: str) -> None:
    lower, upper = getattr(warning, field), getattr(error, field)
    require(
        lower is None or (upper is not None and lower <= upper),
        f"error {field} must be at least as strict as warning {field}",
    )


def _validate_level_order(levels: ReportLevels) -> None:
    warning, error = levels.warning, levels.error
    mass_mode = warning.score_levels is not None or error.score_levels is not None
    if mass_mode:
        require(
            warning.score_levels is not None and error.score_levels is not None,
            "score warning and error levels must use the same threshold mode",
        )
        for field in ("min_probability", "min_confidence"):
            _ordered_at_least(warning, error, field)
        return

    require(
        (warning.min_score is None) == (error.min_score is None),
        "score levels must use the same threshold direction",
    )
    for field in ("min_probability", "min_confidence", "min_score"):
        _ordered_at_least(warning, error, field)
    require(
        warning.max_score is None or (error.max_score is not None and warning.max_score >= error.max_score),
        "error max_score must be at least as strict as warning max_score",
    )


_QUESTION_VALIDATORS = {
    ScoreQuestion: _validate_score,
    ChoiceQuestion: _validate_choice,
    NoulQuestion: _validate_noul,
}
