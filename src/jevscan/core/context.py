"""Exact, possibly non-contiguous source evidence; source bytes remain authoritative."""

import hashlib
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from jevscan.core.models import CALLABLE_KINDS, ParsedFile, Target
from jevscan.core.protocol import Check, encode

Span = tuple[int, int]


def merge_spans(spans: list[Span]) -> tuple[Span, ...]:
    result: list[Span] = []
    for start, end in sorted(spans):
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Evidence:
    start: int
    end: int
    state: dict[str, Any]
    encoded: bytes

    @property
    def key(self) -> str:
        # Different selections with the same bounding span must never share an identity.
        return hashlib.sha256(self.encoded).hexdigest()


class ContextBuilder:
    def __init__(self, parsed: ParsedFile) -> None:
        self.parsed = parsed
        self.file = Target.from_file(parsed)
        self.units = {unit.id: unit for unit in parsed.units}
        self.newlines = [i for i, byte in enumerate(parsed.source) if byte == 10]
        self.references = sorted(parsed.references, key=lambda ref: ref.start_byte)
        self.reference_positions = [ref.start_byte for ref in self.references]
        self._envelopes: OrderedDict[Span, Evidence] = OrderedDict()

    def names_in(self, start: int, end: int) -> set[str]:
        first = bisect_left(self.reference_positions, start)
        last = bisect_left(self.reference_positions, end)
        return {ref.name for ref in self.references[first:last]}

    def owner(self, target: Target) -> Target:
        if target.scope == "file":
            return self.file
        unit = self.units[target.id]
        if unit.kind not in CALLABLE_KINDS:
            return target
        return Target.from_unit(self.units[unit.parent_id]) if unit.parent_id else self.file

    def requested(self, check: Check) -> Evidence:
        target = check.target
        if target.scope == "file" or check.rule.context == "file":
            target = self.file
        elif check.rule.context == "owner":
            target = self.file if target.language == "rust" else self.owner(target)
        return self.envelope(target.start_byte, target.end_byte)

    def minimum(self, check: Check) -> Evidence:
        return self.envelope(check.target.start_byte, check.target.end_byte)

    def _range(self, start: int, end: int) -> dict[str, int]:
        return {
            "start_byte": start,
            "end_byte": end,
            "start_line": bisect_left(self.newlines, start) + 1,
            "end_line": bisect_left(self.newlines, max(start, end - 1)) + 1,
        }

    def coverage(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        spans = merge_spans([
            (doc["start_byte"], doc["end_byte"]) for doc in documents if doc["path"] == self.parsed.path
        ])
        omitted = []
        cursor = 0
        for start, end in spans:
            if start > cursor:
                omitted.append(self._range(cursor, start))
            cursor = max(cursor, end)
        if cursor < len(self.parsed.source):
            omitted.append(self._range(cursor, len(self.parsed.source)))
        return {
            "file_complete": not omitted,
            "omitted_ranges": omitted,
            "external_references": "unresolved; no cross-file contracts or caller bodies supplied",
        }

    def assemble(self, spans: list[Span]) -> Evidence:
        merged = merge_spans(spans)
        assert merged, "evidence needs at least the complete target"
        documents = [
            {
                "path": self.parsed.path,
                "language": self.parsed.language,
                **self._range(start, end),
                "content": self.parsed.source[start:end].decode("utf-8"),
            }
            for start, end in merged
        ]
        state: dict[str, Any] = {"documents": documents, "coverage": self.coverage(documents)}
        return Evidence(merged[0][0], merged[-1][1], state, encode(state))

    def envelope(self, start: int, end: int) -> Evidence:
        key = start, end
        if key in self._envelopes:
            self._envelopes.move_to_end(key)
            return self._envelopes[key]
        evidence = self.assemble([key])
        self._envelopes[key] = evidence
        if len(self._envelopes) > 8:
            self._envelopes.popitem(last=False)
        return evidence

    def contains(self, evidence: Evidence, start: int, end: int) -> bool:
        spans = merge_spans([
            (doc["start_byte"], doc["end_byte"])
            for doc in evidence.state["documents"]
            if doc["path"] == self.parsed.path
        ])
        return any(a <= start and b >= end for a, b in spans)

    def describe(self, check: Check, evidence: Evidence) -> dict[str, Any]:
        requested = self.requested(check)
        complete = self.contains(evidence, check.target.start_byte, check.target.end_byte)
        assert complete, "an assessment must retain its complete target"
        return {
            "requested": check.rule.context,
            "context_complete": self.contains(evidence, requested.start, requested.end),
            "target_complete": complete,
            "included_ranges": [
                {key: value for key, value in doc.items() if key != "content"} for doc in evidence.state["documents"]
            ],
            **evidence.state["coverage"],
        }
