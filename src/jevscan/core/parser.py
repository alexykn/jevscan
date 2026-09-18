"""Native Tree-sitter queries, executed in process workers, produce lexical code units."""

import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from jevscan.core.languages import SPECS
from jevscan.core.models import Diagnostic, FileJob, Kind, ParsedFile, Severity, Unit


class ParserUnavailableError(RuntimeError):
    """Missing or incompatible Tree-sitter runtime/grammar installation."""


@dataclass(slots=True)
class _Symbol:
    node: Any
    kind: Kind
    name: str
    start: int
    end: int
    body: Any
    bound_kind: Kind | None = None


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


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _body(node: Any) -> Any:
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    return next(
        (
            child
            for child in node.named_children
            if child.type in {"block", "class_body", "interface_body", "declaration_list"}
        ),
        None,
    )


def _binding(node: Any, source: bytes) -> tuple[str | None, Kind | None]:
    """Recognize actual assignment/field syntax, not a name guessed from function text."""
    current = node.parent
    for _ in range(3):
        if current is None:
            break
        fields = {
            "variable_declarator": "name",
            "assignment_expression": "left",
            "let_declaration": "pattern",
            "pair": "key",
            "public_field_definition": "name",
            "field_definition": "property",
        }
        if current.type in fields:
            name = current.child_by_field_name(fields[current.type])
            if name is not None:
                kind = (
                    Kind.METHOD
                    if current.type in {"pair", "public_field_definition", "field_definition"}
                    else Kind.FUNCTION
                )
                return _text(name, source).strip("\"'"), kind
            break
        if current.type not in {"parenthesized_expression", "as_expression", "satisfies_expression"}:
            break
        current = current.parent
    return None, None


def _symbol(node: Any, kind: Kind, source: bytes) -> _Symbol:
    body = _body(node)
    start = node.start_byte
    if node.parent is not None and node.parent.type == "decorated_definition":
        start = node.parent.start_byte
    name_node = node.child_by_field_name("name")
    bound_kind = None
    if kind == Kind.IMPL:
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")
        target = _text(type_node, source) if type_node is not None else "<type>"
        trait = _text(trait_node, source) + " for " if trait_node is not None else ""
        name = f"impl {trait}{target}"
    elif name_node is not None:
        name = _text(name_node, source)
    else:
        name, bound_kind = _binding(node, source)
        if name is None:
            name = f"<anonymous@{node.start_point.row + 1}:{node.start_point.column + 1}>"
    return _Symbol(node, kind, name, start, node.end_byte, body, bound_kind)


def _extend_perl_namespaces(symbols: list[_Symbol]) -> None:
    """`package Foo; ...` has lexical extent even though its syntax node is only a statement."""
    scopes: dict[int, list[_Symbol]] = defaultdict(list)
    for symbol in symbols:
        if (
            symbol.node.type in {"package_statement", "class_statement"}
            and symbol.node.parent is not None
            and symbol.body is None
        ):
            # Block-scoped namespaces restore their enclosing namespace afterward.
            scopes[symbol.node.parent.id].append(symbol)
    for siblings in scopes.values():
        siblings.sort(key=lambda item: item.start)
        for index, symbol in enumerate(siblings):
            symbol.end = siblings[index + 1].start if index + 1 < len(siblings) else symbol.node.parent.end_byte


def _first_error_line(root: Any) -> int:
    pending = [root]
    while pending:
        node = pending.pop()
        if node.is_error or node.is_missing:
            return node.start_point.row + 1
        pending.extend(reversed([child for child in node.children if child.has_error or child.is_missing]))
    return 1


def _normalize(symbols: list[_Symbol], source: bytes, job: FileJob, branches: list[int]) -> tuple[Unit, ...]:
    # Sorting and this interval stack avoid an O(symbols**2) enclosing-parent search.
    symbols.sort(key=lambda item: (item.start, -item.end))
    newlines = [match.start() for match in re.finditer(b"\n", source)]
    units: list[Unit] = []
    stack: list[Unit] = []
    member_counts: dict[str, int] = defaultdict(int)
    for symbol in symbols:
        while stack and not (
            stack[-1].start_byte <= symbol.start < stack[-1].end_byte and symbol.end <= stack[-1].end_byte
        ):
            stack.pop()
        parent = stack[-1] if stack else None
        kind = symbol.kind
        if kind == Kind.FUNCTION and parent and parent.kind in {Kind.CLASS, Kind.IMPL, Kind.TRAIT, Kind.INTERFACE}:
            kind = Kind.METHOD
        if kind == Kind.CLOSURE and symbol.bound_kind is not None:
            kind = symbol.bound_kind
        separator = "::" if job.language in {"rust", "perl"} else "."
        qualified = parent.qualified_name + separator + symbol.name if parent else symbol.name
        if job.language == "perl" and (kind in {Kind.PACKAGE, Kind.CLASS} or "::" in symbol.name):
            # Perl package declarations and explicitly qualified subs name absolute namespaces.
            qualified = symbol.name
        signature_end = symbol.body.start_byte if symbol.body is not None else symbol.node.end_byte
        signature = source[symbol.start : signature_end].decode("utf-8").strip()
        # Metadata is bounded; the full source unit sent for analysis remains untruncated.
        if len(signature) > 1024:
            signature = signature[:1024] + " [signature abbreviated]"
        unit = Unit(
            id=f"{job.display_path}:{symbol.start}:{kind}",
            path=job.display_path,
            language=job.language,
            kind=kind,
            name=symbol.name,
            qualified_name=qualified,
            parent_id=parent.id if parent else None,
            start_byte=symbol.start,
            end_byte=symbol.end,
            start_line=bisect_left(newlines, symbol.start) + 1,
            end_line=bisect_left(newlines, max(symbol.start, symbol.end - 1)) + 1,
            signature=signature,
            has_body=symbol.body is not None,
            branch_nodes=bisect_left(branches, symbol.end) - bisect_left(branches, symbol.start),
        )
        if parent:
            member_counts[parent.id] += 1
        units.append(unit)
        stack.append(unit)
    return tuple(replace(unit, member_count=member_counts[unit.id]) for unit in units)


def parse_source(source: bytes, job: FileJob, max_units: int = 10_000) -> ParsedFile:
    from tree_sitter import QueryCursor

    source.decode("utf-8")  # The file boundary establishes the UTF-8 contract before any slicing.
    parser, query = _frontend(job.grammar)
    tree = parser.parse(source)
    if tree.root_node.has_error:
        error = Diagnostic(
            job.display_path,
            "syntax-error",
            "Tree-sitter reported invalid or unsupported syntax; file not evaluated",
            Severity.ERROR,
            _first_error_line(tree.root_node),
        )
        return ParsedFile(job.display_path, job.language, b"", (), diagnostics=(error,), failed=True)
    captures = QueryCursor(query).captures(tree.root_node)
    captured = captures.get("unit", [])
    if len(captured) > max_units:
        diagnostic = Diagnostic(job.display_path, "unit-limit", f"file exceeds scan.max_units_per_file ({max_units})")
        return ParsedFile(job.display_path, job.language, b"", (), diagnostics=(diagnostic,), failed=True)
    symbols = [_symbol(node, SPECS[job.grammar].nodes[node.type], source) for node in captured]
    if job.language == "perl":
        _extend_perl_namespaces(symbols)
    branches = sorted(node.start_byte for node in captures.get("branch", []))
    units = _normalize(symbols, source, job, branches)
    declarations = tuple(
        _text(node, source)[:1024] for node in sorted(captures.get("import", []), key=lambda n: n.start_byte)[:32]
    )
    return ParsedFile(job.display_path, job.language, source, units, declarations)


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
                results.append(
                    ParsedFile(job.display_path, job.language, b"", (), diagnostics=(diagnostic,), failed=True)
                )
                continue
            results.append(parse_source(source, job, max_units))
        except (OSError, UnicodeError) as exc:
            diagnostic = Diagnostic(job.display_path, "file-read-error", str(exc), Severity.ERROR)
            results.append(ParsedFile(job.display_path, job.language, b"", (), diagnostics=(diagnostic,), failed=True))
    return results
