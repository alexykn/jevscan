"""Source-derived context fragments and callback labels; never invent symbol bindings."""

import json
from typing import Any

from jevscan.core.models import SourceBlock

_WRAPPERS = frozenset({"parenthesized_expression", "as_expression", "satisfies_expression", "type_assertion"})
_DECLARATIONS = frozenset({
    "import_statement",
    "import_from_statement",
    "use_declaration",
    "use_statement",
    "lexical_declaration",
    "variable_declaration",
    "let_declaration",
    "const_item",
    "static_item",
    "assignment",
    "public_field_definition",
    "field_definition",
    "field_declaration",
    "type_alias_declaration",
    "interface_declaration",
    "type_item",
    "struct_item",
    "enum_item",
})


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _callee_label(node: Any, source: bytes) -> str:
    parts = []
    current = node
    while len(parts) < 8:
        if current.type in {"identifier", "scoped_identifier", "this"}:
            parts.append(_text(current, source))
            break
        if current.type not in {"member_expression", "attribute", "field_expression"}:
            break
        member = (
            current.child_by_field_name("property")
            or current.child_by_field_name("attribute")
            or current.child_by_field_name("field")
        )
        owner = current.child_by_field_name("object") or current.child_by_field_name("value")
        if member is None:
            break
        parts.append(_text(member, source))
        if owner is None:
            break
        current = owner
    return ".".join(reversed(parts)) or "call"


def display_path(parent: str, local: str, separator: str) -> str:
    """Keep readable ancestry without allowing deeply nested callbacks to grow headings unboundedly."""
    local = local[:177] + "..." if len(local) > 180 else local
    parent = parent[:96] + "..." + parent[-160:] if len(parent) > 260 else parent
    return parent + separator + local if parent else local


def callback_label(node: Any, source: bytes) -> str:
    """Syntactic call role, not a function name or an assertion about runtime dispatch."""
    current = node
    while current.parent is not None and current.parent.type in _WRAPPERS:
        current = current.parent
    arguments = current.parent
    location = f"{node.start_point.row + 1}:{node.start_point.column + 1}"
    if arguments is None or arguments.type not in {"arguments", "argument_list"}:
        return f"callback@{location}"
    call = arguments.parent
    callee = call.child_by_field_name("function") or call.child_by_field_name("constructor")
    if callee is None:
        return f"callback@{location}"
    label = _callee_label(callee, source)
    # A complex callee can contain other calls/bodies; keep display metadata bounded.
    if len(label) > 80:
        label = label[:77] + "..."
    args = [child for child in arguments.named_children if child.type != "comment"]
    position = next(i for i, child in enumerate(args) if child.id == current.id)
    tail = label.rsplit(".", 1)[-1]
    if tail in {"describe", "test", "it"} and position > 0 and args[0].type == "string":
        literal = _text(args[0], source)[1:-1]
        literal = literal[:93] + "..." if len(literal) > 96 else literal
        return f"{label}[{json.dumps(literal, ensure_ascii=False)}]"
    if call.type == "new_expression" and label == "Promise" and position == 0:
        return "Promise[executor]"
    # Even `then` might be a user-defined method, so use the honest argument position.
    return f"{label}[arg{position}]@{location}"


def _bindings(node: Any, source: bytes) -> tuple[str, ...]:
    names = []
    pending = [node]
    while pending:
        child = pending.pop()
        name = (
            child.child_by_field_name("name")
            or child.child_by_field_name("pattern")
            or child.child_by_field_name("left")
        )
        if name is not None:
            names.append(_text(name, source).rsplit(".", 1)[-1].strip("\"'"))
        elif child.type in {"lexical_declaration", "variable_declaration"}:
            pending.extend(child.named_children)
    return tuple(names)


def context_blocks(root: Any, source: bytes) -> tuple[SourceBlock, ...]:
    """Retain AST node spans for declarations/fields and same-scope setup operations."""
    blocks = []
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type in _DECLARATIONS or node.type == "expression_statement":
            parent = node.parent
            # Nearest lexical block determines where a declaration may supply captured state.
            while parent is not None and parent.type not in {
                "program",
                "module",
                "source_file",
                "block",
                "statement_block",
                "class_body",
                "declaration_list",
            }:
                parent = parent.parent
            blocks.append(
                SourceBlock(
                    node.start_byte,
                    node.end_byte,
                    node.type,
                    _bindings(node, source),
                    parent.start_byte if parent else 0,
                    parent.end_byte if parent else len(source),
                )
            )
        pending.extend(reversed(node.named_children))
    return tuple(blocks)
