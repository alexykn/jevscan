"""Question and reporting contracts, validated once at the configuration boundary."""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jevscan.core.models import Kind, SyntaxFact

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
        if not self.requires_any and not self.requires_all:
            raise ValueError("applicability requires at least one syntax fact")
        if len(self.requires_any) != len(set(self.requires_any)) or len(self.requires_all) != len(
            set(self.requires_all)
        ):
            raise ValueError("applicability fact lists must not contain duplicates")
        if set(self.requires_any) & set(self.requires_all):
            raise ValueError("a syntax fact cannot be both requires_any and requires_all")
        return self


class TargetedEnrichmentPolicy(StrictModel):
    when_choices: list[str] = Field(default_factory=list)
    when_reasons: list[EnrichmentTrigger] = Field(default_factory=list)

    @model_validator(mode="after")
    def has_trigger(self) -> Self:
        if not self.when_choices and not self.when_reasons:
            raise ValueError("targeted enrichment requires a choice or assessment reason trigger")
        if len(self.when_choices) != len(set(self.when_choices)) or len(self.when_reasons) != len(
            set(self.when_reasons)
        ):
            raise ValueError("targeted enrichment trigger lists must not contain duplicates")
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


class ReportLevels(StrictModel):
    warning: ReportThreshold
    error: ReportThreshold


class ReportPolicy(StrictModel):
    message: str = Field(min_length=1)
    levels: ReportLevels
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
        self._validate_target()
        if isinstance(self.question, ScoreQuestion):
            self._validate_score()
        elif isinstance(self.question, ChoiceQuestion):
            self._validate_choice()
        else:
            self._validate_noul()
        self._validate_targeted_enrichment()
        if not isinstance(self.question, NoulQuestion) and self.report.uncertain_range is not None:
            raise ValueError("uncertain_range is only valid for noul questions")
        _validate_level_order(self.report.levels)
        return self

    def _validate_target(self) -> None:
        if self.target == "unit":
            if not self.applies_to:
                raise ValueError("unit rules require nonempty applies_to")
            return
        if self.applies_to or self.require_body or self.require_members or self.context != "file":
            raise ValueError("file rules require context=file, no applies_to, and require_body=false")

    def _validate_score(self) -> None:
        question, report = self.question, self.report
        assert isinstance(question, ScoreQuestion)
        if (
            report.choices is not None
            or report.uncertain_choices
            or report.not_applicable_choices
            or not report.expected
        ):
            raise ValueError("score reports cannot use choices, uncertain_choices, or expected=false")
        for level in (report.levels.warning, report.levels.error):
            if (level.min_score is None) == (level.max_score is None):
                raise ValueError("score levels require exactly one of min_score or max_score")
            threshold = level.min_score if level.min_score is not None else level.max_score
            assert threshold is not None
            if not 0 <= threshold <= len(question.criteria) - 1:
                raise ValueError("score threshold is outside the rubric")
            if level.min_probability is not None:
                raise ValueError("score levels cannot use min_probability")

    def _validate_choice(self) -> None:
        question, report = self.question, self.report
        assert isinstance(question, ChoiceQuestion)
        if not report.choices or set(report.choices) - question.criteria.keys():
            raise ValueError("choice reports require choices present in question.criteria")
        categories = (set(report.choices), set(report.uncertain_choices), set(report.not_applicable_choices))
        if any(category - question.criteria.keys() for category in categories):
            raise ValueError("report choice labels must be present in question.criteria")
        if any(categories[i] & categories[j] for i in range(3) for j in range(i + 1, 3)):
            raise ValueError("defect, uncertain, and not-applicable choices must be disjoint")
        if not report.expected:
            raise ValueError("choice reports cannot use expected=false")
        for level in (report.levels.warning, report.levels.error):
            if level.min_probability is None or level.min_score is not None or level.max_score is not None:
                raise ValueError("choice levels require min_probability and cannot use score thresholds")

    def _validate_noul(self) -> None:
        report = self.report
        if report.choices is not None or report.uncertain_choices or report.not_applicable_choices:
            raise ValueError("noul reports cannot use choices or uncertain_choices")
        if report.uncertain_range is not None:
            lower, upper = report.uncertain_range
            if not 0 <= lower <= 0.5 < upper <= 1:
                raise ValueError("uncertain_range must enclose 0.5 with ordered boundaries in [0, 1]")
        for level in (report.levels.warning, report.levels.error):
            if level.min_probability is None:
                raise ValueError("noul levels require min_probability")
            if any(value is not None for value in (level.min_confidence, level.min_score, level.max_score)):
                raise ValueError("noul levels cannot use confidence or score thresholds")

    def _validate_targeted_enrichment(self) -> None:
        if self.targeted_enrichment is None:
            return
        if not self.enrichment_families:
            raise ValueError("targeted enrichment requires at least one evidence family")
        if self.targeted_enrichment.when_choices and not isinstance(self.question, ChoiceQuestion):
            raise ValueError("targeted enrichment choice triggers require a choice question")
        if isinstance(self.question, ChoiceQuestion):
            unknown = set(self.targeted_enrichment.when_choices) - set(self.question.criteria)
            if unknown:
                raise ValueError("targeted enrichment choices must be present in question.criteria")
        unavailable = set(self.targeted_enrichment.when_reasons) - set(self.enrich_on)
        if unavailable:
            raise ValueError("targeted enrichment reasons must also be present in enrich_on")


def _validate_level_order(levels: ReportLevels) -> None:
    warning, error = levels.warning, levels.error
    if (warning.min_score is None) != (error.min_score is None):
        raise ValueError("score levels must use the same threshold direction")
    for field in ("min_probability", "min_confidence", "min_score"):
        lower, upper = getattr(warning, field), getattr(error, field)
        if lower is not None and (upper is None or lower > upper):
            raise ValueError(f"error {field} must be at least as strict as warning {field}")
    if warning.max_score is not None and (error.max_score is None or warning.max_score < error.max_score):
        raise ValueError("error max_score must be at least as strict as warning max_score")
