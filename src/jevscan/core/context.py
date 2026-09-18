"""Exact source envelopes. Wider evidence never changes the identity of a target."""

from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from jevscan.core.models import CALLABLE_KINDS, ParsedFile, Target
from jevscan.core.protocol import Check, encode


@dataclass(frozen=True, slots=True)
class Evidence:
    start: int
    end: int
    state: dict[str, Any]
    encoded: bytes

    @property
    def key(self) -> tuple[int, int]:
        return self.start, self.end


class ContextBuilder:
    def __init__(self, parsed: ParsedFile) -> None:
        self.parsed = parsed
        self.file = Target.from_file(parsed)
        self.units = {unit.id: unit for unit in parsed.units}
        self.newlines = [i for i, byte in enumerate(parsed.source) if byte == 10]
        self._envelopes: OrderedDict[tuple[int, int], Evidence] = OrderedDict()

    def owner(self, target: Target) -> Target:
        if target.scope == "file":
            return self.file
        unit = self.units[target.id]
        if unit.kind not in CALLABLE_KINDS:
            return target
        return Target.from_unit(self.units[unit.parent_id]) if unit.parent_id else self.file

    def variants(self, check: Check) -> Iterator[Evidence]:
        target = check.target
        if target.scope == "file":
            choices = (self.file,)
        elif check.rule.context == "unit":
            choices = (target,)
        elif check.rule.context == "owner" and target.language != "rust":
            choices = (self.owner(target), target)
        else:
            # Rust struct/enum declarations and impls are siblings. Prefer the file;
            # retain the lexical impl as the next fallback, without claiming resolution.
            choices = (self.file, self.owner(target), target)
        spans = dict.fromkeys((choice.start_byte, choice.end_byte) for choice in choices)
        for start, end in spans:
            yield self.envelope(start, end)

    def _range(self, start: int, end: int) -> dict[str, int]:
        return {
            "start_byte": start,
            "end_byte": end,
            "start_line": bisect_left(self.newlines, start) + 1,
            "end_line": bisect_left(self.newlines, max(start, end - 1)) + 1,
        }

    def coverage(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        spans = sorted(
            (document["start_byte"], document["end_byte"])
            for document in documents
            if document["path"] == self.parsed.path
        )
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

    def envelope(self, start: int, end: int) -> Evidence:
        key = start, end
        if key in self._envelopes:
            self._envelopes.move_to_end(key)
            return self._envelopes[key]
        parsed = self.parsed
        document = {
            "path": parsed.path,
            "language": parsed.language,
            **self._range(start, end),
            "content": parsed.source[start:end].decode("utf-8"),
        }
        state = {
            "documents": [document],
            "coverage": self.coverage([document]),
        }
        if not state["coverage"]["file_complete"]:
            state["declarations"] = {
                "items": parsed.declarations,
                "scope": "bounded same-file import snippets, not resolved definitions",
            }
        evidence = Evidence(start, end, state, encode(state))
        # Bound retained copies for deeply nested owners; source bytes remain authoritative.
        self._envelopes[key] = evidence
        if len(self._envelopes) > 8:
            self._envelopes.popitem(last=False)
        return evidence

    def describe(self, check: Check, evidence: Evidence) -> dict[str, Any]:
        requested = next(self.variants(check))
        return {
            "requested": check.rule.context,
            "context_complete": any(
                document["path"] == check.target.path
                and document["start_byte"] <= requested.start
                and document["end_byte"] >= requested.end
                for document in evidence.state["documents"]
            ),
            "target_complete": True,
            "included_ranges": [
                {key: value for key, value in doc.items() if key != "content"} for doc in evidence.state["documents"]
            ],
            **evidence.state["coverage"],
        }
