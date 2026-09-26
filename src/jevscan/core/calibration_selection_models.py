"""Public calibration-selection result and audit models."""

from dataclasses import dataclass
from typing import Any

from jevscan.core.calibration_candidates import policy_hash
from jevscan.core.calibration_compatibility import CompatibilityCheck, SelectionCompatibility
from jevscan.core.calibration_scoring import CandidateMetrics, SelectionObjective
from jevscan.core.rules import ReportPolicy

SELECTION_VERSION = 1


def _policy_document(policy: ReportPolicy) -> dict[str, Any]:
    return policy.model_dump(mode="json")


def _optional_policy(policy: ReportPolicy | None) -> tuple[dict[str, Any] | None, str | None]:
    if policy is None:
        return None, None
    return _policy_document(policy), policy_hash(policy)


def _optional_metrics(metrics: CandidateMetrics | None) -> dict[str, Any] | None:
    return metrics.as_dict() if metrics is not None else None


def _optional_compatibility(compatibility: CompatibilityCheck | None) -> dict[str, Any] | None:
    return compatibility.as_dict() if compatibility is not None else None


@dataclass(frozen=True, slots=True)
class RuleSelection:
    rule_id: str
    selected_policy: ReportPolicy | None
    baseline_policy: ReportPolicy | None
    reason: str
    candidates: tuple[CandidateMetrics, ...]
    development: CandidateMetrics | None
    heldout: dict[str, Any] | None
    rule_mismatches: tuple[dict[str, Any], ...] = ()
    search: dict[str, Any] | None = None
    selected_development: CandidateMetrics | None = None
    compatibility: CompatibilityCheck | None = None

    def as_dict(self) -> dict[str, Any]:
        selected, selected_hash = _optional_policy(self.selected_policy)
        baseline, baseline_hash = _optional_policy(self.baseline_policy)
        development = _optional_metrics(self.development)
        return {
            "rule_id": self.rule_id,
            "reason": self.reason,
            "selected_policy": selected,
            "selected_policy_hash": selected_hash,
            "baseline_policy": baseline,
            "baseline_policy_hash": baseline_hash,
            "development": development,
            "baseline_development": development,
            "selected_development": _optional_metrics(self.selected_development),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "heldout": self.heldout,
            "rule_mismatches": list(self.rule_mismatches),
            "search": self.search,
            "compatibility": _optional_compatibility(self.compatibility),
        }


@dataclass(frozen=True, slots=True)
class SelectionAudit:
    development_split: str
    heldout_split: str | None
    objective: SelectionObjective
    rules: dict[str, RuleSelection]
    compatibility: SelectionCompatibility

    @property
    def selected_policies(self) -> dict[str, ReportPolicy]:
        return {
            rule_id: result.selected_policy
            for rule_id, result in self.rules.items()
            if result.selected_policy is not None
        }

    def as_dict(self) -> dict[str, Any]:
        selected = {
            rule_id: {
                "policy": _policy_document(policy),
                "policy_hash": policy_hash(policy),
            }
            for rule_id, policy in sorted(self.selected_policies.items())
        }
        return {
            "version": SELECTION_VERSION,
            "development_split": self.development_split,
            "heldout_split": self.heldout_split,
            "objective": self.objective.as_dict(),
            "compatibility": self.compatibility.as_dict(),
            "selected": selected,
            "rules": {rule_id: result.as_dict() for rule_id, result in sorted(self.rules.items())},
        }


def selected_policy_document(audit: SelectionAudit) -> dict[str, Any]:
    """Return the mapping accepted by the existing ``--report-policy`` loader."""
    return {
        "rules": {
            rule_id: policy.model_dump(mode="json", exclude_none=True)
            for rule_id, policy in sorted(audit.selected_policies.items())
        }
    }
