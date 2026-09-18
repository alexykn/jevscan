"""Human reports and lossless streaming machine reports consume the same core events."""

import json
from typing import Any, TextIO

from jevscan.cli.terminal import BOLD, CYAN, DIM, GREEN, KIND_STYLE, LEVEL_MARKER, LEVEL_STYLE, RED, YELLOW, Terminal

REPORT_SCHEMA_VERSION = 5


class Reporter:
    def __init__(
        self,
        stream: TextIO,
        format_name: str,
        metadata: dict[str, Any],
        *,
        max_display: int = 0,
        verbose: bool = False,
        width: int | None = None,
    ) -> None:
        self.stream, self.format = stream, format_name
        self.first, self.closed = True, False
        self.limit, self.shown, self.hidden = max_display, 0, 0
        self.verbose = verbose
        self.current_path: str | None = None
        self.terminal = Terminal(stream, width)
        if self.format == "json":
            self.stream.write(
                '{"schema_version":'
                + str(REPORT_SCHEMA_VERSION)
                + ',"metadata":'
                + json.dumps(metadata, ensure_ascii=False)
                + ',"events":['
            )
        elif self.format == "jsonl":
            self._line({"event": "start", "schema_version": REPORT_SCHEMA_VERSION, **metadata})
        else:
            self.terminal.write(f"jevscan {metadata['mode']}  model={metadata['model']}", style=BOLD + CYAN)
            self.terminal.write(str(metadata["root"]), style=DIM)
            if self.verbose:
                self.terminal.write(
                    "· ok  ! warning  x error  ? uncertain  - not applicable; conf is not severity", style=DIM
                )
            self.stream.write("\n")
            self.stream.flush()

    def _line(self, event: dict[str, Any]) -> None:
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def emit(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        if event["event"] == "summary":
            self._finish(event)
        elif self.format == "jsonl":
            self._line(event)
        elif self.format == "json":
            if not self.first:
                self.stream.write(",")
            self.first = False
            self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        else:
            self._text_event(event)
            self.stream.flush()

    def _admit(self) -> bool:
        if self.limit and self.shown >= self.limit:
            self.hidden += 1
            return False
        self.shown += 1
        return True

    def _target_header(self, target: dict[str, Any], cached: bool = False) -> None:
        path = target["path"]
        if path != self.current_path:
            if self.current_path is not None:
                self.stream.write("\n")
            self.current_path = path
            self.terminal.write(path, style=BOLD)
        if target.get("scope") == "file":
            marker, style = "FILE", CYAN
            location, name = f"1-{target['end_line']}", "whole file"
        else:
            marker, style = KIND_STYLE[target["kind"]]
            location, name = str(target["start_line"]), target["qualified_name"]
        self.terminal.header(marker, location, name, cached, style)

    @staticmethod
    def _answer_text(answer: dict[str, Any], scale: int | None) -> str:
        if answer["type"] == "noul":
            return f"noul={answer['noul']:.3f}"
        if answer["type"] == "choice":
            probability = answer["probabilities"][answer["choice"]]
            return f"{answer['choice']}  p={probability:.3f}  conf={answer['confidence']:.3f}"
        return f"score={answer['score']:.3f}/{scale}  conf={answer['confidence']:.3f}"

    def _evaluation(self, event: dict[str, Any]) -> None:
        tentative = {finding["rule"]: finding for finding in event["tentative_findings"]}
        rows = [
            (name, answer)
            for name, answer in event["answers"].items()
            if self.verbose or event["statuses"][name] in {"warning", "error"} or name in tentative
        ]
        if not rows or not self._admit():
            return
        self._target_header(event["target"], event["cached"])
        findings = {finding["rule"]: finding for finding in [*event["findings"], *event["tentative_findings"]]}
        labels = {
            name: " ".join(part for part in (name, event["rule_metadata"][name]["title"]) if part) for name, _ in rows
        }
        width = max(len(label) for label in labels.values())
        for name, answer in rows:
            status = event["statuses"][name]
            text = self._answer_text(answer, event["scales"].get(name))
            if name in tentative:
                text += f"  [uncertain {tentative[name]['severity']}]"
            self.terminal.row(LEVEL_MARKER[status], f"{labels[name]:<{width}}", text, LEVEL_STYLE[status])
            if name in findings:
                self.terminal.write(findings[name]["message"], 10, DIM)
            self._review_detail(event, name, status)

    def _review_detail(self, event: dict[str, Any], name: str, status: str) -> None:
        reason = event["uncertainty_reasons"].get(name)
        if status == "unknown" and reason:
            self.terminal.write("Uncertain: " + reason.replace("_", " "), 10, DIM)
        review = event["reviews"].get(name)
        if not review:
            return
        self.terminal.write("Evidence review: " + review["outcome"].replace("_", " "), 10, DIM)
        for item in review["selected"]:
            target = item["target"]
            self.terminal.write(
                f"+ {target['path']}:{target['start_line']}-{target['end_line']} ({item['relation']})", 12, DIM
            )

    def _text_event(self, event: dict[str, Any]) -> None:
        if event["event"] == "unit" and self._admit():
            # Offline is an inventory command, not a semantic report filtered to findings.
            self._target_header(event["unit"])
        elif event["event"] == "evaluation":
            self._evaluation(event)
        elif event["event"] == "diagnostic":
            location = event["path"]
            if location and event.get("line"):
                location += f":{event['line']}"
            suffix = f" ({location})" if location else ""
            level = event["severity"]
            self.terminal.write(
                f"{LEVEL_MARKER[level]} {level}: {event['code']}: {event['message']}{suffix}", 2, LEVEL_STYLE[level]
            )

    def _finish(self, event: dict[str, Any]) -> None:
        summary = {key: value for key, value in event.items() if key != "event"}
        if self.format == "json":
            self.stream.write('],"summary":' + json.dumps(summary, ensure_ascii=False) + "}\n")
        elif self.format == "jsonl":
            self._line(event)
        else:
            self._summary(summary)
        self.stream.flush()
        self.closed = True

    def _summary(self, summary: dict[str, Any]) -> None:
        status = "incomplete" if summary["incomplete"] else "complete"
        findings = summary["findings"]
        style = RED if summary["incomplete"] or findings["error"] else YELLOW if findings["warning"] else GREEN
        if style == GREEN and summary["uncertain"]:
            style = CYAN
        self.stream.write("\n")
        self.terminal.write(
            f"{status}: {findings['warning']} warnings, {findings['error']} errors; "
            f"{summary['uncertain']} uncertain checks; {summary['not_applicable']} not applicable",
            style=BOLD + style,
        )
        tentative = summary["tentative_findings"]
        if any(tentative.values()):
            self.terminal.write(
                f"Uncertain findings: {tentative['warning']} warnings, {tentative['error']} errors "
                "(included in uncertain checks; not confirmed, do not trigger --fail-on)",
                style=CYAN,
            )
        self.terminal.write(
            f"files={summary['files_parsed']}/{summary['files_discovered']}  units={summary['units_found']}  "
            f"evaluated={summary['units_evaluated']} units/{summary['file_targets_evaluated']} files  "
            f"checks={summary['checks_evaluated']}  skipped={summary['checks_skipped']}  "
            f"requests={summary['requests']}  cache-hits={summary['cache_hits']}  "
            f"elapsed={summary['elapsed_seconds']:.2f}s",
            style=DIM,
        )
        if summary["enrichment_reviewed"]:
            self.terminal.write(
                f"enrichment: reviewed={summary['enrichment_reviewed']} rerun={summary['enrichment_reruns']} "
                f"resolved={summary['enrichment_resolved']} calls={summary['enrichment_calls']} "
                f"cached={summary['enrichment_cache_hits']}",
                style=DIM,
            )
        if self.hidden:
            self.terminal.write(
                f"omitted={self.hidden} targets (--max-display); machine reports remain complete", style=DIM
            )
        if not self.verbose and summary["mode"] == "live":
            self.terminal.write("Use -v/--verbose to show all evaluated answers.", style=DIM)
