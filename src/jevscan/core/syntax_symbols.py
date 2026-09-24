"""Language-specific declaration names, bindings, bodies, and lexical extents."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from jevscan.core.languages import SPECS
from jevscan.core.models import FileJob, Kind


@dataclass(slots=True)
class Symbol:
    node: Any
    kind: Kind
    name: str
    start: int
    end: int
    body: Any
    bound_kind: Kind | None = None
    outer_binding: str | None = None


def node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def node_body(node: Any) -> Any:
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


def _rust_tuple_binding(node: Any, source: bytes) -> tuple[str, Kind] | None:
    """Bind a closure only when its tuple expression and pattern align exactly."""
    expression = node.parent
    declaration = expression.parent if expression is not None else None
    if (
        node.type != "closure_expression"
        or expression is None
        or expression.type != "tuple_expression"
        or declaration is None
        or declaration.type != "let_declaration"
    ):
        return None
    pattern = declaration.child_by_field_name("pattern")
    value = declaration.child_by_field_name("value")
    if pattern is None or pattern.type != "tuple_pattern" or value is None or value.id != expression.id:
        return None
    expressions = list(expression.named_children)
    patterns = list(pattern.named_children)
    if len(expressions) != len(patterns) or any(item.type != "identifier" for item in patterns):
        return None
    try:
        position = next(index for index, item in enumerate(expressions) if item.id == node.id)
    except StopIteration:
        return None
    return node_text(patterns[position], source), Kind.FUNCTION


def _typescript_property_object(node: Any) -> Any | None:
    pair = node.parent
    object_node = pair.parent if pair is not None else None
    if (
        node.type != "arrow_function"
        or pair is None
        or pair.type != "pair"
        or pair.child_by_field_name("value") is None
        or pair.child_by_field_name("value").id != node.id
        or object_node is None
        or object_node.type != "object"
    ):
        return None
    return object_node


def _variable_name_for_value(declaration: Any, value: Any, source: bytes) -> str | None:
    if declaration is None or declaration.type != "variable_declarator":
        return None
    declared_value = declaration.child_by_field_name("value")
    name = declaration.child_by_field_name("name")
    if declared_value is None or declared_value.id != value.id or name is None or name.type != "identifier":
        return None
    return node_text(name, source)


def _typescript_outer_binding(node: Any, source: bytes) -> str | None:
    object_node = _typescript_property_object(node)
    if object_node is None:
        return None
    container = object_node.parent
    direct = _variable_name_for_value(container, object_node, source)
    if direct is not None:
        return direct
    call = container.parent if container.type == "arguments" else None
    if (
        container.type != "arguments"
        or call is None
        or call.type != "call_expression"
        or len(container.named_children) != 1
        or container.named_children[0].id != object_node.id
    ):
        return None
    return _variable_name_for_value(call.parent, call, source)


def _binding(node: Any, source: bytes) -> tuple[str | None, Kind | None]:
    """Recognize actual assignment/field syntax, not a name guessed from function text."""
    tuple_binding = _rust_tuple_binding(node, source)
    if tuple_binding is not None:
        return tuple_binding
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
                return node_text(name, source).strip("\"'"), kind
            break
        if current.type not in {
            "parenthesized_expression",
            "as_expression",
            "satisfies_expression",
            "type_cast_expression",
        }:
            break
        current = current.parent
    return None, None


def _rust_callable_start(node: Any) -> int:
    if node.type not in {"function_item", "function_signature_item"} or node.parent is None:
        return node.start_byte
    start = node.start_byte
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type == "attribute_item":
        start = sibling.start_byte
        sibling = sibling.prev_named_sibling
    return start


def _symbol(node: Any, kind: Kind, source: bytes, language: str) -> Symbol:
    body = node_body(node)
    start = _rust_callable_start(node)
    if node.parent is not None and node.parent.type == "decorated_definition":
        start = node.parent.start_byte
    if (
        kind == Kind.METHOD
        and node.type == "function_signature_item"
        and node.parent is not None
        and node.parent.type == "declaration_list"
        and node.parent.parent is not None
        and node.parent.parent.type == "foreign_mod_item"
    ):
        kind = Kind.FUNCTION
    name_node = node.child_by_field_name("name")
    bound_kind = None
    outer_binding = None
    if kind == Kind.IMPL:
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")
        target = node_text(type_node, source) if type_node is not None else "<type>"
        trait = node_text(trait_node, source) + " for " if trait_node is not None else ""
        name = f"impl {trait}{target}"
    elif name_node is not None:
        name = node_text(name_node, source)
    else:
        name, bound_kind = _binding(node, source)
        if language == "typescript":
            outer_binding = _typescript_outer_binding(node, source)
        if name is None:
            name = f"<anonymous@{node.start_point.row + 1}:{node.start_point.column + 1}>"
    return Symbol(node, kind, name, start, node.end_byte, body, bound_kind, outer_binding)


def _extend_perl_namespaces(symbols: list[Symbol]) -> None:
    """`package Foo; ...` extends past its statement, within its lexical scope."""
    scopes: dict[int, list[Symbol]] = defaultdict(list)
    for symbol in symbols:
        if (
            symbol.node.type in {"package_statement", "class_statement"}
            and symbol.node.parent is not None
            and symbol.body is None
        ):
            scopes[symbol.node.parent.id].append(symbol)
    for siblings in scopes.values():
        siblings.sort(key=lambda item: item.start)
        for index, symbol in enumerate(siblings):
            symbol.end = siblings[index + 1].start if index + 1 < len(siblings) else symbol.node.parent.end_byte


def capture_symbols(captured: list[Any], source: bytes, job: FileJob) -> list[Symbol]:
    symbols = [_symbol(node, SPECS[job.grammar].nodes[node.type], source, job.language) for node in captured]
    if job.language == "perl":
        _extend_perl_namespaces(symbols)
    return symbols
