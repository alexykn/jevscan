"""Merge supplemental source candidates into exact-span evidence."""

import hashlib
from collections import defaultdict
from typing import Any, Iterable

from jevscan.core.context import ContextBuilder, Evidence
from jevscan.core.protocol import encode
from jevscan.core.retrieval import Candidate


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return merged


def _source_ranges(
    context: ContextBuilder,
    initial: Evidence,
    candidates: list[Candidate],
) -> tuple[dict[str, bytes], dict[str, str], dict[str, list[tuple[int, int]]]]:
    sources = {context.parsed.path: context.parsed.source}
    languages: dict[str, str] = {}
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for document in initial.state["documents"]:
        path = document["path"]
        languages[path] = document["language"]
        ranges[path].append((document["start_byte"], document["end_byte"]))

    for candidate in candidates:
        target = candidate.target
        sources[target.path] = candidate.snapshot.parsed.source
        languages[target.path] = target.language
        ranges[target.path].append((target.start_byte, target.end_byte))

    return sources, languages, ranges


def _documents(
    sources: dict[str, bytes],
    languages: dict[str, str],
    ranges: dict[str, list[tuple[int, int]]],
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path, spans in sorted(ranges.items()):
        source = sources[path]
        digest = hashlib.sha256(source).hexdigest()
        for start, end in _merge_spans(spans):
            documents.append({
                "path": path,
                "language": languages[path],
                "start_byte": start,
                "end_byte": end,
                "start_line": source.count(b"\n", 0, start) + 1,
                "end_line": source.count(b"\n", 0, max(start, end - 1)) + 1,
                "content": source[start:end].decode("utf-8"),
                "file_sha256": digest,
            })
    return documents


def augment_evidence(
    context: ContextBuilder,
    initial: Evidence,
    candidates: list[Candidate],
    retrieval: dict[str, Any],
) -> Evidence:
    """Union overlapping exact source spans without fabricating source."""
    sources, languages, ranges = _source_ranges(context, initial, candidates)
    documents = _documents(sources, languages, ranges)
    state = {
        **initial.state,
        "documents": documents,
        "coverage": {
            **context.coverage(documents),
            "external_references": (
                "Selected syntax/name-based candidates only; no complete or resolved call graph. "
                "Sources are per-file snapshots, not an atomic repository snapshot."
            ),
        },
        "supplemental_evidence": [candidate.model_metadata() for candidate in candidates],
        "retrieval_coverage": retrieval,
    }
    return Evidence(state, encode(state))
