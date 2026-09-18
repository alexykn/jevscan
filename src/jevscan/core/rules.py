"""Question and reporting contracts, validated once at the configuration boundary."""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jevscan.core.models import Kind

LANGUAGES = frozenset({"python", "rust", "perl", "typescript", "javascript"})


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


class Rule(StrictModel):
    enabled: bool = True
    target: Literal["unit", "file"] = "unit"
    context: Literal["unit", "owner", "file"] = "owner"
    applies_to: list[Kind] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: sorted(LANGUAGES))
    require_body: bool = False
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
        _validate_level_order(self.report.levels)
        return self

    def _validate_target(self) -> None:
        if self.target == "unit":
            if not self.applies_to:
                raise ValueError("unit rules require nonempty applies_to")
            return
        if self.applies_to or self.require_body or self.context != "file":
            raise ValueError("file rules require context=file, no applies_to, and require_body=false")

    def _validate_score(self) -> None:
        question, report = self.question, self.report
        assert isinstance(question, ScoreQuestion)
        if report.choices is not None or report.uncertain_choices or not report.expected:
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
        if set(report.uncertain_choices) - question.criteria.keys():
            raise ValueError("uncertain_choices must be present in question.criteria")
        if set(report.choices) & set(report.uncertain_choices):
            raise ValueError("defect choices and uncertain_choices must be disjoint")
        if not report.expected:
            raise ValueError("choice reports cannot use expected=false")
        for level in (report.levels.warning, report.levels.error):
            if level.min_probability is None or level.min_score is not None or level.max_score is not None:
                raise ValueError("choice levels require min_probability and cannot use score thresholds")

    def _validate_noul(self) -> None:
        report = self.report
        if report.choices is not None or report.uncertain_choices:
            raise ValueError("noul reports cannot use choices or uncertain_choices")
        for level in (report.levels.warning, report.levels.error):
            if level.min_probability is None:
                raise ValueError("noul levels require min_probability")
            if any(value is not None for value in (level.min_confidence, level.min_score, level.max_score)):
                raise ValueError("noul levels cannot use confidence or score thresholds")


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
