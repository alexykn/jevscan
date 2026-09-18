"""Radon-style terminal presentation and streaming machine-readable reports."""

import json
import os
from typing import Any, TextIO

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
WHITE = "\x1b[37m"

KIND_STYLE = {
    "function": ("F", MAGENTA),
    "method": ("M", WHITE),
    "closure": ("L", MAGENTA),
    "class": ("C", CYAN),
    "struct": ("S", CYAN),
    "enum": ("E", CYAN),
    "trait": ("T", CYAN),
    "impl": ("I", CYAN),
    "interface": ("I", CYAN),
    "type": ("T", CYAN),
    "module": ("M", BLUE),
    "package": ("P", BLUE),
}
SEVERITY_STYLE = {"info": CYAN, "warning": YELLOW, "error": RED}


def _color_enabled(stream: TextIO) -> bool:
    if os.getenv("NO_COLOR") is not None:
        return False
    setting = os.getenv("COLOR", "auto").lower()
    if setting == "yes":
        return True
    if setting == "no":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


class Reporter:
    def __init__(self, stream: TextIO, format_name: str, metadata: dict[str, Any], *, max_display: int = 0) -> None:
        self.stream = stream
        self.format = format_name
        self.first = True
        self.closed = False
        self.limit = max_display
        self.shown = 0
        self.hidden = 0
        self.current_path: str | None = None
        self.color = format_name == "text" and _color_enabled(stream)
        if self.format == "json":
            self.stream.write(
                '{"schema_version":1,"metadata":' + json.dumps(metadata, ensure_ascii=False) + ',"events":['
            )
        elif self.format == "jsonl":
            self._line({"event": "start", "schema_version": 1, **metadata})
        else:
            title = self._paint("jevscan", BOLD + CYAN)
            details = self._paint(
                f"{metadata['mode']}  root={metadata['root']}  model={metadata['model']}",
                DIM,
            )
            self.stream.write(f"{title} {details}\n\n")

    def _paint(self, text: str, style: str) -> str:
        return f"{style}{text}{RESET}" if self.color else text

    def _line(self, event: dict[str, Any]) -> None:
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    def emit(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        if event["event"] == "summary":
            self._finish(event)
            return
        if self.format == "jsonl":
            self._line(event)
        elif self.format == "json":
            if not self.first:
                self.stream.write(",")
            self.first = False
            self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        else:
            self._text_event(event)

    def _admit_detail(self) -> bool:
        if self.limit and self.shown >= self.limit:
            self.hidden += 1
            return False
        self.shown += 1
        return True

    @staticmethod
    def _answer_text(answer: dict[str, Any]) -> str:
        kind = answer["type"]
        if kind == "noul":
            return f"noul={answer['noul']:.3f}"
        if kind == "choice":
            probability = answer["probabilities"][answer["choice"]]
            return f"{answer['choice']}  p={probability:.3f}  conf={answer['confidence']:.3f}"
        return f"score={answer['score']:.3f}  conf={answer['confidence']:.3f}"

    def _file_header(self, path: str) -> None:
        if path == self.current_path:
            return
        if self.current_path is not None:
            self.stream.write("\n")
        self.current_path = path
        self.stream.write(self._paint(path, BOLD) + "\n")

    def _unit_header(self, unit: dict[str, Any], *, cached: bool = False) -> None:
        self._file_header(unit["path"])
        letter, color = KIND_STYLE.get(unit["kind"], ("?", WHITE))
        marker = self._paint(letter, BOLD + color)
        location = self._paint(f"{unit['start_line']}:{unit.get('start_byte', 0)}", DIM)
        name = self._paint(unit["qualified_name"], BOLD)
        cached_text = self._paint("  cached", DIM) if cached else ""
        self.stream.write(f"    {marker} {location} {name}{cached_text}\n")

    def _evaluation(self, event: dict[str, Any]) -> None:
        unit = event["unit"]
        self._unit_header(unit, cached=event["cached"])
        findings = {finding["rule"]: finding for finding in event["findings"]}
        width = max((len(rule) for rule in event["answers"]), default=0)
        for rule, answer in event["answers"].items():
            finding = findings.get(rule)
            if finding:
                color = SEVERITY_STYLE[finding["severity"]]
                marker = self._paint("!", BOLD + color)
                value = self._paint(self._answer_text(answer), color)
            else:
                marker = self._paint("·", GREEN)
                value = self._paint(self._answer_text(answer), GREEN)
            self.stream.write(f"        {marker} {rule:<{width}}  {value}\n")
            if finding:
                self.stream.write(f"          {self._paint(finding['message'], DIM)}\n")

    def _text_event(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        if kind == "unit" and self._admit_detail():
            self._unit_header(event["unit"])
        elif kind == "evaluation" and self._admit_detail():
            self._evaluation(event)
        elif kind == "diagnostic":
            location = event["path"]
            if location and event.get("line"):
                location += f":{event['line']}"
            suffix = f" ({location})" if location else ""
            color = SEVERITY_STYLE[event["severity"]]
            level = self._paint(event["severity"], BOLD + color)
            self.stream.write(f"{level}: {event['code']}: {event['message']}{suffix}\n")

    def _finish(self, event: dict[str, Any]) -> None:
        summary = {key: value for key, value in event.items() if key != "event"}
        if self.format == "json":
            self.stream.write('],"summary":' + json.dumps(summary, ensure_ascii=False) + "}\n")
        elif self.format == "jsonl":
            self._line(event)
        else:
            status = (
                "incomplete"
                if summary["incomplete"]
                else ("complete" if summary["mode"] == "live" else "inventory-complete")
            )
            status_color = RED if summary["incomplete"] else GREEN
            findings = " ".join(f"{level}={count}" for level, count in summary["findings"].items())
            self.stream.write("\n")
            self.stream.write(
                f"{self._paint(status, BOLD + status_color)}: "
                f"files={summary['files_parsed']}/{summary['files_discovered']} "
                f"units={summary['units_found']} evaluated={summary['units_evaluated']} "
                f"cached={summary['units_cached']} skipped={summary['units_skipped']} "
                f"failed={summary['units_failed']} findings={findings} "
                f"requests={summary['requests']} elapsed={summary['elapsed_seconds']:.2f}s\n"
            )
            if self.hidden:
                self.stream.write(
                    f"omitted={self.hidden} units (use --format jsonl for the full report)\n"
                )
        self.stream.flush()
        self.closed = True
