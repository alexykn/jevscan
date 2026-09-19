"""Integration tests exercise the real pinned grammars, never a simulated syntax tree."""

import json
from pathlib import Path

import pytest

from jevscan.cli.main import main
from jevscan.core.languages import language_for
from jevscan.core.models import FileJob, Kind
from jevscan.core.parser import parse_batch, parse_source

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
