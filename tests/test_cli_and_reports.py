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
