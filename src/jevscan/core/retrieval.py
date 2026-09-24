"""Bounded, lazy source snapshots and syntax-based evidence candidates.

Names are lexical hints, never proof of a call graph or an upstream guarantee.
Only discovered, non-ignored source under the project root can enter a snapshot.
"""

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jevscan.core.config import EnrichmentConfig, ScanConfig
from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.discovery import discover
from jevscan.core.models import Diagnostic, FileJob, Kind, ParsedFile, Reference, Target
from jevscan.core.parser import parse_source
from jevscan.core.protocol import Check
from jevscan.core.source_snapshot import read_source as _read_source


def _reference_targets(parsed: ParsedFile) -> Iterator[tuple[Reference, Target]]:
    """Associate occurrences with their innermost lexical unit in a single sweep."""
    units = iter(parsed.units)
    following = next(units, None)
    stack: list[Target] = []
    file = Target.from_file(parsed)
    for reference in sorted(parsed.references, key=lambda item: item.start_byte):
        while following is not None and following.start_byte <= reference.start_byte:
            while stack and following.start_byte >= stack[-1].end_byte:
                stack.pop()
            stack.append(Target.from_unit(following))
            following = next(units, None)
        while stack and reference.end_byte > stack[-1].end_byte:
            stack.pop()
        yield reference, stack[-1] if stack else file


def _test_path(path: str) -> bool:
    name = Path(path).name
    return (
        any(part in {"test", "tests", "__tests__"} for part in Path(path).parts)
        or name.startswith("test_")
        or name.endswith(("_test.py", ".t"))
        or ".test." in name
        or ".spec." in name
    )


def _impl_type_name(name: str) -> str:
    """Normalize a displayed impl to a lexical type hint, not a resolved Rust type."""
    return name.rsplit(" for ", 1)[-1].removeprefix("impl ").split("<", 1)[0].rsplit("::", 1)[-1]


@dataclass(frozen=True, slots=True)
class Snapshot:
    parsed: ParsedFile
    digest: str

    @classmethod
    def from_parsed(cls, parsed: ParsedFile) -> "Snapshot":
        return cls(parsed, hashlib.sha256(parsed.source).hexdigest())


@dataclass(frozen=True, slots=True)
class Candidate:
    snapshot: Snapshot
    target: Target
    relation: str
    reference: Reference | None = None

    @property
    def id(self) -> str:
        identity = f"{self.target.id}:{self.snapshot.digest}"
        return "c" + hashlib.sha256(identity.encode()).hexdigest()[:24]

    def document(self) -> dict[str, Any]:
        target = self.target
        return {
            "path": target.path,
            "language": target.language,
            "start_byte": target.start_byte,
            "end_byte": target.end_byte,
            "start_line": target.start_line,
            "end_line": target.end_line,
            "content": self.snapshot.parsed.source[target.start_byte : target.end_byte].decode("utf-8"),
        }

    def metadata(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "target": self.target.metadata(),
            "relation": self.relation,
            "file_sha256": self.snapshot.digest,
            "resolution": "lexical_candidate_not_resolved",
        }
        if self.reference is not None:
            result["reference"] = {
                "name": self.reference.name,
                "kind": self.reference.kind,
                "start_byte": self.reference.start_byte,
                "end_byte": self.reference.end_byte,
            }
        return result

    def model_metadata(self) -> dict[str, Any]:
        """Candidate locator safe to include in model-facing evidence state."""
        result = {
            "target": self.target.model_metadata(),
            "relation": self.relation,
            "resolution": "lexical_candidate_not_resolved",
        }
        if self.reference is not None:
            result["reference"] = {
                "name": self.reference.name,
                "kind": self.reference.kind,
                "start_line": self.snapshot.parsed.source.count(b"\n", 0, self.reference.start_byte) + 1,
            }
        return result

    def preview(self) -> dict[str, Any]:
        source = self.snapshot.parsed.source
        anchor = self.reference.start_byte if self.reference is not None else self.target.start_byte
        start = max(self.target.start_byte, anchor - 200)
        # Find a UTF-8 boundary without inventing replacement characters.
        while start < anchor and source[start] & 0xC0 == 0x80:
            start += 1
        content = source[start : self.target.end_byte].decode("utf-8")
        preview = content[:1200]
        return {
            **self.model_metadata(),
            "candidate_id": self.id,
            "preview": preview,
            "preview_start_line": source.count(b"\n", 0, start) + 1,
            "preview_complete": start == self.target.start_byte and len(content) <= 1200,
        }


@dataclass(slots=True)
class Catalogue:
    sources: dict[str, Snapshot] = field(default_factory=dict)
    callers: dict[str, list[Candidate]] = field(default_factory=lambda: defaultdict(list))
    definitions: dict[str, list[Candidate]] = field(default_factory=lambda: defaultdict(list))
    tests: dict[str, list[Candidate]] = field(default_factory=lambda: defaultdict(list))
    coverage: dict[str, Any] = field(
        default_factory=lambda: {
            "discovery_complete": True,
            "files_read": 0,
            "source_bytes": 0,
            "unreadable": 0,
            "parse_failed": 0,
            "oversized": 0,
        }
    )

    def add(self, parsed: ParsedFile) -> None:
        snapshot = Snapshot.from_parsed(parsed)
        self.sources[parsed.path] = snapshot
        for unit in parsed.units:
            target = Target.from_unit(unit)
            candidate = Candidate(snapshot, target, "possible_definition")
            self.definitions[unit.name.rsplit("::", 1)[-1]].append(candidate)
            if unit.kind == Kind.IMPL:
                # This is a lexical type hint, not Rust type/trait resolution.
                name = _impl_type_name(unit.name)
                self.definitions[name].append(Candidate(snapshot, target, "possible_sibling_impl"))
        seen: set[tuple[str, str, str]] = set()
        for reference, target in _reference_targets(parsed):
            key = reference.kind, reference.name, target.id
            if key in seen:
                continue
            seen.add(key)
            if reference.kind == "call":
                self.callers[reference.name].append(Candidate(snapshot, target, "possible_call_site", reference))
            if _test_path(parsed.path):
                self.tests[reference.name].append(Candidate(snapshot, target, "test_reference", reference))

    def record_gap(self, reason: str) -> None:
        self.coverage[reason] += 1
        self.coverage["discovery_complete"] = False

    def related(self, context: ContextBuilder, target: Target, route: str) -> list[Candidate]:
        parsed = context.parsed
        primary_names = (
            {context.units[target.id].name.rsplit("::", 1)[-1]}
            if target.scope == "unit"
            else {unit.name.rsplit("::", 1)[-1] for unit in parsed.units}
        )
        if route != "definitions":
            pool = self.callers if route == "callers" else self.tests
            return [candidate for name in sorted(primary_names) for candidate in pool.get(name, ())]
        used_names = {
            reference.name
            for reference in parsed.references
            if target.start_byte <= reference.start_byte < target.end_byte
        }
        owner = context.owner(target)
        if target.language == "rust" and owner.kind == Kind.IMPL:
            used_names.add(_impl_type_name(context.units[owner.id].name))
        return [
            candidate for name in sorted(used_names - primary_names) for candidate in self.definitions.get(name, ())
        ]


def _index_source(root: Path, entry: FileJob, scan: ScanConfig, byte_limit: int, result: Catalogue) -> None:
    """Read and admit one source snapshot, recording why it cannot enter the index."""
    result.coverage["files_read"] += 1
    try:
        source = _read_source(root, entry.display_path, byte_limit)
        result.coverage["source_bytes"] += len(source)
        if len(source) > byte_limit:
            result.record_gap("oversized")
            return
        parsed = parse_source(source, entry, scan.max_units_per_file)
    except (OSError, UnicodeError):
        result.record_gap("unreadable")
        return
    if parsed.failed:
        result.record_gap("parse_failed")
        return
    result.add(parsed)


def _catalogue(root: Path, scan: ScanConfig, limits: EnrichmentConfig) -> Catalogue:
    result = Catalogue()
    iterator = discover([root], root, scan)
    try:
        for entry in iterator:
            if isinstance(entry, Diagnostic):
                result.record_gap("unreadable")
                continue
            remaining = limits.max_source_bytes - result.coverage["source_bytes"]
            if result.coverage["files_read"] >= limits.max_source_files or remaining <= 0:
                result.coverage["discovery_complete"] = False
                break
            _index_source(root, entry, scan, min(scan.max_file_bytes, remaining), result)
    finally:
        iterator.close()
    return result


@dataclass(frozen=True, slots=True)
class Candidates:
    items: tuple[Candidate, ...]
    coverage: dict[str, Any]


class SourceIndex:
    """One bounded lazy snapshot per invocation, shared by fixed file evaluators."""

    def __init__(self, root: Path, scan: ScanConfig, limits: EnrichmentConfig) -> None:
        self.root, self.scan, self.limits = root.resolve(), scan, limits
        self._catalogue: Catalogue | None = None
        self._lock = asyncio.Lock()

    async def _load(self) -> Catalogue:
        async with self._lock:
            if self._catalogue is None:
                task = asyncio.create_task(asyncio.to_thread(_catalogue, self.root, self.scan, self.limits))
                try:
                    self._catalogue = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await asyncio.gather(task, return_exceptions=True)
                    raise
        return self._catalogue

    async def candidates(self, context: ContextBuilder, check: Check, evidence: Evidence, route: str) -> Candidates:
        parsed, target = context.parsed, check.target
        current = Snapshot.from_parsed(parsed)
        if route == "enclosing_context":
            available = [
                Candidate(current, context.owner(target), "enclosing_owner"),
                Candidate(current, context.file, "containing_file"),
            ]
            coverage: dict[str, Any] = {"scope": "current_file", "discovery_complete": True}
        else:
            catalogue = await self._load()
            available = catalogue.related(context, target, route)
            indexed = catalogue.sources.get(parsed.path)
            coverage = {
                **catalogue.coverage,
                "scope": "project_root",
                "primary_snapshot_changed": indexed is not None and indexed.digest != current.digest,
            }
        # Never mix an indexed revision of this file with its authoritative scan snapshot.
        available = [
            candidate
            for candidate in available
            if candidate.target.language == target.language
            and (candidate.target.path != parsed.path or candidate.snapshot.digest == current.digest)
        ]
        unique = {candidate.id: candidate for candidate in available if not _already_present(candidate, evidence)}
        ordered = sorted(
            unique.values(),
            key=lambda candidate: (
                candidate.target.path != parsed.path,
                Path(candidate.target.path).parent != Path(parsed.path).parent,
                candidate.target.end_byte - candidate.target.start_byte,
                candidate.target.path,
                candidate.target.start_byte,
            ),
        )
        return Candidates(
            tuple(ordered[: self.limits.max_candidates]),
            {
                **coverage,
                "matched_candidates": len(ordered),
                "candidate_limit_omissions": max(0, len(ordered) - self.limits.max_candidates),
            },
        )


def _already_present(candidate: Candidate, evidence: Evidence) -> bool:
    target = candidate.target
    return any(
        document["path"] == target.path
        and document["start_byte"] <= target.start_byte
        and document["end_byte"] >= target.end_byte
        for document in evidence.state["documents"]
    )
