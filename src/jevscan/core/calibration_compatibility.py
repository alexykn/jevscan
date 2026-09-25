"""Model/prompt compatibility rules for calibration selection."""

from dataclasses import dataclass
from typing import Any, Iterable

from jevscan.core.calibration_cases import CalibrationCase

@dataclass(frozen=True, slots=True)
class CompatibilityAuthority:
    """Frozen metadata authority for one selection scope."""

    returned_model: str
    prompt_version: int
    prompt_policy: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_identity": "returned_model",
            "returned_model": self.returned_model,
            "prompt": {
                "version": self.prompt_version,
                "policy": self.prompt_policy,
            },
        }


@dataclass(frozen=True, slots=True)
class CompatibilityCheck:
    """Selection-only compatibility evidence for one set of cases."""

    status: str
    authority: CompatibilityAuthority | None
    checked_case_ids: tuple[str, ...]
    mismatches: tuple[dict[str, Any], ...] = ()

    @property
    def incompatible_case_ids(self) -> tuple[str, ...]:
        return tuple(sorted({str(item["case_id"]) for item in self.mismatches}))

    @property
    def has_missing_metadata(self) -> bool:
        return any(item.get("reason") == "missing_metadata" for item in self.mismatches)

    @property
    def incompatible(self) -> bool:
        return bool(self.mismatches)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "authority": self.authority.as_dict() if self.authority is not None else None,
            "checked_case_ids": list(self.checked_case_ids),
            "rejected_case_ids": list(self.incompatible_case_ids),
            "mismatches": list(self.mismatches),
        }


@dataclass(frozen=True, slots=True)
class SelectionCompatibility:
    """Compatibility policy recorded for a complete requested fit."""

    allow_incompatible_model_prompt: bool
    development: CompatibilityCheck

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": (
                "allow_incompatible_model_prompt" if self.allow_incompatible_model_prompt else "strict_model_prompt"
            ),
            "model_identity": "returned_model",
            "prompt_identity": ["version", "policy"],
            "requested_model": "not_used_for_compatibility",
            "allow_incompatible_model_prompt": self.allow_incompatible_model_prompt,
            "development": self.development.as_dict(),
        }


def compatibility_reason(check: CompatibilityCheck, scope: str) -> str:
    if check.has_missing_metadata:
        return f"{scope}_compatibility_metadata_missing"
    fields = {str(item["field"]) for item in check.mismatches}
    model = "returned_model" in fields
    prompt = bool(fields & {"prompt.version", "prompt.policy"})
    if model and not prompt:
        return f"{scope}_model_mismatch"
    if prompt and not model:
        return f"{scope}_prompt_mismatch"
    return f"{scope}_model_prompt_mismatch"



def support_key(case: CalibrationCase) -> str:
    """Return only an explicit, stable provenance grouping contract."""

    for name in ("source_group", "scenario_group", "owner_group"):
        value = case.provenance.get(name)
        if isinstance(value, str) and value.strip():
            return f"{name}:{value}"
    return ""


def _compatibility_values(case: CalibrationCase) -> tuple[str | None, int | None, str | None]:
    returned_model = (
        case.returned_model if isinstance(case.returned_model, str) and case.returned_model.strip() else None
    )
    prompt_version = case.prompt.version if isinstance(case.prompt.version, int) and case.prompt.version >= 1 else None
    prompt_policy = case.prompt.policy if isinstance(case.prompt.policy, str) and case.prompt.policy.strip() else None
    return returned_model, prompt_version, prompt_policy


def _missing_compatibility_mismatches(case: CalibrationCase) -> list[dict[str, Any]]:
    returned_model, prompt_version, prompt_policy = _compatibility_values(case)
    missing: list[dict[str, Any]] = []
    for field, actual in (
        ("returned_model", returned_model),
        ("prompt.version", prompt_version),
        ("prompt.policy", prompt_policy),
    ):
        if actual is None:
            missing.append({
                "case_id": case.case_id,
                "field": field,
                "reason": "missing_metadata",
                "actual": None,
            })
    return missing


def _partition_compatibility_cases(
    cases: Iterable[CalibrationCase],
) -> tuple[list[CalibrationCase], tuple[dict[str, Any], ...]]:
    complete: list[CalibrationCase] = []
    missing: list[dict[str, Any]] = []
    for case in cases:
        case_missing = _missing_compatibility_mismatches(case)
        if case_missing:
            missing.extend(case_missing)
        else:
            complete.append(case)
    return complete, tuple(missing)


def _compatibility_mismatches(
    authority: CompatibilityAuthority,
    case: CalibrationCase,
) -> list[dict[str, Any]]:
    returned_model, prompt_version, prompt_policy = _compatibility_values(case)
    actual_values = {
        "returned_model": returned_model,
        "prompt.version": prompt_version,
        "prompt.policy": prompt_policy,
    }
    expected_values = {
        "returned_model": authority.returned_model,
        "prompt.version": authority.prompt_version,
        "prompt.policy": authority.prompt_policy,
    }
    return [
        {
            "case_id": case.case_id,
            "field": field,
            "expected": expected,
            "actual": actual_values[field],
        }
        for field, expected in expected_values.items()
        if actual_values[field] is None or actual_values[field] != expected
    ]


def compatibility_check(cases: Iterable[CalibrationCase]) -> CompatibilityCheck:
    material = sorted(cases, key=lambda case: (case.case_id, case.split))
    checked_case_ids = tuple(case.case_id for case in material)
    if not material:
        return CompatibilityCheck("no_cases", None, checked_case_ids)
    complete, missing = _partition_compatibility_cases(material)
    if not complete:
        return CompatibilityCheck("missing_metadata", None, checked_case_ids, missing)
    first = complete[0]
    returned_model, prompt_version, prompt_policy = _compatibility_values(first)
    assert returned_model is not None and prompt_version is not None and prompt_policy is not None
    authority = CompatibilityAuthority(returned_model, prompt_version, prompt_policy)
    mismatches = missing + tuple(
        mismatch for case in complete for mismatch in _compatibility_mismatches(authority, case)
    )
    status = "missing_metadata" if missing else "compatible" if not mismatches else "mismatch"
    return CompatibilityCheck(status, authority, checked_case_ids, mismatches)


def compatibility_check_against(
    authority: CompatibilityAuthority | None,
    cases: Iterable[CalibrationCase],
) -> CompatibilityCheck:
    material = sorted(cases, key=lambda case: (case.case_id, case.split))
    checked_case_ids = tuple(case.case_id for case in material)
    if authority is None:
        return CompatibilityCheck("no_development_authority", None, checked_case_ids)
    complete, missing = _partition_compatibility_cases(material)
    mismatches = missing + tuple(
        mismatch for case in complete for mismatch in _compatibility_mismatches(authority, case)
    )
    status = "missing_metadata" if missing else "compatible" if not mismatches else "mismatch"
    return CompatibilityCheck(status, authority, checked_case_ids, mismatches)


