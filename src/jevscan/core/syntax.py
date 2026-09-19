"""Small syntax-derived display and declaration facts; no model or runtime evaluation."""

from typing import Any

from jevscan.core.models import SourceBlock

_WRAPPERS = {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"}
_DECLARATIONS = {
    "lexical_declaration",
    "variable_declaration",
    "assignment",
    "annotated_assignment",
    "const_item",
    "static_item",
    "let_declaration",
    "field_declaration",
    "public_field_definition",
    "field_definition",
    "attribute_item",
    "use_declaration",
    "import_statement",
    "import_from_statement",
    "use_statement",
}


def _text(node: Any, source: bytes, limit: int = 96) -> str:
    value = source[node.start_byte : node.end_byte].decode("utf-8")
    return value if len(value) <= limit else value[:limit] + "…"


def callback_label(node: Any, source: bytes) -> str:
    """Call-site role for display only: it must never become a resolved symbol name."""
    current = node
    while current.parent is not None and current.parent.type in _WRAPPERS:
        current = current.parent
    arguments = current.parent
    if arguments is None or arguments.type not in {"arguments", "argument_list"}:
        return f"callback@{node.start_point.row + 1}:{node.start_point.column + 1}"
    call = arguments.parent
    callee = call.child_by_field_name("function") or call.child_by_field_name("constructor")
    if callee is None:
        return f"callback@{node.start_point.row + 1}:{node.start_point.column + 1}"
    # A method call's receiver can be an arbitrarily long chain. Use only its property.
    name = callee.child_by_field_name("property") or callee.child_by_field_name("attribute") or callee
    label = _text(name, source)
    position = next(i for i, child in enumerate(arguments.named_children, 1) if child.id == current.id)
    first = arguments.named_children[0]
    if label in {"describe", "test", "it", "suite"} and first.type in {"string", "string_literal"}:
        return f"{label}[{_text(first, source)}]"
    return f"{label}[arg{position}]"


def _declared_names(node: Any, source: bytes) -> tuple[str, ...]:
    names = []
    candidates = node.named_children if node.type in {"lexical_declaration", "variable_declaration"} else [node]
    for candidate in candidates:
        name = (
            candidate.child_by_field_name("name")
            or candidate.child_by_field_name("left")
            or candidate.child_by_field_name("pattern")
            or candidate.child_by_field_name("property")
        )
        if name is not None:
            names.append(_text(name, source, 256))
    return tuple(names)


def source_blocks(root: Any, source: bytes) -> tuple[SourceBlock, ...]:
    blocks = []
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type in _DECLARATIONS:
            blocks.append(SourceBlock(node.start_byte, node.end_byte, _declared_names(node, source), node.type))
        pending.extend(reversed(node.named_children))
    return tuple(blocks)
