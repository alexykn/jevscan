"""Language-specific syntax mapped onto the shared unit model; no Python AST frontend."""

from dataclasses import dataclass
from pathlib import Path

from jevscan.core.models import Kind


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    language: str
    grammar: str
    nodes: dict[str, Kind]
    required: frozenset[str]
    imports: tuple[str, ...]
    branches: tuple[str, ...]


_JS_NODES = {
    "function_declaration": Kind.FUNCTION,
    "generator_function_declaration": Kind.FUNCTION,
    "function_expression": Kind.CLOSURE,
    "generator_function": Kind.CLOSURE,
    "arrow_function": Kind.CLOSURE,
    "method_definition": Kind.METHOD,
    "class_declaration": Kind.CLASS,
    "class": Kind.CLASS,
}
_JS_BRANCHES = (
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "switch_case",
    "catch_clause",
    "ternary_expression",
)

SPECS = {
    "python": LanguageSpec(
        "python",
        "python",
        {"function_definition": Kind.FUNCTION, "class_definition": Kind.CLASS},
        frozenset({"function_definition", "class_definition"}),
        ("import_statement", "import_from_statement"),
        ("if_statement", "elif_clause", "for_statement", "while_statement", "except_clause", "case_clause"),
    ),
    "rust": LanguageSpec(
        "rust",
        "rust",
        {
            "function_item": Kind.FUNCTION,
            "function_signature_item": Kind.METHOD,
            "struct_item": Kind.STRUCT,
            "union_item": Kind.UNION,
            "enum_item": Kind.ENUM,
            "trait_item": Kind.TRAIT,
            "impl_item": Kind.IMPL,
            "mod_item": Kind.MODULE,
            "type_item": Kind.TYPE,
            "closure_expression": Kind.CLOSURE,
            "async_block": Kind.CLOSURE,
        },
        frozenset({
            "function_item",
            "struct_item",
            "union_item",
            "enum_item",
            "trait_item",
            "impl_item",
            "async_block",
        }),
        ("use_declaration",),
        ("if_expression", "match_arm", "for_expression", "while_expression", "loop_expression"),
    ),
    "perl": LanguageSpec(
        "perl",
        "perl",
        {
            "subroutine_declaration_statement": Kind.FUNCTION,
            "method_declaration_statement": Kind.METHOD,
            "anonymous_subroutine_expression": Kind.CLOSURE,
            "package_statement": Kind.PACKAGE,
            "class_statement": Kind.CLASS,
        },
        frozenset({"subroutine_declaration_statement", "package_statement"}),
        ("use_statement",),
        ("conditional_statement", "loop_statement", "for_statement", "cstyle_for_statement", "try_statement"),
    ),
    "javascript": LanguageSpec(
        "javascript",
        "javascript",
        _JS_NODES,
        frozenset({"function_declaration", "class_declaration", "method_definition", "arrow_function"}),
        ("import_statement",),
        _JS_BRANCHES,
    ),
    "typescript": LanguageSpec(
        "typescript",
        "typescript",
        {
            **_JS_NODES,
            "abstract_class_declaration": Kind.CLASS,
            "interface_declaration": Kind.INTERFACE,
            "type_alias_declaration": Kind.TYPE,
            "internal_module": Kind.MODULE,
            "method_signature": Kind.METHOD,
            "abstract_method_signature": Kind.METHOD,
            "function_signature": Kind.FUNCTION,
        },
        frozenset({"function_declaration", "class_declaration", "method_definition", "interface_declaration"}),
        ("import_statement",),
        _JS_BRANCHES,
    ),
}
SPECS["tsx"] = LanguageSpec(
    "typescript",
    "tsx",
    SPECS["typescript"].nodes,
    SPECS["typescript"].required,
    SPECS["typescript"].imports,
    SPECS["typescript"].branches,
)

EXTENSIONS = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".pl": "perl",
    ".pm": "perl",
    ".t": "perl",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


def language_for(path: Path) -> LanguageSpec | None:
    grammar = EXTENSIONS.get(path.suffix.lower())
    return SPECS[grammar] if grammar else None
