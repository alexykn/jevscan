"""Native Tree-sitter queries, executed in process workers, produce lexical code units."""

import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from jevscan.core.languages import SPECS
from jevscan.core.models import (
    CALLABLE_KINDS,
    Diagnostic,
    FileJob,
    Kind,
    ParsedFile,
    Reference,
    Severity,
    SyntaxFact,
    Unit,
)
from jevscan.core.syntax import callback_label, source_blocks


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


_NAME_NODES = frozenset({
    "identifier",
    "property_identifier",
    "field_identifier",
    "type_identifier",
    "bareword",
    "function",
    "method",
})
_CALL_NODES = frozenset({
    "call",
    "call_expression",
    "func1op_call_expression",
    "function_call_expression",
    "method_call_expression",
})
_VALIDATION_NODES = _CALL_NODES | frozenset({
    "assert_expression",
    "assert_statement",
    "binary_expression",
    "boolean_operator",
    "comparison_expression",
    "comparison_operator",
    "conditional_expression",
    "conditional_statement",
    "if_expression",
    "if_statement",
    "in_operator",
    "is_operator",
    "macro_invocation",
    "match_expression",
    "type_check",
    "unary_expression",
    "unless_statement",
})
_FALLBACK_NODES = _CALL_NODES | frozenset({
    "assignment_expression",
    "binary_expression",
    "boolean_operator",
    "catch_clause",
    "coalesce_expression",
    "conditional_expression",
    "conditional_statement",
    "default_parameter",
    "else_clause",
    "eval_expression",
    "except_clause",
    "finally_clause",
    "if_expression",
    "if_statement",
    "macro_invocation",
    "match_expression",
    "optional_parameter",
    "rescue_clause",
    "try_expression",
    "try_statement",
    "unless_statement",
})
_SENTINEL_NODES = frozenset({"none", "null", "undefined", "undef", "undef_expression"})
_SENTINEL_NAMES = frozenset({"None", "null", "undefined", "undef"})


def _callee_name(node: Any, source: bytes) -> str | None:
    callee = node.child_by_field_name("function") or node.child_by_field_name("method")
    if callee is None:
        return None
    # Calls through returned functions or indexed expressions cannot be named here.
    while callee.type not in _NAME_NODES:
        if callee.type not in {
            "attribute",
            "member_expression",
            "field_expression",
            "scoped_identifier",
            "generic_function",
        }:
            return None
        callee = (
            callee.child_by_field_name("attribute")
            or callee.child_by_field_name("property")
            or callee.child_by_field_name("field")
            or callee.child_by_field_name("name")
            or callee.child_by_field_name("function")
        )
        if callee is None:
            return None
    return _text(callee, source).rsplit("::", 1)[-1]


def _references(root: Any, source: bytes) -> tuple[Reference, ...]:
    references = []
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type in _CALL_NODES:
            name = _callee_name(node, source)
            if name:
                references.append(Reference(name, node.start_byte, node.end_byte, "call"))
        elif node.type in _NAME_NODES and not node.named_children:
            references.append(
                Reference(_text(node, source).rsplit("::", 1)[-1], node.start_byte, node.end_byte, "name")
            )
        pending.extend(reversed(node.named_children))
    return tuple(references)


def _has_implementation(symbol: _Symbol) -> bool:
    if symbol.body is None:
        return False
    if symbol.kind not in CALLABLE_KINDS:
        return True
    if symbol.body.type not in {"block", "statement_block"}:
        return True  # Expression-bodied arrows/closures are implementations too.
    for statement in symbol.body.named_children:
        if statement.type == "expression_statement" and len(statement.named_children) == 1:
            statement = statement.named_children[0]
        if statement.type not in {"comment", "pass_statement", "ellipsis", "string", "concatenated_string"}:
            return True
    return False


def _syntax_node_types(node: Any) -> frozenset[str]:
    pending = [node]
    result: set[str] = set()
    while pending:
        current = pending.pop()
        result.add(current.type)
        pending.extend(current.named_children)
    return frozenset(result)


def _contains_sentinel(node: Any, source: bytes) -> bool:
    pending = [node]
    while pending:
        current = pending.pop()
        if current.type in _SENTINEL_NODES:
            return True
        if current.type in _NAME_NODES and not current.named_children and _text(current, source) in _SENTINEL_NAMES:
            return True
        pending.extend(current.named_children)
    return False


def _syntax_facts(symbol: _Symbol, node_types: frozenset[str], source: bytes) -> tuple[SyntaxFact, ...]:
    """Record broad syntactic admission facts; semantic classification remains with Jev."""
    facts: set[SyntaxFact] = set()
    if node_types & _VALIDATION_NODES:
        facts.add("validation_candidate")
    if node_types & _FALLBACK_NODES or _contains_sentinel(symbol.node, source):
        facts.add("fallback_candidate")
    if symbol.kind in CALLABLE_KINDS and _has_implementation(symbol):
        facts.add("executable_behavior")
    return tuple(sorted(facts))


def _calls_by_callable(units: list[Unit], references: tuple[Reference, ...]) -> dict[str, set[str]]:
    callables = iter(unit for unit in units if unit.kind in CALLABLE_KINDS and unit.has_implementation)
    following = next(callables, None)
    stack: list[Unit] = []
    result: dict[str, set[str]] = defaultdict(set)
    for reference in sorted((item for item in references if item.kind == "call"), key=lambda item: item.start_byte):
        while following is not None and following.start_byte <= reference.start_byte:
            while stack and following.start_byte >= stack[-1].end_byte:
                stack.pop()
            stack.append(following)
            following = next(callables, None)
        while stack and reference.end_byte > stack[-1].end_byte:
            stack.pop()
        if stack:
            result[stack[-1].id].add(reference.name)
    return result


def _has_helper_relationship(children: list[Unit], calls_by_callable: dict[str, set[str]]) -> bool:
    ids_by_name: dict[str, set[str]] = defaultdict(set)
    for child in children:
        ids_by_name[child.name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]].add(child.id)
    for caller in children:
        for called_name in calls_by_callable.get(caller.id, ()):
            if any(child_id != caller.id for child_id in ids_by_name.get(called_name, ())):
                return True
    return False


def _finalize_units(
    units: list[Unit], member_counts: dict[str, int], references: tuple[Reference, ...]
) -> tuple[Unit, ...]:
    children_by_parent: dict[str, list[Unit]] = defaultdict(list)
    for child in units:
        if child.parent_id:
            children_by_parent[child.parent_id].append(child)
    calls_by_callable = _calls_by_callable(units, references)
    result = []
    for unit in units:
        children = [
            child for child in children_by_parent[unit.id] if child.kind in CALLABLE_KINDS and child.has_implementation
        ]
        facts = set(unit.syntax_facts)
        if children:
            facts.add("executable_behavior")
        if len(children) >= 2 and _has_helper_relationship(children, calls_by_callable):
            facts.add("helper_relationship")
        result.append(replace(unit, member_count=member_counts[unit.id], syntax_facts=tuple(sorted(facts))))
    return tuple(result)


def _normalize(
    symbols: list[_Symbol],
    source: bytes,
    job: FileJob,
    branches: list[int],
    references: tuple[Reference, ...],
) -> tuple[Unit, ...]:
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
        label = callback_label(symbol.node, source) if symbol.name.startswith("<anonymous@") else symbol.name
        display = parent.display_name + separator + label if parent else label
        if job.language == "perl" and (kind in {Kind.PACKAGE, Kind.CLASS} or "::" in symbol.name):
            display = symbol.name
        node_types = _syntax_node_types(symbol.node)
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
            display_name=display,
            body_start_byte=symbol.body.start_byte if symbol.body is not None else None,
            body_end_byte=symbol.body.end_byte if symbol.body is not None else None,
            has_implementation=_has_implementation(symbol),
            branch_nodes=bisect_left(branches, symbol.end) - bisect_left(branches, symbol.start),
            syntax_facts=_syntax_facts(symbol, node_types, source),
        )
        if parent:
            member_counts[parent.id] += 1
        units.append(unit)
        stack.append(unit)
    return _finalize_units(units, member_counts, references)


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
    references = _references(tree.root_node, source)
    units = _normalize(symbols, source, job, branches, references)
    declarations = tuple(
        _text(node, source)[:1024] for node in sorted(captures.get("import", []), key=lambda n: n.start_byte)[:32]
    )
    return ParsedFile(
        job.display_path,
        job.language,
        source,
        units,
        declarations,
        references=references,
        blocks=source_blocks(tree.root_node, source),
    )


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
