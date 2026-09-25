"""Human-readable event rendering, separate from machine report framing."""

from typing import Any, TextIO

from jevscan.cli.terminal import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    KIND_STYLE,
    LEVEL_MARKER,
    LEVEL_STYLE,
    RED,
    YELLOW,
    Terminal,
    safe_text,
)


def _answer_text(answer: dict[str, Any], scale: int | None) -> str:
    if answer["type"] == "noul":
        return f"noul={answer['noul']:.3f}"
    if answer["type"] == "choice":
        probability = answer["probabilities"][answer["choice"]]
        return f"{answer['choice']}  p={probability:.3f}  conf={answer['confidence']:.3f}"
    return f"score={answer['score']:.3f}/{scale}  conf={answer['confidence']:.3f}"


def _coverage_message(event: dict[str, Any]) -> str:
    if event.get("aborted"):
        return (
            f"scan aborted — {event['skipped_checks']} checks not completed "
            f"({event['skipped_file_checks']} file checks)"
        )
    if event["context_reduced_targets"]:
        label = "coverage warning" if not event["skipped_checks"] else "coverage"
        return (
            f"{label} — context compacted for {event['context_reduced_targets']} targets; "
            f"{event['skipped_checks']} checks omitted ({event['skipped_file_checks']} file checks)"
        )
    return f"coverage — {event['skipped_checks']} checks omitted ({event['skipped_file_checks']} file checks)"


def _summary_style(summary: dict[str, Any]) -> str:
    findings = summary["findings"]
    if summary["incomplete"] or findings["error"]:
        return RED
    if findings["warning"] or summary["context_reduced"]:
        return YELLOW
    return CYAN if summary["uncertain"] else GREEN


class TextReport:
    def __init__(
        self,
        stream: TextIO,
        terminal: Terminal,
        metadata: dict[str, Any],
        *,
        max_display: int,
        verbose: bool,
    ) -> None:
        self.stream = stream
        self.terminal = terminal
        self.limit = max_display
        self.shown = 0
        self.hidden = 0
        self.verbose = verbose
        self.current_path: str | None = None
        self.progress_width = 0
        self.terminal.write(f"jevscan {metadata['mode']}  model={metadata['model']}", style=BOLD + CYAN)
        self.terminal.write(str(metadata["root"]), style=DIM)
        if self.verbose:
            self.terminal.write(
                "· ok  ! warning  x error  ? uncertain  - not applicable; conf is not severity",
                style=DIM,
            )
        self.stream.write("\n")
        self.stream.flush()

    def progress(self, event: dict[str, Any]) -> None:
        if not self.stream.isatty():
            return
        text = safe_text(
            f"working — requests={event['completed_requests']}/{event['requests']} "
            f"cache-hits={event['cache_hits']} input={event['input_tokens']} "
            f"reserved≈{event['estimated_cost']:.4f} elapsed={event['elapsed_seconds']:.1f}s"
        )
        width = max(self.progress_width, len(text))
        self.stream.write("\r" + text.ljust(width))
        self.stream.flush()
        self.progress_width = width

    def _clear_progress(self) -> None:
        if self.progress_width:
            self.stream.write("\r" + (" " * self.progress_width) + "\r")
            self.progress_width = 0

    def _admit(self) -> bool:
        if self.limit and self.shown >= self.limit:
            self.hidden += 1
            return False
        self.shown += 1
        return True

    def _file_header(self, path: str) -> None:
        if path == self.current_path:
            return
        if self.current_path is not None:
            self.stream.write("\n")
        self.current_path = path
        self.terminal.write(path, style=BOLD)

    def _target_header(self, target: dict[str, Any], cached: bool = False) -> None:
        self._file_header(target["path"])
        if target.get("scope") == "file":
            marker, style = "FILE", CYAN
            location, name = f"1-{target['end_line']}", "whole file"
        else:
            marker, style = KIND_STYLE[target["kind"]]
            location = str(target["start_line"])
            name = target.get("display_name") or target["qualified_name"]
        indent = 6 if target.get("kind") == "closure" else 4
        self.terminal.header(marker, location, name, cached, style, indent=indent)

    def _visible_rows(self, event: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
        tentative = {finding["rule"]: finding for finding in event["tentative_findings"]}
        rows = [
            (name, answer)
            for name, answer in event["answers"].items()
            if self.verbose or event["statuses"][name] in {"warning", "error"} or name in tentative
        ]
        return rows, tentative

    def _review_detail(self, event: dict[str, Any], name: str, status: str) -> None:
        reason = event["uncertainty_reasons"].get(name)
        if status == "unknown" and reason:
            self.terminal.write("Uncertain: " + reason.replace("_", " "), 10, DIM)
        review = event["reviews"].get(name)
        if review:
            self.terminal.write("Evidence review: " + review["outcome"].replace("_", " "), 10, DIM)
            for item in review["selected"]:
                target = item["target"]
                self.terminal.write(
                    f"+ {target['path']}:{target['start_line']}-{target['end_line']} ({item['relation']})",
                    12,
                    DIM,
                )

    def _evaluation_row(
        self,
        event: dict[str, Any],
        name: str,
        answer: dict[str, Any],
        label: str,
        width: int,
        tentative: dict[str, Any],
        findings: dict[str, Any],
    ) -> None:
        status = event["statuses"][name]
        text = _answer_text(answer, event["scales"].get(name))
        if name in tentative:
            text += f"  [uncertain {tentative[name]['severity']}]"
        self.terminal.row(LEVEL_MARKER[status], f"{label:<{width}}", text, LEVEL_STYLE[status])
        if name in findings:
            self.terminal.write(findings[name]["message"], 10, DIM)
        self._review_detail(event, name, status)

    def _evaluation(self, event: dict[str, Any]) -> None:
        rows, tentative = self._visible_rows(event)
        if not rows or not self._admit():
            return
        self._target_header(event["target"], event["cached"])
        findings = {
            finding["rule"]: finding
            for finding in [*event["findings"], *event["tentative_findings"]]
        }
        labels = {
            name: " ".join(part for part in (name, event["rule_metadata"][name]["title"]) if part)
            for name, _ in rows
        }
        width = max(map(len, labels.values()))
        for name, answer in rows:
            self._evaluation_row(event, name, answer, labels[name], width, tentative, findings)

    def _unit(self, event: dict[str, Any]) -> None:
        if self._admit():
            self._target_header(event["unit"])

    def _plan(self, event: dict[str, Any]) -> None:
        self._file_header(event["path"])
        self.terminal.write(
            f"plan — {event['checks']} checks, {event['requests']} requests, "
            f"~{event['estimated_input_tokens']} input tokens; {event['omitted_checks']} omitted",
            2,
            DIM,
        )

    def _coverage(self, event: dict[str, Any]) -> None:
        self._file_header(event["path"])
        message = _coverage_message(event)
        rejected = event.get("request_rejected_checks", 0)
        if rejected:
            message += f"; {rejected} provider-rejected"
        self.terminal.write(message, 2, YELLOW)

    def _diagnostic(self, event: dict[str, Any]) -> None:
        if event["code"] in {"context-reduced", "evaluation-size-limit"}:
            return
        location = event["path"]
        if location and event.get("line"):
            location += f":{event['line']}"
        suffix = f" ({location})" if location else ""
        level = event["severity"]
        self.terminal.write(
            f"{LEVEL_MARKER[level]} {level}: {event['code']}: {event['message']}{suffix}",
            2,
            LEVEL_STYLE[level],
        )

    def emit(self, event: dict[str, Any]) -> None:
        self._clear_progress()
        handlers = {
            "unit": self._unit,
            "plan": self._plan,
            "evaluation": self._evaluation,
            "coverage": self._coverage,
            "diagnostic": self._diagnostic,
        }
        handler = handlers.get(event["event"])
        if handler is not None:
            handler(event)
        self.stream.flush()

    def _headline(self, summary: dict[str, Any]) -> None:
        status = "incomplete" if summary["incomplete"] else "complete"
        findings = summary["findings"]
        self.stream.write("\n")
        self.terminal.write(
            f"{status}: {findings['warning']} warnings, {findings['error']} errors; "
            f"{summary['uncertain']} uncertain checks; {summary['not_applicable']} not applicable",
            style=BOLD + _summary_style(summary),
        )

    def _finding_notes(self, summary: dict[str, Any]) -> None:
        advisory = summary.get("advisory_findings", {})
        if any(advisory.values()):
            self.terminal.write(
                f"Advisory findings: {advisory.get('warning', 0)} warnings, {advisory.get('error', 0)} errors "
                "(shown in findings; do not trigger --fail-on)",
                style=CYAN,
            )
        tentative = summary["tentative_findings"]
        if any(tentative.values()):
            self.terminal.write(
                f"Uncertain findings: {tentative['warning']} warnings, {tentative['error']} errors "
                "(included in uncertain checks; not confirmed, do not trigger --fail-on)",
                style=CYAN,
            )

    def _plan_summary(self, summary: dict[str, Any]) -> None:
        self.terminal.write(
            f"files={summary['files_parsed']}/{summary['files_discovered']}  units={summary['units_found']}  "
            f"planned-checks={summary['planned_checks']}  planned-requests={summary['planned_requests']}  "
            f"estimated-input={summary['planned_input_tokens']}  estimated-cost={summary['estimated_cost']:.4f}",
            style=DIM,
        )

    def _live_summary(self, summary: dict[str, Any]) -> None:
        self.terminal.write(
            f"files={summary['files_parsed']}/{summary['files_discovered']}  units={summary['units_found']}  "
            f"evaluated={summary['units_evaluated']} units/{summary['file_targets_evaluated']} files  "
            f"checks={summary['checks_evaluated']}  skipped={summary['checks_skipped']}  "
            f"requests={summary['requests']}  cache-hits={summary['cache_hits']}  "
            f"input={summary['input_tokens']}  reported-cost={summary['reported_cost']:.4f}  "
            f"reserved-cost={summary['estimated_cost']:.4f}  elapsed={summary['elapsed_seconds']:.2f}s",
            style=DIM,
        )
        self.terminal.write(
            f"input-by-phase: evaluation={summary['evaluation_input_tokens']} "
            f"compaction={summary['compaction_input_tokens']} enrichment={summary['enrichment_input_tokens']}  "
            f"retries={summary['retry_attempts']}",
            style=DIM,
        )

    def _activity_notes(self, summary: dict[str, Any]) -> None:
        if summary["size_rejections"] or summary.get("request_rejections", 0) or summary["compaction_calls"]:
            self.terminal.write(
                f"context: size-rejections={summary['size_rejections']} "
                f"request-rejections={summary.get('request_rejections', 0)} "
                f"compact-selection-calls={summary['compaction_calls']} cached={summary['compaction_cache_hits']}",
                style=DIM,
            )
        if summary["enrichment_reviewed"]:
            self.terminal.write(
                f"enrichment: reviewed={summary['enrichment_reviewed']} rerun={summary['enrichment_reruns']} "
                f"resolved={summary['enrichment_resolved']} calls={summary['enrichment_calls']} "
                f"cached={summary['enrichment_cache_hits']}",
                style=DIM,
            )
        if summary.get("applicability_skips", 0):
            self.terminal.write(
                f"applicability: skipped={summary['applicability_skips']} declared prerequisites absent",
                style=DIM,
            )

    def _display_notes(self, summary: dict[str, Any]) -> None:
        if self.hidden:
            self.terminal.write(
                f"omitted={self.hidden} targets (--max-display); machine reports remain complete",
                style=DIM,
            )
        if not self.verbose and summary["mode"] == "live":
            self.terminal.write("Use -v/--verbose to show all evaluated answers.", style=DIM)

    def summary(self, summary: dict[str, Any]) -> None:
        self._clear_progress()
        self._headline(summary)
        self._finding_notes(summary)
        (self._plan_summary if summary["mode"] == "plan" else self._live_summary)(summary)
        self._activity_notes(summary)
        self._display_notes(summary)
        self.stream.flush()
