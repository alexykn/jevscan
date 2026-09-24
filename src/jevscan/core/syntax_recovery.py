"""Recognize narrowly supported TypeScript type-only grammar errors.

These predicates admit known syntax shapes, not arbitrary broken programs.
Executable-body errors always remain fatal to the file's classification.
"""

from typing import Any

from jevscan.core.models import CALLABLE_KINDS
from jevscan.core.syntax_symbols import node_body, node_text

TYPE_SCRIPT_GRAMMARS = frozenset({"typescript", "tsx"})
_TYPE_ANNOTATION_PARENTS = frozenset({
    "required_parameter",
    "optional_parameter",
    "rest_parameter",
    "function_declaration",
    "function_expression",
    "generator_function",
    "generator_function_declaration",
    "method_definition",
    "function_signature",
    "abstract_method_signature",
    "method_signature",
    "arrow_function",
})


def parse_errors(root: Any) -> tuple[Any, ...]:
    pending = [root]
    errors = []
    while pending:
        node = pending.pop()
        if node.is_error or node.is_missing:
            errors.append(node)
        pending.extend(reversed(node.children))
    return tuple(errors)


def first_error_line(errors: tuple[Any, ...]) -> int:
    return min((node.start_point.row + 1 for node in errors), default=1)


def _is_signature_type_annotation(node: Any) -> bool:
    return node.type == "type_annotation" and node.parent is not None and node.parent.type in _TYPE_ANNOTATION_PARENTS


def _named_child(node: Any, index: int, node_type: str) -> Any | None:
    children = node.named_children
    return children[index] if len(children) > index and children[index].type == node_type else None


def _parent_of_type(node: Any, node_type: str) -> Any | None:
    parent = node.parent
    return parent if parent is not None and parent.type == node_type else None


def _is_nested_signature_type_annotation(node: Any) -> bool:
    if _is_signature_type_annotation(node):
        return True
    property_signature = _parent_of_type(node, "property_signature")
    object_type = _parent_of_type(property_signature, "object_type") if property_signature is not None else None
    if object_type is None:
        return False
    container = object_type.parent
    if container is not None and container.type == "type_arguments":
        generic = _parent_of_type(container, "generic_type")
        container = generic.parent if generic is not None else None
    return container is not None and _is_signature_type_annotation(container)


def _is_imported_member(node: Any) -> bool:
    if node.type != "member_expression" or [child.type for child in node.children] != [
        "call_expression",
        ".",
        "property_identifier",
    ]:
        return False
    call = node.child_by_field_name("object")
    property_node = node.child_by_field_name("property")
    if (
        call is None
        or property_node is None
        or call.type != "call_expression"
        or property_node.type != "property_identifier"
    ):
        return False
    function = call.child_by_field_name("function")
    arguments = call.child_by_field_name("arguments")
    return (
        function is not None
        and function.type == "import"
        and arguments is not None
        and arguments.type == "arguments"
        and [child.type for child in arguments.children] == ["(", "string", ")"]
        and len(arguments.named_children) == 1
    )


def _is_nested_imported_member(node: Any, source: bytes) -> bool:
    arguments = _parent_of_type(node, "type_arguments")
    if arguments is None:
        return False
    inner_generic = _parent_of_type(arguments, "generic_type")
    outer_arguments = _parent_of_type(inner_generic, "type_arguments") if inner_generic is not None else None
    outer_generic = _parent_of_type(outer_arguments, "generic_type") if outer_arguments is not None else None
    lookup = _parent_of_type(outer_generic, "lookup_type") if outer_generic is not None else None
    annotation = lookup.parent if lookup is not None else None
    if inner_generic is None or outer_arguments is None or outer_generic is None or lookup is None:
        return False
    if annotation is None or not _is_signature_type_annotation(annotation):
        return False
    inner_name = _named_child(inner_generic, 0, "type_identifier")
    outer_name = _named_child(outer_generic, 0, "type_identifier")
    if (
        inner_name is None
        or node_text(inner_name, source) != "NonNullable"
        or outer_name is None
        or node_text(outer_name, source) != "Parameters"
    ):
        return False
    inner_index = _named_child(arguments, 1, "tuple_type")
    lookup_index = _named_child(lookup, 1, "literal_type")
    return (
        inner_index is not None
        and len(inner_index.named_children) == 1
        and inner_index.named_children[0].type == "literal_type"
        and lookup_index is not None
        and node_text(lookup_index, source) == "0"
    )


def _is_recoverable_export_type(node: Any, source: bytes) -> bool:
    statement = node.parent
    block = statement.parent if statement is not None else None
    module = block.parent if block is not None else None
    ambient = module.parent if module is not None else None
    if (
        node.type != "ERROR"
        or node_text(node, source) != "type"
        or statement is None
        or statement.type != "export_statement"
        or block is None
        or block.type != "statement_block"
        or module is None
        or module.type != "module"
        or ambient is None
        or ambient.type != "ambient_declaration"
    ):
        return False
    child_types = [child.type for child in statement.children]
    return child_types in (["export", "ERROR", "*", "from", "string", ";"], ["export", "ERROR", "*", "from", "string"])


def _is_recoverable_import_type(node: Any, source: bytes) -> bool:
    parent = node.parent
    if node.type != "ERROR" or parent is None:
        return False
    children = node.named_children
    if parent.type == "type_annotation" and _is_nested_signature_type_annotation(parent):
        if len(children) == 1 and _is_imported_member(children[0]):
            return True
        if len(children) == 1 and children[0].type == "readonly_type":
            readonly = children[0]
            return (
                [child.type for child in readonly.children] == ["readonly", "member_expression"]
                and len(readonly.named_children) == 1
                and _is_imported_member(readonly.named_children[0])
            )
    return (
        parent.type == "type_arguments"
        and len(children) == 1
        and _is_imported_member(children[0])
        and _is_nested_imported_member(node, source)
    )


def recoverable_type_errors(errors: tuple[Any, ...], source: bytes) -> bool:
    return bool(errors) and all(
        _is_recoverable_export_type(node, source) or _is_recoverable_import_type(node, source) for node in errors
    )


def errors_outside_executable_bodies(errors: tuple[Any, ...], captured: list[Any], spec: Any) -> bool:
    executable = [node for node in captured if spec.nodes[node.type] in CALLABLE_KINDS and node_body(node) is not None]
    for error in errors:
        containing = [
            node for node in executable if node.start_byte <= error.start_byte and error.end_byte <= node.end_byte
        ]
        if not containing:
            continue
        body = node_body(min(containing, key=lambda node: node.end_byte - node.start_byte))
        if body is not None and error.start_byte < body.end_byte and error.end_byte > body.start_byte:
            return False
    return True
