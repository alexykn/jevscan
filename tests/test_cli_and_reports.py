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
    from jevscan.core.parser import ParserUnavailable

    def unavailable() -> None:
        raise ParserUnavailable("deliberate missing-dependency test")

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
    reporter = Reporter(stream, "text", _report_metadata())
    unit = {
        "path": "src/example.py",
        "start_line": 25,
        "kind": "method",
        "qualified_name": "ContextBuilder.__init__",
    }
    reporter.emit({
        "event": "evaluation",
        "unit": unit,
        "cached": True,
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
                "rule": "unclear-control-flow",
                "severity": "warning",
                "message": "Control flow is difficult to follow.",
            }
        ],
    })

    text = stream.getvalue()
    assert text.count("src/example.py") == 1
    assert text.count("ContextBuilder.__init__") == 1
    assert "M 25 ContextBuilder.__init__  cached" in text
    assert "mixed-responsibilities" in text and "noul=0.210" in text
    assert "unclear-control-flow" in text and "score=2.000  conf=0.840" in text
    assert "redundant-validation" in text and "justified_or_absent  p=0.830  conf=0.740" in text
    assert "Control flow is difficult to follow." in text
    assert "\x1b[" not in text


def test_text_report_colors_tty_output(monkeypatch) -> None:
    class TtyStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLOR", "auto")
    stream = TtyStream()
    reporter = Reporter(stream, "text", _report_metadata())
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
