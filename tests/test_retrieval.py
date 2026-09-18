"""The evidence index uses syntax, bounded source snapshots, and safe local paths."""

from pathlib import Path

import pytest

from jevscan.core.config import Config, EnrichmentConfig, ScanConfig
from jevscan.core.context import ContextBuilder
from jevscan.core.models import FileJob
from jevscan.core.parser import parse_source
from jevscan.core.planning import Planner
from jevscan.core.retrieval import SourceIndex, _read_source
from jevscan.core.rules import Rule

pytestmark = [pytest.mark.parser, pytest.mark.usefixtures("grammar_runtime")]


@pytest.mark.parametrize(
    "language,extension,source,caller",
    [
        (
            "python",
            ".py",
            "def target(x): return x\n",
            "def use(): return target(1)\n# target(99)\nS = 'target(123)'\n",
        ),
        (
            "javascript",
            ".js",
            "function target(x) { return x; }",
            "function use() { return target(1); } // target(99)\nconst s = 'target(123)';",
        ),
        (
            "typescript",
            ".ts",
            "function target(x: number) { return x; }",
            "function use() { return target(1); } // target(99)\nconst s = 'target(123)';",
        ),
        (
            "rust",
            ".rs",
            "fn target(x: i32) -> i32 { x }",
            'fn use_it() { target(1); } // target(99)\nconst S: &str = "target(123)";',
        ),
        (
            "perl",
            ".pm",
            "sub target { return $_[0]; }",
            "sub use_it { target(1); } # target(99)\nmy $s = 'target(123)';",
        ),
    ],
)
async def test_calls_are_syntax_occurrences_not_comment_or_string_matches(
    tmp_path, basic_rule, language, extension, source, caller
):
    path = "target" + extension
    (tmp_path / path).write_text(source)
    (tmp_path / ("caller" + extension)).write_text(caller)
    parsed = parse_source(source.encode(), FileJob(path, path, language, language))
    context = ContextBuilder(parsed)
    planner = Planner(context, Config(rules={"rule": basic_rule}))
    check = next(check for check in planner.checks if check.target.qualified_name == "target")
    index = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig())
    result = await index.candidates(context, check, context.requested(check), "callers")
    assert len(result.items) == 1 and result.items[0].target.path == "caller" + extension
    assert result.items[0].relation == "possible_call_site"
    assert "target(1)" in result.items[0].document()["content"]
    calls = [ref for ref in result.items[0].snapshot.parsed.references if ref.kind == "call"]
    assert len(calls) == 1


async def test_filters_limits_and_safe_reads(tmp_path: Path, basic_rule: Rule) -> None:
    outside = tmp_path.parent / "not-evidence.py"
    outside.write_text("def steal(): return target('SECRET')\n")
    (tmp_path / "linked.py").symlink_to(outside)
    (tmp_path / "ignored.py").write_text(outside.read_text())
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "caller.py").write_text("def use(): return target(1)\n")
    source = "def target(v): return v\n"
    parsed = parse_source(source.encode(), FileJob("target.py", "target.py", "python", "python"))
    context = ContextBuilder(parsed)
    check = Planner(context, Config(rules={"rule": basic_rule})).checks[0]
    index = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig())
    found = await index.candidates(context, check, context.requested(check), "callers")
    assert [item.target.path for item in found.items] == ["caller.py"]
    with pytest.raises(OSError):
        _read_source(tmp_path, "linked.py", 1000)
    with pytest.raises(OSError):
        _read_source(tmp_path, "../not-evidence.py", 1000)
    (tmp_path / "linked_dir").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(OSError):
        _read_source(tmp_path, "linked_dir/not-evidence.py", 1000)
    small = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig(max_source_bytes=1024))
    (tmp_path / "huge.py").write_text('DATA = "' + "x" * 5000 + '"')
    found = await small.candidates(context, check, context.requested(check), "callers")
    assert not found.coverage["discovery_complete"]
    assert found.coverage["source_bytes"] <= 1025


async def test_snapshots_are_immutable_and_changed_primary_is_not_mixed(tmp_path, basic_rule):
    (tmp_path / "caller.py").write_text("def use(): return work(1)\n")
    source = "def work(v): return v\n"
    (tmp_path / "target.py").write_text(source + "def more(): return work(2)\n")
    parsed = parse_source(source.encode(), FileJob("target.py", "target.py", "python", "python"))
    context = ContextBuilder(parsed)
    check = Planner(context, Config(rules={"rule": basic_rule})).checks[0]
    index = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig())
    found = await index.candidates(context, check, context.requested(check), "callers")
    assert found.coverage["primary_snapshot_changed"]
    assert [item.target.path for item in found.items] == ["caller.py"]
    before = found.items[0].document()
    (tmp_path / "caller.py").write_text("def use(): return work(99)\n")
    assert found.items[0].document() == before


async def test_cross_file_definitions_tests_and_rust_impl_candidates(tmp_path, basic_rule):
    (tmp_path / "contract.py").write_text("class Contract:\n    allowed = True\n")
    (tmp_path / "test_work.py").write_text("def test_work(): assert work(1) == 1\n")
    source = "def work(x: Contract): return x\n"
    parsed = parse_source(source.encode(), FileJob("target.py", "target.py", "python", "python"))
    context = ContextBuilder(parsed)
    check = Planner(context, Config(rules={"rule": basic_rule})).checks[0]
    index = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig())
    evidence = context.requested(check)
    definitions = await index.candidates(context, check, evidence, "definitions")
    tests = await index.candidates(context, check, evidence, "tests")
    assert [item.target.qualified_name for item in definitions.items] == ["Contract"]
    assert [item.target.qualified_name for item in tests.items] == ["test_work"]
    source = "struct S; impl S { fn work(&self) { self.validate(); } }"
    (tmp_path / "other.rs").write_text("impl S { fn validate(&self) { assert!(true); } }")
    parsed = parse_source(source.encode(), FileJob("target.rs", "target.rs", "rust", "rust"))
    context = ContextBuilder(parsed)
    check = next(
        check
        for check in Planner(context, Config(rules={"rule": basic_rule})).checks
        if check.target.qualified_name == "impl S::work"
    )
    index = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig())
    definitions = await index.candidates(context, check, context.requested(check), "definitions")
    assert any(item.relation == "possible_sibling_impl" for item in definitions.items)


def test_expression_bodies_are_implementations_not_stubs():
    for language, source in (
        ("python", "f = lambda: 1\n"),
        ("typescript", "const f = () => 1;"),
        ("rust", "fn main() { let f = || 1; }"),
    ):
        parsed = parse_source(source.encode(), FileJob("sample", "sample", language, language))
        assert not parsed.failed
        assert all(unit.has_implementation for unit in parsed.units)


async def test_catalogue_limits_and_concurrent_single_load(tmp_path, basic_rule, monkeypatch):
    import asyncio
    import threading

    from jevscan.core import retrieval

    for index in range(4):
        (tmp_path / f"caller{index}.py").write_text(f"def use(): return work({index})\n")
    parsed = parse_source(b"def work(v): return v\n", FileJob("target.py", "target.py", "python", "python"))
    context = ContextBuilder(parsed)
    check = Planner(context, Config(rules={"rule": basic_rule})).checks[0]
    index = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig(max_candidates=1, max_evidence=1, max_source_files=3))
    found = await index.candidates(context, check, context.requested(check), "callers")
    assert found.coverage["files_read"] == 3 and not found.coverage["discovery_complete"]
    assert len(found.items) == 1 and found.coverage["candidate_limit_omissions"] == 2

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    builds = []
    original = retrieval._catalogue

    def blocked(*args):
        builds.append(1)
        started.set()
        assert release.wait(5)
        result = original(*args)
        finished.set()
        return result

    monkeypatch.setattr(retrieval, "_catalogue", blocked)
    index = SourceIndex(tmp_path, ScanConfig(), EnrichmentConfig())
    task = asyncio.create_task(index._load())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # Cancellation must wait for the filesystem operation.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    first, second = await asyncio.gather(index._load(), index._load())
    assert first is second and len(builds) == 2  # One cancelled load, one shared subsequent load.
