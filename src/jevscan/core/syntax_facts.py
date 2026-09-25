"""Lexical references and broad syntax facts; never semantic defect judgments."""

from collections import defaultdict
from dataclasses import replace
from typing import Any

from jevscan.core.languages import SPECS
from jevscan.core.lexical_ownership import owned_references
from jevscan.core.models import CALLABLE_KINDS, Reference, SyntaxFact, Unit
from jevscan.core.syntax_symbols import Symbol, node_text

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
_ASSIGNMENT_NODES = frozenset({
    "assignment",
    "assignment_expression",
    "augmented_assignment",
    "augmented_assignment_expression",
})
_STATE_UPDATE_NODES = frozenset({"delete_statement", "update_expression"})
_STATE_MUTATION_NODES = _ASSIGNMENT_NODES | _STATE_UPDATE_NODES
_STATEFUL_TARGET_NODES = frozenset({
    "attribute",
    "field_expression",
    "index_expression",
    "member_expression",
    "subscript",
    "subscript_expression",
})
_CALLABLE_NODE_TYPES = frozenset(
    name for spec in SPECS.values() for name, kind in spec.nodes.items() if kind in CALLABLE_KINDS
)
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
    return node_text(callee, source).rsplit("::", 1)[-1]


def references(root: Any, source: bytes) -> tuple[Reference, ...]:
    result = []
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type in _CALL_NODES:
            name = _callee_name(node, source)
            if name:
                result.append(Reference(name, node.start_byte, node.end_byte, "call"))
        elif node.type in _NAME_NODES and not node.named_children:
            result.append(
                Reference(node_text(node, source).rsplit("::", 1)[-1], node.start_byte, node.end_byte, "name")
            )
        pending.extend(reversed(node.named_children))
    return tuple(result)


def has_implementation(symbol: Symbol, language: str) -> bool:
    if symbol.body is None:
        return False
    if symbol.kind not in CALLABLE_KINDS:
        return True
    if language == "rust" and symbol.node.type == "function_item":
        return True
    if symbol.body.type not in {"block", "statement_block"}:
        return True
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
        if current.type in _NAME_NODES and not current.named_children and node_text(current, source) in _SENTINEL_NAMES:
            return True
        pending.extend(current.named_children)
    return False


def _mutates_stateful_target(node: Any) -> bool:
    target = node.child_by_field_name("left") or node.child_by_field_name("target")
    if target is None and node.named_children:
        target = node.named_children[0]
    if target is None:
        return False
    pending = [target]
    while pending:
        current = pending.pop()
        if current.type in _STATEFUL_TARGET_NODES:
            return True
        pending.extend(current.named_children)
    return False


def _effect_operation_count(body: Any, limit: int = 2) -> int:
    """Count visible effect-shaped operations owned by one callable."""
    pending = list(reversed(body.named_children))
    count = 0
    while pending:
        current = pending.pop()
        if current.type in _CALLABLE_NODE_TYPES:
            continue
        if current.type in _CALL_NODES or (current.type in _STATE_MUTATION_NODES and _mutates_stateful_target(current)):
            count += 1
            if count >= limit:
                return count
            continue
        pending.extend(current.named_children)
    return count


def syntax_facts(symbol: Symbol, source: bytes, language: str) -> tuple[SyntaxFact, ...]:
    """Record syntactic admission facts; semantic classification remains with Jev."""
    node_types = _syntax_node_types(symbol.node)
    facts: set[SyntaxFact] = set()
    if node_types & _VALIDATION_NODES:
        facts.add("validation_candidate")
    if node_types & _FALLBACK_NODES or _contains_sentinel(symbol.node, source):
        facts.add("fallback_candidate")
    if symbol.kind in CALLABLE_KINDS and has_implementation(symbol, language):
        facts.add("executable_behavior")
        if symbol.body is not None and _effect_operation_count(symbol.body) >= 2:
            facts.add("state_transition_candidate")
    return tuple(sorted(facts))


def _calls_by_callable(units: list[Unit], occurrences: tuple[Reference, ...]) -> dict[str, set[str]]:
    callables = (unit for unit in units if unit.kind in CALLABLE_KINDS and unit.has_implementation)
    calls = (reference for reference in occurrences if reference.kind == "call")
    result: dict[str, set[str]] = defaultdict(set)
    for reference, owner in owned_references(callables, calls):
        if owner is not None:
            result[owner.id].add(reference.name)
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


def finalize_units(
    units: list[Unit], member_counts: dict[str, int], occurrences: tuple[Reference, ...]
) -> tuple[Unit, ...]:
    children_by_parent: dict[str, list[Unit]] = defaultdict(list)
    for child in units:
        if child.parent_id:
            children_by_parent[child.parent_id].append(child)
    calls_by_callable = _calls_by_callable(units, occurrences)
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
