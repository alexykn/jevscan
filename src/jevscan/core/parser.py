"""Native Tree-sitter parsing and normalization into stable lexical code units."""

import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from jevscan.core.languages import SPECS
from jevscan.core.models import Diagnostic, FileJob, Kind, ParsedFile, Reference, Severity, Unit
from jevscan.core.syntax import callback_label, source_blocks
from jevscan.core.syntax_facts import finalize_units, has_implementation, references, syntax_facts
from jevscan.core.syntax_recovery import (
    TYPE_SCRIPT_GRAMMARS,
    errors_outside_executable_bodies,
    first_error_line,
    parse_errors,
    recoverable_type_errors,
)
from jevscan.core.syntax_symbols import Symbol, capture_symbols, node_text


class ParserUnavailableError(RuntimeError):
    """Missing or incompatible Tree-sitter runtime/grammar installation."""


def require_parser_runtime() -> None:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_language_pack  # noqa: F401
    except ImportError as exc:
        raise ParserUnavailableError(
            "Tree-sitter dependencies are missing; install the project with 'uv sync'"
        ) from exc


@lru_cache(maxsize=6)
def _frontend(grammar: str) -> tuple[Any, Any]:
    from tree_sitter import Query
    from tree_sitter_language_pack import SupportedLanguage, get_language, get_parser

    spec = SPECS[grammar]
    language_name = cast(SupportedLanguage, grammar)
    language = get_language(language_name)
    available = {
        language.node_kind_for_id(i) for i in range(language.node_kind_count) if language.node_kind_is_named(i)
    }
    missing = spec.required - available
    if missing:
        raise ParserUnavailableError(f"{grammar} grammar lacks required syntax nodes: {', '.join(sorted(missing))}")
    patterns = []
    for capture, names in (("unit", spec.nodes), ("import", spec.imports), ("branch", spec.branches)):
        supported = [name for name in names if name in available]
        if supported:
            patterns.append("[" + " ".join(f"({name})" for name in supported) + f"] @{capture}")
    return get_parser(language_name), Query(language, "\n".join(patterns))


def _unit_kind(symbol: Symbol, parent: Unit | None) -> Kind:
    kind = symbol.kind
    if kind == Kind.FUNCTION and parent and parent.kind in {Kind.CLASS, Kind.IMPL, Kind.TRAIT, Kind.INTERFACE}:
        return Kind.METHOD
    if kind == Kind.CLOSURE and symbol.node.type != "async_block" and symbol.bound_kind is not None:
        return symbol.bound_kind
    return kind


def _qualified_name(symbol: Symbol, kind: Kind, language: str, parent_name: str | None, label: str) -> str:
    if language == "perl" and (kind in {Kind.PACKAGE, Kind.CLASS} or "::" in symbol.name):
        return symbol.name
    parts = [parent_name] if parent_name is not None else []
    if symbol.outer_binding is not None:
        parts.append(symbol.outer_binding)
    parts.append(label)
    return ("::" if language in {"rust", "perl"} else ".").join(parts)


def _signature(symbol: Symbol, source: bytes) -> str:
    end = symbol.body.start_byte if symbol.body is not None else symbol.node.end_byte
    signature = source[symbol.start : end].decode("utf-8").strip()
    # Only display metadata is abbreviated, never the analyzed source span.
    return signature[:1024] + " [signature abbreviated]" if len(signature) > 1024 else signature


@dataclass(frozen=True, slots=True)
class _UnitBuilder:
    source: bytes
    job: FileJob
    newlines: list[int]
    branches: list[int]

    def build(self, symbol: Symbol, parent: Unit | None) -> Unit:
        kind = _unit_kind(symbol, parent)
        label = callback_label(symbol.node, self.source) if symbol.name.startswith("<anonymous@") else symbol.name
        qualified = _qualified_name(
            symbol, kind, self.job.language, parent.qualified_name if parent else None, symbol.name
        )
        display = _qualified_name(symbol, kind, self.job.language, parent.display_name if parent else None, label)
        return Unit(
            id=f"{self.job.display_path}:{symbol.start}:{kind}",
            path=self.job.display_path,
            language=self.job.language,
            kind=kind,
            name=symbol.name,
            qualified_name=qualified,
            parent_id=parent.id if parent else None,
            start_byte=symbol.start,
            end_byte=symbol.end,
            start_line=bisect_left(self.newlines, symbol.start) + 1,
            end_line=bisect_left(self.newlines, max(symbol.start, symbol.end - 1)) + 1,
            signature=_signature(symbol, self.source),
            has_body=symbol.body is not None,
            display_name=display,
            body_start_byte=symbol.body.start_byte if symbol.body is not None else None,
            body_end_byte=symbol.body.end_byte if symbol.body is not None else None,
            has_implementation=has_implementation(symbol, self.job.language),
            branch_nodes=bisect_left(self.branches, symbol.end) - bisect_left(self.branches, symbol.start),
            syntax_facts=syntax_facts(symbol, self.source, self.job.language),
        )


def _normalize(
    symbols: list[Symbol],
    source: bytes,
    job: FileJob,
    branches: list[int],
    occurrences: tuple[Reference, ...],
) -> tuple[Unit, ...]:
    # A single interval stack preserves lexical parentage without a quadratic search.
    symbols.sort(key=lambda item: (item.start, -item.end))
    builder = _UnitBuilder(source, job, [match.start() for match in re.finditer(b"\n", source)], branches)
    units: list[Unit] = []
    stack: list[Unit] = []
    member_counts: dict[str, int] = defaultdict(int)
    for symbol in symbols:
        while stack and not (
            stack[-1].start_byte <= symbol.start < stack[-1].end_byte and symbol.end <= stack[-1].end_byte
        ):
            stack.pop()
        parent = stack[-1] if stack else None
        unit = builder.build(symbol, parent)
        if parent:
            member_counts[parent.id] += 1
        units.append(unit)
        stack.append(unit)
    return finalize_units(units, member_counts, occurrences)


def _failed_file(job: FileJob, diagnostic: Diagnostic) -> ParsedFile:
    return ParsedFile(job.display_path, job.language, b"", (), diagnostics=(diagnostic,), failed=True)


def _syntax_failure(job: FileJob, errors: tuple[Any, ...]) -> ParsedFile:
    return _failed_file(
        job,
        Diagnostic(
            job.display_path,
            "syntax-error",
            "Tree-sitter reported invalid or unsupported syntax; file not evaluated",
            Severity.ERROR,
            first_error_line(errors),
        ),
    )


def _recovery_diagnostics(job: FileJob, errors: tuple[Any, ...]) -> tuple[Diagnostic, ...]:
    if not errors:
        return ()
    return (
        Diagnostic(
            job.display_path,
            "typescript-type-recovery",
            "TypeScript type-only syntax was recovered; reference/type extraction may be incomplete",
            Severity.WARNING,
            first_error_line(errors),
            False,
        ),
    )


def _parsed_file(
    source: bytes, job: FileJob, root: Any, captures: dict[str, list[Any]], errors: tuple[Any, ...]
) -> ParsedFile:
    symbols = capture_symbols(captures.get("unit", []), source, job)
    branches = sorted(node.start_byte for node in captures.get("branch", []))
    occurrences = references(root, source)
    units = _normalize(symbols, source, job, branches, occurrences)
    declarations = tuple(
        node_text(node, source)[:1024] for node in sorted(captures.get("import", []), key=lambda n: n.start_byte)[:32]
    )
    return ParsedFile(
        job.display_path,
        job.language,
        source,
        units,
        declarations,
        diagnostics=_recovery_diagnostics(job, errors),
        references=occurrences,
        blocks=source_blocks(root, source),
    )


def parse_source(source: bytes, job: FileJob, max_units: int = 10_000) -> ParsedFile:
    from tree_sitter import QueryCursor

    source.decode("utf-8")  # Establish the UTF-8 contract before slicing source.
    parser, query = _frontend(job.grammar)
    tree = parser.parse(source)
    errors = parse_errors(tree.root_node)
    if errors and (job.grammar not in TYPE_SCRIPT_GRAMMARS or not recoverable_type_errors(errors, source)):
        return _syntax_failure(job, errors)
    captures = QueryCursor(query).captures(tree.root_node)
    if errors and not errors_outside_executable_bodies(errors, captures.get("unit", []), SPECS[job.grammar]):
        return _syntax_failure(job, errors)
    if len(captures.get("unit", [])) > max_units:
        return _failed_file(
            job, Diagnostic(job.display_path, "unit-limit", f"file exceeds scan.max_units_per_file ({max_units})")
        )
    return _parsed_file(source, job, tree.root_node, captures, errors)


def parse_batch(jobs: list[FileJob], max_file_bytes: int, max_units: int) -> list[ParsedFile]:
    results = []
    for job in jobs:
        try:
            with Path(job.absolute_path).open("rb") as stream:
                source = stream.read(max_file_bytes + 1)
            if len(source) > max_file_bytes:
                diagnostic = Diagnostic(
                    job.display_path, "file-size-limit", f"file exceeds scan.max_file_bytes ({max_file_bytes})"
                )
                results.append(_failed_file(job, diagnostic))
                continue
            results.append(parse_source(source, job, max_units))
        except (OSError, UnicodeError) as exc:
            diagnostic = Diagnostic(job.display_path, "file-read-error", str(exc), Severity.ERROR)
            results.append(_failed_file(job, diagnostic))
    return results
