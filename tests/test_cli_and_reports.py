import io
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from jevscan.cli.main import main
from jevscan.cli.render import Reporter
from jevscan.core.models import Summary


def test_init_config_does_not_overwrite_and_resolved_config_is_available(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["--init-config"]) == 0
    original = (tmp_path / "jevscan.yaml").read_text()
    assert main(["--init-config"]) == 2
    assert (tmp_path / "jevscan.yaml").read_text() == original
    assert main(["--show-config"]) == 0
    assert "mixed-responsibilities" in capsys.readouterr().out
    assert main(["--init-config", "custom.yml"]) == 0
    assert main(["--show-config", "--config", "custom.yml"]) == 0
    assert main(["--init-config", "custom.toml"]) == 2
    assert not (tmp_path / "custom.toml").exists()


@pytest.mark.parametrize("format_name", ["json", "jsonl"])
def test_machine_reports_are_valid_and_preserve_literal_text(format_name: str) -> None:
    stream = io.StringIO()
    metadata = {"root": "/repo", "config": "default", "mode": "offline", "parser_processes": 2, "concurrency": 0}
    reporter = Reporter(stream, format_name, metadata)
    reporter.emit({"event": "diagnostic", "message": "[red] literal λ", "path": "x.py"})
    reporter.emit({"event": "summary", **asdict(Summary("offline"))})
    text = stream.getvalue()
    if format_name == "json":
        doc = json.loads(text)
        assert doc["events"][0]["message"] == "[red] literal λ"
        assert doc["summary"]["mode"] == "offline"
    else:
        events = [json.loads(line) for line in text.splitlines()]
        assert [event["event"] for event in events] == ["start", "diagnostic", "summary"]


def test_missing_parser_produces_incomplete_json_not_fake_success(tmp_path: Path, monkeypatch, capsys) -> None:
    from jevscan.core import scanner
    from jevscan.core.parser import ParserUnavailableError

    def unavailable() -> None:
        raise ParserUnavailableError("deliberate missing-dependency test")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scanner, "require_parser_runtime", unavailable)
    assert main([".", "--offline", "--format", "json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["incomplete"]
    assert report["summary"]["units_evaluated"] == 0


def test_output_cannot_overwrite_an_input(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "x.py"
    source.write_text("def f(): pass\n")
    assert main([str(source), "--offline", "-o", str(source)]) == 2
    assert source.read_text() == "def f(): pass\n"


@pytest.mark.parser
@pytest.mark.usefixtures("grammar_runtime")
def test_project_skill_workflow_captures_imports_and_updates_yaml(tmp_path: Path, monkeypatch) -> None:
    from functools import partial

    import httpx
    import yaml

    from jevscan.cli.calibrate import main as calibrate
    from jevscan.cli.import_calibration import main as import_capture
    from jevscan.core import scanner
    from jevscan.core.client import JevClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test-key")
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "alpha.py").write_text("def alpha():\n    return 1\n")
    (source_root / "beta.py").write_text("def beta():\n    return 2\n")
    rules = tmp_path / "project.yaml"
    rules.write_text(
        "# Keep this project comment.\n"
        "version: 4\n"
        "rulesets:\n"
        "  TEAM:\n"
        "    description: Workflow test\n"
        "enrichment:\n"
        "  enabled: false\n"
        "rules:\n"
        "  - name: TEAM01\n"
        "    ruleset: TEAM\n"
        "    applies_to: [function]\n"
        "    context: unit\n"
        "    question:\n"
        "      type: noul\n"
        "      instructions: Is the operation defective?\n"
        "    report:\n"
        "      message: Review this operation.\n"
        "      levels:\n"
        "        warning:\n"
        "          min_probability: 0.5 # Calibrate this value.\n"
        "        error:\n"
        "          min_probability: 0.95\n"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        source = "".join(document["content"] for document in body["state"]["documents"])
        probability = 0.9 if "def alpha" in source else 0.65
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {key: {"type": "noul", "noul": probability} for key in body["questions"]},
                "usage": {"input_tokens": 1},
            },
        )

    monkeypatch.setattr(scanner, "JevClient", partial(JevClient, transport=httpx.MockTransport(respond)))
    capture, report = tmp_path / "judgments.jsonl", tmp_path / "scan.json"
    assert (
        main([
            str(source_root),
            "--config",
            str(rules),
            "--rule",
            "TEAM",
            "--model",
            "jev-test",
            "--jobs",
            "1",
            "--no-cache",
            "--format",
            "json",
            "--output",
            str(report),
            "--calibration-output",
            str(capture),
        ])
        == 1
    )
    rows = [json.loads(line) for line in capture.read_text().splitlines()]
    judgments = [row for row in rows if row["kind"] == "judgment"]
    assert len(judgments) == 2
    assert all(row["rule_id"] == "TEAM01" for row in judgments)
    labels = tmp_path / "labels.yaml"
    labels.write_text(
        yaml.safe_dump({
            "cases": [
                {
                    "case_id": row["case_id"],
                    "label": "Agree" if row["target"]["qualified_name"] == "alpha" else "Disagree",
                    "source_group": row["target"]["path"],
                    "split": "development",
                    "explanation": "Synthetic label for mocked command-workflow verification, not semantic accuracy.",
                }
                for row in judgments
            ]
        })
    )
    cases = tmp_path / "cases.jsonl"
    assert import_capture([str(capture), "--labels", str(labels), "--output", str(cases)]) == 0
    audit = tmp_path / "audit.json"
    assert (
        calibrate([
            str(cases),
            "--select",
            "--rules",
            str(rules),
            "--apply",
            "--output",
            str(audit),
        ])
        == 0
    )
    written = rules.read_text()
    assert "# Keep this project comment." in written
    assert "# Calibrate this value." in written
    policy = yaml.safe_load(written)["rules"][0]["report"]
    assert policy["levels"]["warning"]["min_probability"] == 0.9
    assert policy["levels"]["error"]["min_probability"] == 0.95
    assert json.loads(audit.read_text())["writeback"]["applied"] is True
    assert '"content":' not in report.read_text()


def test_exit_codes_keep_operational_errors_distinct() -> None:
    summary = Summary("live", findings={"info": 0, "warning": 1, "error": 0})
    assert summary.exit_code("warning") == 1
    assert summary.exit_code("error") == 0
    assert summary.exit_code("never") == 0
    summary.incomplete = True
    assert summary.exit_code("never") == 2


def _report_metadata() -> dict[str, object]:
    return {
        "root": "/repo",
        "config": "default",
        "mode": "live",
        "model": "jev-latest",
        "parser_processes": 2,
        "concurrency": 4,
    }


def test_text_report_groups_all_answers_under_one_unit() -> None:
    stream = io.StringIO()
    reporter = Reporter(stream, "text", _report_metadata(), verbose=True, width=120)
    unit = {
        "path": "src/example.py",
        "start_line": 25,
        "kind": "method",
        "qualified_name": "ContextBuilder.__init__",
    }
    reporter.emit({
        "event": "evaluation",
        "target": unit,
        "cached": True,
        "rule_metadata": {
            name: {"title": "", "ruleset": "project"}
            for name in ("mixed-responsibilities", "unclear-control-flow", "redundant-validation")
        },
        "statuses": {
            "mixed-responsibilities": "error",
            "unclear-control-flow": "warning",
            "redundant-validation": "ok",
        },
        "uncertainty_reasons": {},
        "reviews": {},
        "scales": {"unclear-control-flow": 3},
        "tentative_findings": [],
        "answers": {
            "mixed-responsibilities": {"type": "noul", "noul": 0.21},
            "unclear-control-flow": {
                "type": "score",
                "score": 2.0,
                "confidence": 0.84,
                "probabilities": {"0": 0.01, "1": 0.15, "2": 0.74, "3": 0.10},
            },
            "redundant-validation": {
                "type": "choice",
                "choice": "justified_or_absent",
                "confidence": 0.74,
                "probabilities": {
                    "demonstrably_redundant": 0.10,
                    "justified_or_absent": 0.83,
                    "insufficient_context": 0.07,
                },
            },
        },
        "findings": [
            {
                "rule": "mixed-responsibilities",
                "severity": "error",
                "message": "Responsibilities are interleaved.",
            },
            {
                "rule": "unclear-control-flow",
                "severity": "warning",
                "message": "Control flow is difficult to follow.",
            },
        ],
    })

    text = stream.getvalue()
    assert text.count("src/example.py") == 1
    assert text.count("ContextBuilder.__init__") == 1
    assert "M 25 ContextBuilder.__init__  cached" in text
    assert "x mixed-responsibilities" in text and "noul=0.210" in text
    assert "! unclear-control-flow" in text and "score=2.000/3  conf=0.840" in text
    assert "redundant-validation" in text and "justified_or_absent  p=0.830  conf=0.740" in text
    assert "Responsibilities are interleaved." in text
    assert "Control flow is difficult to follow." in text
    assert "\x1b[" not in text


def test_text_report_colors_tty_output(monkeypatch) -> None:
    class TtyStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLOR", "auto")
    stream = TtyStream()
    reporter = Reporter(stream, "text", _report_metadata(), verbose=True, width=120)
    reporter.emit({
        "event": "unit",
        "unit": {
            "path": "src/example.py",
            "start_line": 7,
            "kind": "function",
            "qualified_name": "work",
        },
    })
    assert "\x1b[" in stream.getvalue()


def test_live_progress_is_ephemeral_tty_output_and_clears_before_results(monkeypatch) -> None:
    class TtyStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("COLOR", "no")
    stream = TtyStream()
    reporter = Reporter(stream, "text", _report_metadata(), width=120)
    reporter.emit({
        "event": "progress",
        "requests": 12,
        "completed_requests": 9,
        "cache_hits": 3,
        "input_tokens": 4200,
        "estimated_cost": 0.0123,
        "elapsed_seconds": 2.5,
    })
    reporter.emit(_evaluation_event("finished", "warning"))
    text = stream.getvalue()
    assert "working — requests=9/12" in text
    assert "\r" in text
    assert "finished" in text

    machine = io.StringIO()
    jsonl = Reporter(machine, "jsonl", _report_metadata())
    jsonl.emit({
        "event": "progress",
        "requests": 1,
        "completed_requests": 1,
        "cache_hits": 0,
        "input_tokens": 100,
        "elapsed_seconds": 1.0,
    })
    jsonl.emit({"event": "summary", **asdict(Summary("live"))})
    assert [json.loads(line)["event"] for line in machine.getvalue().splitlines()] == ["start", "summary"]


def _evaluation_event(name: str = "work", status: str = "warning") -> dict:
    finding = {"rule": "cohesion", "severity": status, "message": "A configurable explanation."}
    return {
        "event": "evaluation",
        "target": {
            "scope": "unit",
            "kind": "function",
            "path": "src/demo.py",
            "qualified_name": name,
            "start_line": 10,
            "end_line": 20,
        },
        "answers": {"cohesion": {"type": "noul", "noul": 0.95}},
        "statuses": {"cohesion": status},
        "rule_metadata": {"cohesion": {"title": "", "ruleset": "project"}},
        "findings": [finding] if status in {"warning", "error"} else [],
        "tentative_findings": [],
        "uncertainty_reasons": {},
        "reviews": {},
        "scales": {},
        "cached": False,
    }


@pytest.mark.parametrize("verbose", [False, True])
def test_default_filters_before_headers_and_display_limits(verbose: bool, monkeypatch) -> None:
    monkeypatch.setenv("COLOR", "no")
    stream = io.StringIO()
    reporter = Reporter(stream, "text", _report_metadata(), max_display=1, verbose=verbose)
    reporter.emit(_evaluation_event("clean_target", "ok"))
    reporter.emit(_evaluation_event("warning_target", "warning"))
    reporter.emit(_evaluation_event("error_target", "error"))
    reporter.emit({
        "event": "diagnostic",
        "path": "other.py",
        "severity": "warning",
        "code": "coverage",
        "message": "Still visible",
    })
    reporter.emit({"event": "summary", **asdict(Summary("live"))})
    text = stream.getvalue()
    assert ("clean_target" in text) is verbose
    assert ("warning_target" in text) is not verbose
    assert "error_target" not in text
    assert "Still visible" in text
    assert f"omitted={2 if verbose else 1} targets" in text
    assert text.count("src/demo.py") == 1


@pytest.mark.parametrize("format_name", ["json", "jsonl"])
def test_machine_output_ignores_verbose_limits_and_color(format_name: str, monkeypatch) -> None:
    monkeypatch.setenv("COLOR", "yes")
    stream = io.StringIO()
    reporter = Reporter(stream, format_name, _report_metadata(), max_display=1)
    events = [
        _evaluation_event("clean", "ok"),
        _evaluation_event("unknown", "unknown"),
        _evaluation_event("bad", "error"),
    ]
    for event in events:
        reporter.emit(event)
    reporter.emit({"event": "summary", **asdict(Summary("live"))})
    text = stream.getvalue()
    assert "\x1b" not in text
    if format_name == "json":
        decoded = json.loads(text)
        assert decoded["schema_version"] == 9 and decoded["events"] == events
    else:
        decoded = [json.loads(line) for line in text.splitlines()]
        assert decoded[0]["schema_version"] == 9 and decoded[1:-1] == events


@pytest.mark.parametrize("width", [32, 80])
@pytest.mark.parametrize("color", [False, True])
def test_wrapping_keeps_hanging_indent_and_display_cell_width(width: int, color: bool, monkeypatch) -> None:
    from wcwidth import strip_sequences, wcswidth

    from jevscan.cli.terminal import DIM, Terminal

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLOR", "yes" if color else "no")
    stream = io.StringIO()
    terminal = Terminal(stream, width)
    message = "The café e\u0301 中文 execution path " + "verylongword" * 12 + " needs review.\nA second paragraph."
    terminal.write(message, 10, DIM)
    lines = [strip_sequences(line) for line in stream.getvalue().splitlines()]
    assert len(lines) > 2
    assert all(line.startswith(" " * 10) and wcswidth(line) <= width for line in lines)
    # Wrapping does not delete non-whitespace characters or interpret literal markup.
    assert "".join("".join(lines).split()) == "".join(message.split())


@pytest.mark.parametrize("flag", [[], ["-v"], ["--verbose"]])
def test_cli_verbose_flag_controls_only_text_details(flag: list[str], tmp_path: Path, monkeypatch, capsys) -> None:
    import importlib

    scan_command = importlib.import_module("jevscan.cli.scan_command")

    async def scan(_paths, _loaded, sink, **_options):
        sink.emit(_evaluation_event("clean_target", "ok"))
        sink.emit(_evaluation_event("bad_target", "error"))
        summary = Summary("live", findings={"info": 0, "warning": 0, "error": 1})
        sink.emit({"event": "summary", **asdict(summary)})
        return summary

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan_command, "run_scan", scan)
    assert main([".", *flag]) == 1
    text = capsys.readouterr().out
    assert ("clean_target" in text) == bool(flag)
    assert "x cohesion" in text and "bad_target" in text


def test_untrusted_terminal_text_cannot_inject_ansi(monkeypatch) -> None:
    from jevscan.cli.terminal import Terminal

    monkeypatch.setenv("COLOR", "no")
    stream = io.StringIO()
    Terminal(stream).write("[red] literal λ \x1b[2J \r \u202e text")
    output = stream.getvalue()
    assert "[red] literal λ" in output
    assert "\x1b" not in output and "\r" not in output and "\u202e" not in output
    assert "\\u001b[2J" in output


def test_no_enrichment_is_a_resolved_config_override(tmp_path, monkeypatch, capsys):
    import yaml

    monkeypatch.chdir(tmp_path)
    assert main(["--show-config", "--no-enrichment"]) == 0
    document = yaml.safe_load(capsys.readouterr().out)
    assert document["enrichment"]["enabled"] is False
    assert next(rule for rule in document["rules"] if rule["name"] == "JEV06")["require_members"] is True


@pytest.mark.parametrize("format_name", ["json", "jsonl", "text"])
def test_review_audit_and_unknown_reasons_survive_reporting(format_name):
    stream = io.StringIO()
    reporter = Reporter(stream, format_name, _report_metadata(), verbose=True, width=90)
    event = {
        "event": "evaluation",
        "target": {"path": "x.py", "scope": "file", "end_line": 3},
        "cached": False,
        "scales": {},
        "findings": [],
        "tentative_findings": [],
        "answers": {"test": {"type": "noul", "noul": 0.5}},
        "statuses": {"test": "unknown"},
        "rule_metadata": {"test": {"title": "", "ruleset": "project"}},
        "uncertainty_reasons": {"test": "probability_ambiguous"},
        "reviews": {
            "test": {"outcome": "no_relevant_evidence", "selected": [], "candidates": [{"id": "c1", "relevance": 0.1}]}
        },
    }
    reporter.emit(event)
    reporter.emit({"event": "summary", **asdict(Summary("live"))})
    text = stream.getvalue()
    if format_name == "text":
        assert "? test" in text and "probability ambiguous" in text and "no relevant evidence" in text
    else:
        payload = json.loads(text) if format_name == "json" else json.loads(text.splitlines()[0])
        assert payload["schema_version"] == 9
        actual = payload["events"][0] if format_name == "json" else json.loads(text.splitlines()[1])
        assert actual == event


@pytest.mark.parametrize("color", [False, True])
def test_tentative_warning_and_error_are_visible_without_verbose(color, monkeypatch):
    from wcwidth import strip_sequences, wcswidth

    from jevscan.cli.terminal import CYAN

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLOR", "yes" if color else "no")
    stream = io.StringIO()
    reporter = Reporter(stream, "text", _report_metadata(), max_display=2, width=76)
    reporter.emit(_evaluation_event("ordinary_ambiguity", "unknown"))
    for severity in ("warning", "error"):
        event = _evaluation_event("tentative_" + severity, "unknown")
        event["answers"]["cohesion"] = {
            "type": "score",
            "score": 1.66 if severity == "warning" else 2.5,
            "confidence": 0.58,
        }
        event["scales"] = {"cohesion": 3}
        event["uncertainty_reasons"] = {"cohesion": "low_confidence"}
        event["tentative_findings"] = [
            {"rule": "cohesion", "severity": severity, "message": "Review this independently of confidence."}
        ]
        reporter.emit(event)
    reporter.emit(_evaluation_event("third_visible_target", "warning"))
    summary = Summary("live", uncertain=3, tentative_findings={"warning": 1, "error": 1})
    reporter.emit({"event": "summary", **asdict(summary)})
    raw = stream.getvalue()
    text = strip_sequences(raw)
    assert "ordinary_ambiguity" not in text and "third_visible_target" not in text
    assert "tentative_warning" in text and "tentative_error" in text
    assert text.count("? cohesion") == 2 and "! cohesion" not in text and "x cohesion" not in text
    assert "[uncertain warning]" in text and "[uncertain error]" in text
    assert text.count("Uncertain: low confidence") == 2
    assert "Uncertain findings: 1 warnings, 1 errors" in text and "omitted=1 targets" in text
    assert all(wcswidth(line) <= 76 for line in text.splitlines())
    if color:
        assert CYAN + "?" in raw
    assert summary.exit_code("warning") == summary.exit_code("error") == summary.exit_code("never") == 0


def test_rule_codes_titles_and_ruleset_selection_reach_cli_and_planner(tmp_path, monkeypatch, capsys):
    from jevscan.core.config import load_config
    from jevscan.core.context import ContextBuilder
    from jevscan.core.models import FileJob
    from jevscan.core.parser import parse_source
    from jevscan.core.planning import Planner

    monkeypatch.chdir(tmp_path)
    (tmp_path / "jevscan.yaml").write_text("lint:\n  ignore: [JEV09]\n")
    assert main(["--list-rules", "--select", "JEV", "--ignore", "JEV02"]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert "title=mixed-responsibilities" in rows[0] and "ruleset=JEV" in rows[0]
    assert "enabled=False" in rows[1] and "enabled=False" in rows[8]
    assert main(["--list-rules", "--ignore", "JVE09"]) == 2
    assert "unknown rule/ruleset" in capsys.readouterr().err
    config = load_config([tmp_path], cwd=tmp_path).config
    parsed = parse_source(b"def f(): return 1\n", FileJob("x.py", "x.py", "python", "python"))
    planner = Planner(ContextBuilder(parsed), config)
    assert "JEV09" not in {check.rule_id for check in planner.checks}
    assert "JEV01" in {check.rule_id for check in planner.checks}


def test_rule_metadata_is_presented_but_not_used_as_a_model_instruction(tmp_path):
    from jevscan.core.config import load_config
    from jevscan.core.context import ContextBuilder
    from jevscan.core.evaluation import FileResults
    from jevscan.core.models import FileJob
    from jevscan.core.parser import parse_source
    from jevscan.core.planning import Planner
    from jevscan.core.protocol import NoulAnswer

    config = load_config([], cwd=tmp_path).config
    parsed = parse_source(b"def f(): return 1\n", FileJob("x.py", "x.py", "python", "python"))
    planner = Planner(ContextBuilder(parsed), config)
    check = next(check for check in planner.checks if check.rule_id == "JEV01")
    evidence = planner.context.requested(check)
    results = FileResults(planner)
    results.accept(planner.request(evidence, (check,)), {check.id: NoulAnswer(type="noul", noul=0.95)}, "test", False)
    event = results.records[check.target.id].event()
    assert event["rule_metadata"]["JEV01"] == {"title": "mixed-responsibilities", "ruleset": "JEV", "blocks_exit": True}
    stream = io.StringIO()
    Reporter(stream, "text", _report_metadata(), width=120).emit(event)
    assert "JEV01 mixed-responsibilities" in stream.getvalue()
    assert "title" not in check.question()["instructions"]


@pytest.mark.parametrize("verbose", [False, True])
def test_hundreds_of_coverage_details_are_aggregated_without_hiding_real_errors(verbose):
    stream = io.StringIO()
    reporter = Reporter(stream, "text", _report_metadata(), verbose=verbose, width=100)
    reporter.emit({
        "event": "coverage",
        "path": "large.ts",
        "context_reduced_targets": 580,
        "skipped_checks": 4,
        "skipped_file_checks": 2,
        "aborted": False,
    })
    for i in range(580):
        reporter.emit({
            "event": "diagnostic",
            "path": "large.ts",
            "line": i + 1,
            "severity": "warning",
            "code": "context-reduced",
            "message": "reduced surrounding context",
        })
    reporter.emit({
        "event": "diagnostic",
        "path": "broken.ts",
        "line": 46,
        "severity": "error",
        "code": "syntax-error",
        "message": "invalid syntax",
    })
    assert "context compacted for 580 targets" in stream.getvalue()
    assert "context-reduced" not in stream.getvalue()
    assert "broken.ts:46" in stream.getvalue()
    assert len(stream.getvalue().splitlines()) <= 8

    aborted = io.StringIO()
    aborted_reporter = Reporter(aborted, "text", _report_metadata(), width=100)
    aborted_reporter.emit({
        "event": "coverage",
        "path": "commit.ts",
        "context_reduced_targets": 0,
        "skipped_checks": 1190,
        "skipped_file_checks": 2,
        "request_rejected_checks": 0,
        "aborted": True,
    })
    assert "scan aborted — 1190 checks not completed" in aborted.getvalue()
    assert "context compacted" not in aborted.getvalue()
    machine = io.StringIO()
    reporter = Reporter(machine, "jsonl", _report_metadata())
    reporter.emit({
        "event": "diagnostic",
        "path": "large.ts",
        "line": 1,
        "severity": "warning",
        "code": "context-reduced",
        "message": "reduced surrounding context",
    })
    assert "context-reduced" in machine.getvalue()


def test_reduced_context_is_a_yellow_coverage_warning_not_an_incomplete_summary():
    stream = io.StringIO()
    reporter = Reporter(stream, "text", _report_metadata(), width=100)
    reporter.terminal.color = True
    reporter.emit({
        "event": "coverage",
        "path": "large.ts",
        "context_reduced_targets": 2,
        "skipped_checks": 0,
        "skipped_file_checks": 0,
        "aborted": False,
    })
    summary = Summary("live")
    summary.context_reduced = 2
    assert summary.exit_code("warning") == 0
    reporter.emit({"event": "summary", **asdict(summary)})
    output = stream.getvalue()
    assert "coverage warning — context compacted for 2 targets" in output
    assert "\x1b[33mcomplete: 0 warnings, 0 errors" in output
    assert "incomplete:" not in output
    summary.findings["warning"] = 1
    assert summary.exit_code("warning") == 1
    assert summary.exit_code("never") == 0


@pytest.mark.usefixtures("grammar_runtime")
def test_plan_mode_estimates_without_api_key(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "demo.py"
    source.write_text("class S:\n    def work(self):\n        return 1\n")
    assert main([str(source), "--plan", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    summary = report["summary"]
    assert summary["mode"] == "plan"
    assert summary["planned_checks"] > 0
    assert summary["planned_requests"] > 0
    assert summary["requests"] == 0
    assert summary["estimated_input_tokens"] == summary["planned_input_tokens"]


def test_changed_and_staged_modes_filter_explicit_scan_targets(tmp_path: Path, monkeypatch, capsys) -> None:
    import importlib
    import shutil
    import subprocess

    scan_command = importlib.import_module("jevscan.cli.scan_command")
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True)  # noqa: S603
    subprocess.run([git, "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)  # noqa: S603
    subprocess.run([git, "-C", str(tmp_path), "config", "user.name", "Test"], check=True)  # noqa: S603
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("def f(): return 1\n")
    subprocess.run([git, "-C", str(tmp_path), "add", "a.py", "b.py"], check=True)  # noqa: S603
    subprocess.run([git, "-C", str(tmp_path), "commit", "-qm", "base"], check=True)  # noqa: S603
    (tmp_path / "a.py").write_text("def f(): return 2\n")
    subprocess.run([git, "-C", str(tmp_path), "add", "a.py"], check=True)  # noqa: S603
    (tmp_path / "b.py").write_text("def f(): return 3\n")
    (tmp_path / "new.py").write_text("def f(): return 4\n")

    seen: list[list[str]] = []

    async def scan(paths, _loaded, sink, **_options):
        seen.append(sorted(path.name for path in paths))
        summary = Summary("plan")
        sink.emit({"event": "summary", **asdict(summary)})
        return summary

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan_command, "run_scan", scan)
    assert main([".", "--changed", "--plan", "--format", "json"]) == 0
    capsys.readouterr()
    assert seen[-1] == ["a.py", "b.py", "new.py"]
    assert main([".", "--staged", "--plan", "--format", "json"]) == 0
    capsys.readouterr()
    assert seen[-1] == ["a.py"]
