"""Exact source spans and coverage. Compaction never changes the scored target."""

import hashlib
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from itertools import islice
from typing import Any

from jevscan.core.models import CALLABLE_KINDS, ParsedFile, Target
from jevscan.core.protocol import Check, encode

Span = tuple[int, int]


def merge_spans(spans: list[Span]) -> tuple[Span, ...]:
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class Evidence:
    state: dict[str, Any]
    encoded: bytes

    @property
    def key(self) -> str:
        # A bounding interval is insufficient for disjoint or cross-file evidence.
        return hashlib.sha256(self.encoded).hexdigest()

    def contains(self, path: str, start: int, end: int) -> bool:
        spans = merge_spans([
            (doc["start_byte"], doc["end_byte"]) for doc in self.state["documents"] if doc["path"] == path
        ])
        return any(a <= start and b >= end for a, b in spans)


class ContextBuilder:
    def __init__(self, parsed: ParsedFile) -> None:
        self.parsed = parsed
        self.file = Target.from_file(parsed)
        self.units = {unit.id: unit for unit in parsed.units}
        self.newlines = [i for i, byte in enumerate(parsed.source) if byte == 10]
        self._envelopes: OrderedDict[tuple[Span, ...], Evidence] = OrderedDict()

    def owner(self, target: Target) -> Target:
        if target.scope == "file":
            return self.file
        unit = self.units[target.id]
        if unit.kind not in CALLABLE_KINDS:
            return target
        return Target.from_unit(self.units[unit.parent_id]) if unit.parent_id else self.file

    def requested_target(self, check: Check) -> Target:
        if check.target.scope == "file" or check.rule.context == "file":
            return self.file
        if check.rule.context == "unit":
            return check.target
        return self.file if check.target.language == "rust" else self.owner(check.target)

    def requested(self, check: Check) -> Evidence:
        target = self.requested_target(check)
        spans = [(target.start_byte, target.end_byte)]
        if target.scope != "file":
            imports = (
                block
                for block in self.parsed.blocks
                if block.kind in {"import_statement", "import_from_statement", "use_declaration", "use_statement"}
            )
            spans.extend((block.start_byte, block.end_byte) for block in islice(imports, 32))
        return self.compose(spans)

    def location(self, start: int, end: int) -> dict[str, int]:
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
                omitted.append(self.location(cursor, start))
            cursor = max(cursor, end)
        if cursor < len(self.parsed.source):
            omitted.append(self.location(cursor, len(self.parsed.source)))
        return {
            "file_complete": not omitted,
            "omitted_ranges": omitted,
            "external_references": "unresolved; no cross-file contracts or caller bodies supplied",
        }

    def envelope(self, start: int, end: int) -> Evidence:
        return self.compose([(start, end)])

    def compose(self, spans: list[Span]) -> Evidence:
        key = merge_spans(spans)
        if key in self._envelopes:
            self._envelopes.move_to_end(key)
            return self._envelopes[key]
        documents = [
            dict(
                path=self.parsed.path,
                language=self.parsed.language,
                **self.location(start, end),
                content=self.parsed.source[start:end].decode("utf-8"),
            )
            for start, end in key
        ]
        state = {"documents": documents, "coverage": self.coverage(documents)}
        evidence = Evidence(state, encode(state))
        self._envelopes[key] = evidence
        if len(self._envelopes) > 8:
            self._envelopes.popitem(last=False)
        return evidence

    def describe(self, check: Check, evidence: Evidence) -> dict[str, Any]:
        target = self.requested_target(check)
        return {
            "requested": check.rule.context,
            "context_complete": evidence.contains(target.path, target.start_byte, target.end_byte),
            "target_complete": evidence.contains(check.target.path, check.target.start_byte, check.target.end_byte),
            "included_ranges": [
                {key: value for key, value in doc.items() if key != "content"} for doc in evidence.state["documents"]
            ],
            **evidence.state["coverage"],
        }
