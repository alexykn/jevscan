"""Plain-text terminal presentation and streaming machine-readable reports."""

import json
from typing import Any, TextIO


class Reporter:
    def __init__(self, stream: TextIO, format_name: str, metadata: dict[str, Any], *, max_display: int = 0) -> None:
        self.stream = stream
        self.format = format_name
        self.first = True
        self.closed = False
        self.limit = max_display
        self.shown = 0
        self.hidden = 0
        if self.format == "json":
            self.stream.write(
                '{"schema_version":1,"metadata":' + json.dumps(metadata, ensure_ascii=False) + ',"events":['
            )
        elif self.format == "jsonl":
            self._line({"event": "start", "schema_version": 1, **metadata})
        else:
            self.stream.write(f"jevscan {metadata['mode']} root={metadata['root']} model={metadata['model']}\n")

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
            return f"choice={answer['choice']} p={probability:.3f} confidence={answer['confidence']:.3f}"
        return f"score={answer['score']:.3f} confidence={answer['confidence']:.3f}"

    def _text_event(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        if kind == "unit" and self._admit_detail():
            unit = event["unit"]
            self.stream.write(f"unit {unit['path']}:{unit['start_line']}-{unit['end_line']} {unit['qualified_name']}\n")
        elif kind == "evaluation" and self._admit_detail():
            unit = event["unit"]
            findings = {finding["rule"]: finding for finding in event["findings"]}
            cached = " cached" if event["cached"] else ""
            for rule, answer in event["answers"].items():
                finding = findings.get(rule)
                marker = "!" if finding else " "
                detail = self._answer_text(answer)
                suffix = f" — {finding['message']}" if finding else ""
                self.stream.write(
                    f"{marker} {unit['path']}:{unit['start_line']} {unit['qualified_name']} "
                    f"{rule} {detail}{cached}{suffix}\n"
                )
        elif kind == "diagnostic":
            location = event["path"]
            if location and event.get("line"):
                location += f":{event['line']}"
            suffix = f" ({location})" if location else ""
            self.stream.write(f"{event['severity']}: {event['code']}: {event['message']}{suffix}\n")

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
            findings = " ".join(f"{level}={count}" for level, count in summary["findings"].items())
            self.stream.write(
                f"{status}: files={summary['files_parsed']}/{summary['files_discovered']} "
                f"units={summary['units_found']} evaluated={summary['units_evaluated']} "
                f"cached={summary['units_cached']} skipped={summary['units_skipped']} "
                f"failed={summary['units_failed']} findings={findings} "
                f"requests={summary['requests']} elapsed={summary['elapsed_seconds']:.2f}s\n"
            )
            if self.hidden:
                self.stream.write(f"omitted={self.hidden} details (use --format jsonl for the full report)\n")
        self.stream.flush()
        self.closed = True
