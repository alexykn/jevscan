"""Published Jev model limits used for conservative local admission.

These profiles are planning inputs, not tokenizer implementations. The local estimator
keeps headroom below the provider ceilings and provider rejection still remains the
final authority when estimates are wrong.
"""

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelLimits:
    context_tokens: int
    total_tokens: int


# TypeSafe/Pydantic documentation for Jev 1.13: state + longest question <= 32k,
# state + all questions <= 64k. Keep alias handling explicit so moving aliases are
# easy to update when TypeSafe ships another model.
JEV_113 = ModelLimits(context_tokens=32_000, total_tokens=64_000)
_JEV_113_VERSION = re.compile(r"jev-1\.13(?:\.0)?\Z")


def limits_for_model(model: str) -> ModelLimits | None:
    value = model.strip().lower()
    if value in {"jev-latest", "jev-preview"} or _JEV_113_VERSION.fullmatch(value):
        return JEV_113
    return None


@dataclass(slots=True)
class TokenCalibration:
    """Conservative run-local correction for the byte/token heuristic."""

    bytes_per_token: float | None = None
    observations: int = 0

    @staticmethod
    def _candidate(body_bytes: int, input_tokens: int | None) -> float | None:
        if input_tokens is None or input_tokens <= 0 or body_bytes <= 0:
            return None
        observed = body_bytes / input_tokens
        if not math.isfinite(observed) or observed <= 0:
            return None
        return max(1.0, min(8.0, observed * 0.90))

    def observe(self, body_bytes: int, input_tokens: int | None) -> None:
        candidate = self._candidate(body_bytes, input_tokens)
        if candidate is None:
            return
        # Keep ten percent headroom and only move toward more conservative estimates.
        self.bytes_per_token = candidate if self.bytes_per_token is None else min(self.bytes_per_token, candidate)
        self.observations += 1

    def effective(self, configured: float) -> float:
        return min(configured, self.bytes_per_token) if self.bytes_per_token is not None else configured
