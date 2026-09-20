"""Integration tests exercise the real pinned grammars, never a simulated syntax tree."""

import json
from pathlib import Path

import pytest

from jevscan.cli.main import main
from jevscan.core.languages import language_for
from jevscan.core.models import FileJob, Kind
from jevscan.core.parser import _rust_callable_start, parse_batch, parse_source

pytestmark = [pytest.mark.parser, pytest.mark.usefixtures("grammar_runtime")]


def parse_fixture(path: Path):
    spec = language_for(path)
    assert spec is not None
    job = FileJob(str(path), path.name, spec.grammar, spec.language)
    parsed = parse_source(path.read_bytes(), job)
    assert not parsed.failed, parsed.diagnostics
    return parsed


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (
            "sample.py",
            {
                ("standalone", Kind.FUNCTION),
                ("Service", Kind.CLASS),
                ("Service.__init__", Kind.METHOD),
                ("Service.create", Kind.METHOD),
                ("Service.normalize", Kind.METHOD),
                ("Service.load", Kind.METHOD),
                ("Service.load.inner", Kind.FUNCTION),
                ("Service.Nested", Kind.CLASS),
                ("Service.Nested.run", Kind.METHOD),
            },
        ),
        (
            "sample.rs",
            {
                ("standalone", Kind.FUNCTION),
                ("Service", Kind.STRUCT),
                ("State", Kind.ENUM),
                ("Store", Kind.TRAIT),
                ("Store::load", Kind.METHOD),
                ("Store::ready", Kind.METHOD),
                ("impl Service", Kind.IMPL),
                ("impl Service::new", Kind.METHOD),
                ("impl Service::fetch", Kind.METHOD),
                ("impl Store for Service", Kind.IMPL),
                ("impl Store for Service::load", Kind.METHOD),
                ("nested", Kind.MODULE),
                ("nested::helper", Kind.FUNCTION),
            },
        ),
        (
            "sample.pm",
            {
                ("Service", Kind.PACKAGE),
                ("Service::new", Kind.FUNCTION),
                ("Service::load", Kind.FUNCTION),
                ("Other", Kind.PACKAGE),
                ("Other::helper", Kind.FUNCTION),
            },
        ),
        (
            "native_class.pl",
            {
                ("Counter", Kind.CLASS),
                ("Counter::value", Kind.METHOD),
                ("Counter::increment", Kind.METHOD),
            },
        ),
        (
            "sample.ts",
            {
                ("standalone", Kind.FUNCTION),
                ("increment", Kind.FUNCTION),
                ("Service", Kind.CLASS),
                ("Service.constructor", Kind.METHOD),
                ("Service.create", Kind.METHOD),
                ("Service.load", Kind.METHOD),
                ("Service.transform", Kind.METHOD),
                ("Store", Kind.INTERFACE),
                ("Store.load", Kind.METHOD),
                ("Identifier", Kind.TYPE),
                ("Base", Kind.CLASS),
                ("Base.work", Kind.METHOD),
            },
        ),
        (
            "sample.js",
            {
                ("standalone", Kind.FUNCTION),
                ("increment", Kind.FUNCTION),
                ("normalize", Kind.FUNCTION),
                ("Service", Kind.CLASS),
                ("Service.constructor", Kind.METHOD),
                ("Service.create", Kind.METHOD),
                ("Service.load", Kind.METHOD),
                ("Service.transform", Kind.METHOD),
            },
        ),
        ("view.tsx", {("View", Kind.FUNCTION), ("Card", Kind.FUNCTION)}),
        ("view.jsx", {("View", Kind.FUNCTION), ("Card", Kind.FUNCTION)}),
    ],
)
def test_real_language_units(fixture_dir: Path, filename: str, expected: set[tuple[str, Kind]]) -> None:
    parsed = parse_fixture(fixture_dir / filename)
    found = {(unit.qualified_name, unit.kind) for unit in parsed.units}
    assert expected <= found
    by_id = {unit.id: unit for unit in parsed.units}
    for unit in parsed.units:
        assert parsed.source[unit.start_byte : unit.end_byte].decode("utf-8")
        if unit.parent_id:
            parent = by_id[unit.parent_id]
            assert parent.start_byte <= unit.start_byte < unit.end_byte <= parent.end_byte


def test_decorators_nested_owners_and_unicode_byte_ranges() -> None:
    source = "# äöü\nclass Service:\n    @classmethod\n    def create(cls):\n        return '✓'\n".encode()
    parsed = parse_source(source, FileJob("sample.py", "sample.py", "python", "python"))
    assert not parsed.failed
    method = next(unit for unit in parsed.units if unit.name == "create")
    assert method.start_line == 3
    assert method.end_line == 5
    assert source[method.start_byte : method.end_byte].decode().startswith("@classmethod")
    assert method.parent_id == parsed.units[0].id


@pytest.mark.parametrize(
    ("grammar", "language", "source"),
    [
        ("python", "python", "def work(x):\n    if x is None:\n        return 0\n    return x\n"),
        ("rust", "rust", "fn work(x: i32) -> i32 { if x < 0 { return 0; } x }\n"),
        ("javascript", "javascript", "function work(x) { if (x === null) return 0; return x; }\n"),
        (
            "typescript",
            "typescript",
            "function work(x: number | null): number { if (x === null) return 0; return x; }\n",
        ),
        ("perl", "perl", "sub work { my ($x) = @_; if ($x) { return 0; } return $x; }\n"),
    ],
)
def test_common_validation_and_fallback_constructs_are_admitted(grammar: str, language: str, source: str) -> None:
    parsed = parse_source(source.encode(), FileJob("sample", "sample", grammar, language))
    assert not parsed.failed, parsed.diagnostics
    facts = next(unit.syntax_facts for unit in parsed.units if unit.name == "work")
    assert {"validation_candidate", "fallback_candidate"} <= set(facts)


@pytest.mark.parametrize(
    ("grammar", "language", "source"),
    [
        ("python", "python", "def unavailable():\n    return None\n"),
        ("rust", "rust", "fn unavailable() -> Option<i32> { None }\n"),
        ("javascript", "javascript", "function unavailable() { return undefined; }\n"),
        ("typescript", "typescript", "function unavailable(): number | undefined { return undefined; }\n"),
        ("perl", "perl", "sub unavailable { return undef; }\n"),
    ],
)
def test_direct_sentinel_returns_are_fallback_candidates(grammar: str, language: str, source: str) -> None:
    parsed = parse_source(source.encode(), FileJob("sample", "sample", grammar, language))
    assert not parsed.failed, parsed.diagnostics
    facts = next(unit.syntax_facts for unit in parsed.units if unit.name == "unavailable")
    assert "fallback_candidate" in facts


@pytest.mark.parametrize(
    ("grammar", "language", "owner", "source"),
    [
        (
            "python",
            "python",
            "Helpers",
            "class Helpers:\n def public(self): return self.helper()\n def helper(self): return 1\n",
        ),
        (
            "rust",
            "rust",
            "impl Helpers",
            "struct Helpers; impl Helpers { fn public(&self) -> i32 { self.helper() } fn helper(&self) -> i32 { 1 } }\n",
        ),
        (
            "javascript",
            "javascript",
            "Helpers",
            "class Helpers { public() { return this.helper(); } helper() { return 1; } }\n",
        ),
        (
            "typescript",
            "typescript",
            "Helpers",
            "class Helpers { public(): number { return this.helper(); } helper(): number { return 1; } }\n",
        ),
        (
            "perl",
            "perl",
            "Helpers",
            "package Helpers; sub public { return helper(); } sub helper { return 1; }\n",
        ),
    ],
)
def test_helper_relationship_recognizes_visible_sibling_calls(
    grammar: str, language: str, owner: str, source: str
) -> None:
    parsed = parse_source(source.encode(), FileJob("sample", "sample", grammar, language))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert "helper_relationship" in units[owner].syntax_facts


def test_helper_relationship_requires_a_visible_sibling_call() -> None:
    source = "class Independent:\n def first(self): return 1\n def second(self): return 2\n"
    parsed = parse_source(source.encode(), FileJob("sample.py", "sample.py", "python", "python"))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert "helper_relationship" not in units["Independent"].syntax_facts


def test_syntax_error_is_incomplete_not_clean() -> None:
    parsed = parse_source(b"def broken(:\n", FileJob("broken.py", "broken.py", "python", "python"))
    assert parsed.failed
    assert not parsed.units
    assert parsed.diagnostics[0].code == "syntax-error"
    assert parsed.diagnostics[0].incomplete


@pytest.mark.parametrize(
    ("source", "name", "kind", "unit_text", "body_text"),
    [
        (
            'declare module "pkg" {\n  export type * from "pkg";\n  export function run(value: number): void { return; }\n}\n',
            "run",
            Kind.FUNCTION,
            "function run(value: number): void { return; }",
            "{ return; }",
        ),
        (
            (
                'class Service {\n  run(value: readonly import("./types").Result[]): import("./types").Result {\n'
                "    return value[0];\n"
                "  }\n"
                "}\n"
            ),
            "run",
            Kind.METHOD,
            'run(value: readonly import("./types").Result[]): import("./types").Result {\n    return value[0];\n  }',
            "{\n    return value[0];\n  }",
        ),
        (
            (
                'function execute(value: Parameters<NonNullable<import("pkg").ToolContext["approval"]>>[0]): void {\n'
                "  return;\n"
                "}\n"
            ),
            "execute",
            Kind.FUNCTION,
            (
                'function execute(value: Parameters<NonNullable<import("pkg").ToolContext["approval"]>>[0]): void {\n'
                "  return;\n"
                "}"
            ),
            "{\n  return;\n}",
        ),
        (
            (
                'async function load(): Promise<{ readonly loaded: readonly import("./types").Result[]; '
                'readonly failures: readonly import("./types").Failure[] }> {\n'
                '  throw new Error("fixture");\n'
                "}\n"
            ),
            "load",
            Kind.FUNCTION,
            (
                'async function load(): Promise<{ readonly loaded: readonly import("./types").Result[]; '
                'readonly failures: readonly import("./types").Failure[] }> {\n'
                '  throw new Error("fixture");\n'
                "}"
            ),
            '{\n  throw new Error("fixture");\n}',
        ),
    ],
)
def test_typescript_type_only_errors_recover_units_and_spans(
    source: str, name: str, kind: Kind, unit_text: str, body_text: str
) -> None:
    encoded = source.encode()
    parsed = parse_source(encoded, FileJob("recovered.ts", "recovered.ts", "typescript", "typescript"))
    assert not parsed.failed
    assert parsed.source == encoded
    diagnostic = next(diagnostic for diagnostic in parsed.diagnostics if diagnostic.code == "typescript-type-recovery")
    assert "type-only syntax was recovered" in diagnostic.message
    assert "reference/type extraction may be incomplete" in diagnostic.message
    assert not diagnostic.incomplete
    unit = next(unit for unit in parsed.units if unit.name == name)
    assert unit.kind == kind
    assert unit.start_byte == encoded.index(unit_text.encode())
    assert unit.end_byte == unit.start_byte + len(unit_text.encode())
    assert encoded[unit.start_byte : unit.end_byte].decode() == unit_text
    assert unit.has_body
    assert encoded[unit.body_start_byte : unit.body_end_byte].decode() == body_text


@pytest.mark.parametrize(
    "source",
    [
        "function broken(value: number): void { return value;\n",
        "function broken(value: Box<): void { return; }\n",
    ],
)
def test_typescript_unrecognized_errors_remain_fatal(source: str) -> None:
    parsed = parse_source(source.encode(), FileJob("broken.ts", "broken.ts", "typescript", "typescript"))
    assert parsed.failed
    assert not parsed.units
    assert parsed.diagnostics[0].code == "syntax-error"
    assert parsed.diagnostics[0].incomplete


def test_typescript_direct_object_bindings_attribute_arrow_units() -> None:
    source = "const bashTool = defineTool({ execute: async () => 1 });\nconst editTool = { execute: () => 2 };\n"
    encoded = source.encode()
    parsed = parse_source(encoded, FileJob("tools.ts", "tools.ts", "typescript", "typescript"))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert {"bashTool.execute", "editTool.execute"} <= units.keys()
    assert units["bashTool.execute"].name == units["editTool.execute"].name == "execute"
    for qualified_name in ("bashTool.execute", "editTool.execute"):
        unit = units[qualified_name]
        assert encoded[unit.start_byte : unit.end_byte].decode() in {"async () => 1", "() => 2"}
        assert unit.display_name == qualified_name
    assert units["bashTool.execute"].start_byte != units["editTool.execute"].start_byte


def test_typescript_nested_object_bindings_compose_lexical_owners() -> None:
    source = (
        "function owner() {\n"
        "  const first = { execute: () => 1 };\n"
        "  const second = defineTool({ execute: () => 2 });\n"
        "}\n"
        "class Service { work() { const local = { execute: () => 3 }; } }\n"
    )
    parsed = parse_source(source.encode(), FileJob("nested.ts", "nested.ts", "typescript", "typescript"))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert {"owner.first.execute", "owner.second.execute", "Service.work.local.execute"} <= units.keys()
    assert all(units[name].name == "execute" for name in units if name.endswith(".execute"))


def test_rust_empty_callable_blocks_are_implementations() -> None:
    source = (
        "trait Store { fn required(&self); fn defaulted(&self) {} }\n"
        "struct Boxed<T>(T);\n"
        "impl<T> Store for Boxed<T> { fn required(&self) {} }\n"
        "fn empty() {}\n"
    )
    parsed = parse_source(source.encode(), FileJob("empty.rs", "empty.rs", "rust", "rust"))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert not units["Store::required"].has_implementation
    assert units["Store::defaulted"].has_implementation
    assert units["impl Store for Boxed<T>::required"].has_implementation
    assert units["empty"].has_implementation


def test_rust_tuple_closure_bindings_require_exact_positions() -> None:
    direct = parse_source(
        b"fn owner() { let (a, b) = (|| 1, || 2); }",
        FileJob("tuple.rs", "tuple.rs", "rust", "rust"),
    )
    assert not direct.failed
    units = {unit.qualified_name: unit for unit in direct.units}
    assert units["owner::a"].name == "a"
    assert units["owner::b"].name == "b"

    ambiguous = parse_source(
        b"fn owner() { let (a, b) = (|| 1, || 2, || 3); let (a, ..) = (|| 1, || 2); }",
        FileJob("ambiguous.rs", "ambiguous.rs", "rust", "rust"),
    )
    assert not ambiguous.failed
    closures = [unit for unit in ambiguous.units if unit.kind == Kind.CLOSURE]
    assert closures
    assert all(unit.name.startswith("<anonymous@") for unit in closures)


def test_rust_type_cast_closure_binding_is_exact() -> None:
    parsed = parse_source(
        b"fn owner() { let f = (|| 1) as fn() -> i32; let value = 1 as i32; }",
        FileJob("cast.rs", "cast.rs", "rust", "rust"),
    )
    assert not parsed.failed
    assert {unit.qualified_name for unit in parsed.units} >= {"owner", "owner::f"}
    assert not any(unit.qualified_name == "owner::value" for unit in parsed.units)


def test_rust_attributes_extend_callable_spans() -> None:
    source = (
        '#[cfg(feature="x")]\n'
        "#[inline]\n"
        "pub fn foo() {}\n"
        "struct Service;\n"
        "impl Service {\n"
        "  #[must_use]\n"
        "  fn work(&self) {}\n"
        "}\n"
    )
    encoded = source.encode()
    parsed = parse_source(encoded, FileJob("attributes.rs", "attributes.rs", "rust", "rust"))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    top_level = units["foo"]
    assert top_level.start_line == 1
    assert (
        encoded[top_level.start_byte : top_level.end_byte].decode() == '#[cfg(feature="x")]\n#[inline]\npub fn foo() {}'
    )
    assert top_level.signature == '#[cfg(feature="x")]\n#[inline]\npub fn foo()'
    assert top_level.id == f"attributes.rs:{top_level.start_byte}:function"
    method = units["impl Service::work"]
    assert method.start_line == 6
    assert encoded[method.start_byte : method.end_byte].decode() == "#[must_use]\n  fn work(&self) {}"
    assert method.signature == "#[must_use]\n  fn work(&self)"
    assert method.id == f"attributes.rs:{method.start_byte}:method"


def test_rust_attribute_attachment_is_local_to_each_callable() -> None:
    source = (
        "#[first]\n"
        "#[second]\n"
        "fn first() {}\n"
        "fn adjacent() {}\n"
        "#[outer]\n"
        "// comments interrupt attribute attachment\n"
        "#[inner]\n"
        "fn commented() {}\n"
    )
    encoded = source.encode()
    parsed = parse_source(encoded, FileJob("adjacent.rs", "adjacent.rs", "rust", "rust"))
    assert not parsed.failed
    units = {unit.name: unit for unit in parsed.units}

    assert encoded[units["first"].start_byte : units["first"].end_byte].decode() == (
        "#[first]\n#[second]\nfn first() {}"
    )
    assert encoded[units["adjacent"].start_byte : units["adjacent"].end_byte].decode() == "fn adjacent() {}"
    assert encoded[units["commented"].start_byte : units["commented"].end_byte].decode() == (
        "#[inner]\nfn commented() {}"
    )


def test_rust_callable_start_does_not_scan_parent_siblings() -> None:
    class Parent:
        @property
        def named_children(self) -> None:
            raise AssertionError("_rust_callable_start must not materialize all named siblings")

    parent = Parent()

    class Node:
        def __init__(self, node_type: str, start_byte: int, previous: "Node | None" = None) -> None:
            self.type = node_type
            self.start_byte = start_byte
            self.parent = parent
            self._previous = previous
            self.lookups = 0

        @property
        def prev_named_sibling(self) -> "Node | None":
            self.lookups += 1
            return self._previous

    previous: Node | None = None
    for position in range(4_000):
        previous = Node("ordinary_item", position, previous)
    callable_node = Node("function_item", 4_000, previous)

    assert _rust_callable_start(callable_node) == 4_000
    assert callable_node.lookups == 1


def test_rust_union_and_foreign_functions_keep_distinct_inventory_kinds() -> None:
    source = (
        "union Packet { value: u32, ptr: *const u8 }\n"
        'extern "C" {\n'
        '  #[cfg(feature = "ffi")]\n'
        "  fn ffi(value: i32);\n"
        "}\n"
        "trait Store {\n"
        "  #[must_use]\n"
        "  fn required(&self);\n"
        "}\n"
    )
    encoded = source.encode()
    parsed = parse_source(encoded, FileJob("inventory.rs", "inventory.rs", "rust", "rust"))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert units["Packet"].kind == Kind.UNION
    assert units["ffi"].kind == Kind.FUNCTION
    assert not units["ffi"].has_body
    assert not units["ffi"].has_implementation
    assert units["ffi"].start_line == 3
    assert encoded[units["ffi"].start_byte : units["ffi"].end_byte].decode().startswith("#[cfg")
    assert units["ffi"].signature.startswith("#[cfg")
    assert units["Store::required"].kind == Kind.METHOD
    assert units["Store::required"].start_line == 7
    assert (
        encoded[units["Store::required"].start_byte : units["Store::required"].end_byte]
        .decode()
        .startswith("#[must_use]")
    )


def test_rust_async_blocks_are_closures_without_duplicate_async_closures() -> None:
    parsed = parse_source(
        b"fn owner() { let assigned = async move { 1 }; async move { 2 }; let closure = async move |x: i32| x; }",
        FileJob("async.rs", "async.rs", "rust", "rust"),
    )
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert units["owner::assigned"].kind == Kind.CLOSURE
    assert units["owner::closure"].name == "closure"
    assert sum(unit.kind == Kind.CLOSURE for unit in parsed.units) == 2


def test_oversized_and_non_utf8_files_are_visible(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_bytes(b"\xff\xfe")
    job = FileJob(str(path), path.name, "python", "python")
    assert parse_batch([job], 100, 10)[0].diagnostics[0].code == "file-read-error"
    path.write_bytes(b"x" * 101)
    assert parse_batch([job], 100, 10)[0].diagnostics[0].code == "file-size-limit"


def test_real_spawn_process_pipeline(fixture_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = main([str(fixture_dir), "--offline", "--jobs", "2", "--format", "jsonl"])
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert result == 0, output
    assert output[-1]["files_parsed"] == 8
    assert output[-1]["units_found"] >= 40
    assert output[-1]["requests"] == 0
    assert {event["language"] for event in output if event["event"] == "file"} == {
        "python",
        "rust",
        "perl",
        "typescript",
        "javascript",
    }


def test_perl_block_namespace_restores_enclosing_package() -> None:
    source = b"package Outer;\nsub before { 1 }\npackage Inner { sub work { 2 } }\nsub after { 3 }\n"
    parsed = parse_source(source, FileJob("nested.pm", "nested.pm", "perl", "perl"))
    assert not parsed.failed
    units = {unit.qualified_name: unit for unit in parsed.units}
    assert {"Outer", "Outer::before", "Inner", "Inner::work", "Outer::after"} <= units.keys()
    assert units["Outer::after"].parent_id == units["Outer"].id
    assert units["Inner::work"].parent_id == units["Inner"].id


def test_callback_display_roles_do_not_replace_lexical_identity():
    from jevscan.core.models import FileJob, Kind, Target
    from jevscan.core.parser import parse_source

    # Bun/React/Promise patterns from the reported TypeScript project, without its runtime dependencies.
    source = """describe("mutation renderer", () => {
  test("mounts through the boundary", async () => {
    await new Promise(resolve => queueMicrotask(() => resolve(1)));
  });
});
class Owner { pump() { Promise.resolve().then(() => 1).then(value => value, error => error); } }
"""
    parsed = parse_source(source.encode(), FileJob("callbacks.ts", "callbacks.ts", "typescript", "typescript"))
    assert not parsed.failed
    closures = [unit for unit in parsed.units if unit.kind == Kind.CLOSURE]
    labels = [unit.display_name for unit in closures]
    assert any('describe["mutation renderer"].test["mounts through the boundary"]' in name for name in labels)
    assert any("Promise[arg1].queueMicrotask[arg1]" in name for name in labels)
    assert any("Owner.pump.then[arg2]" in name for name in labels)
    assert all(unit.name.startswith("<anonymous@") for unit in closures)
    assert all(Target.from_unit(unit).display_name == unit.display_name for unit in closures)
    assert all(unit.id == f"callbacks.ts:{unit.start_byte}:{unit.kind}" for unit in closures)
