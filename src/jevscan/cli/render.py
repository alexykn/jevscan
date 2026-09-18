"""Rich terminal presentation and streaming machine-readable reports."""

import json
from typing import Any, TextIO

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

LEVEL_STYLES = {"info": "cyan", "warning": "yellow", "error": "red"}


class Reporter:
    def __init__(self, stream: TextIO, format_name: str, metadata: dict[str, Any], *, no_color: bool = False,
                 quiet: bool = False, max_display: int = 100) -> None:
        self.stream = stream
        self.format = format_name
        self.first = True
        self.closed = False
        self.limit = max_display
        self.shown = 0
        self.hidden = 0
        self.files = 0
        self.evaluated = 0
        self.console = Console(file=stream, no_color=no_color, highlight=False)
        self.progress: Progress | None = None
        self.task: Any = None
        if self.format == "json":
            self.stream.write('{"schema_version":1,"metadata":' + json.dumps(metadata, ensure_ascii=False) + ',"events":[')
        elif self.format == "jsonl":
            self._line({"event": "start", "schema_version": 1, **metadata})
        else:
            heading = Text("jevscan", style="bold cyan")
            heading.append("  /  code quality scanner", style="dim")
            details = Text(f"{metadata['root']}\n", style="bold")
            details.append(f"{metadata['mode']} · {metadata['parser_processes']} parser processes · ")
            details.append(f"{metadata['concurrency']} API slots\n")
            details.append(f"Config: {metadata['config']}", style="dim")
            self.console.print(Panel(Group(heading, details), border_style="cyan", padding=(1, 2)))
            if metadata["mode"] == "offline":
                self.console.print("Offline inventory — no semantic judgments, no network requests.\n", style="dim")
            if self.console.is_terminal and not quiet:
                self.progress = Progress(SpinnerColumn(), TextColumn("{task.description}"), TimeElapsedColumn(),
                                         console=self.console, transient=True)
                self.task = self.progress.add_task("Discovering source files", total=None)
                self.progress.start()

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
            self._rich_event(event)

    def _admit_detail(self) -> bool:
        if self.limit and self.shown >= self.limit:
            self.hidden += 1
            return False
        self.shown += 1
        return True

    def _rich_event(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        if kind == "file":
            self.files += 1
        elif kind == "unit" and self._admit_detail():
            unit = event["unit"]
            text = Text(f"  {unit['kind']:<10} ", style="cyan")
            text.append(unit["qualified_name"], style="bold")
            text.append(f"  {unit['path']}:{unit['start_line']}–{unit['end_line']}", style="dim")
            self.console.print(text)
        elif kind == "evaluation":
            self.evaluated += 1
            for finding in event["findings"]:
                if self._admit_detail():
                    self._finding(finding, event["cached"])
        elif kind == "diagnostic":
            text = Text(f"{event['severity'].upper()}  {event['code']}  ", style=LEVEL_STYLES[event["severity"]])
            text.append(event["message"])
            if event["path"]:
                text.append(f"\n  {event['path']}" + (f":{event['line']}" if event.get("line") else ""), style="dim")
            self.console.print(text)
        if self.progress:
            self.progress.update(self.task, description=f"{self.files:,} files parsed · {self.evaluated:,} units evaluated")

    def _finding(self, finding: dict[str, Any], cached: bool) -> None:
        unit = finding["unit"]
        level = finding["severity"]
        heading = Text(f"{level.upper()}  ", style=f"bold {LEVEL_STYLES[level]}")
        heading.append(finding["rule"], style="bold")
        if finding["probability"] is not None:
            heading.append(f"   model P={finding['probability']:.3f}", style="dim")
        else:
            heading.append(f"   score={finding['value']:.2f}", style="dim")
        if cached:
            heading.append("  cached", style="dim")
        location = Text(f"{unit['path']}:{unit['start_line']}–{unit['end_line']}  {unit['qualified_name']}", style="cyan")
        message = Text(finding["message"])
        self.console.print(Panel(Group(heading, location, message), border_style=LEVEL_STYLES[level], padding=(0, 1)))

    def _finish(self, event: dict[str, Any]) -> None:
        summary = {key: value for key, value in event.items() if key != "event"}
        if self.format == "json":
            self.stream.write('],"summary":' + json.dumps(summary, ensure_ascii=False) + "}\n")
        elif self.format == "jsonl":
            self._line(event)
        else:
            if self.progress:
                self.progress.stop()
            table = Table.grid(padding=(0, 3))
            table.add_column(style="dim")
            table.add_column(justify="right")
            rows = [
                ("Files parsed / discovered", f"{summary['files_parsed']:,} / {summary['files_discovered']:,}"),
                ("Code units", f"{summary['units_found']:,}"),
                ("Evaluated / cache hits", f"{summary['units_evaluated']:,} / {summary['units_cached']:,}"),
                ("Skipped / failed units", f"{summary['units_skipped']:,} / {summary['units_failed']:,}"),
                ("Findings", " · ".join(f"{n:,} {level}" for level, n in summary["findings"].items())),
                ("API attempts", f"{summary['requests']:,}"),
                ("Elapsed", f"{summary['elapsed_seconds']:.2f}s"),
            ]
            for label, value in rows:
                table.add_row(label, value)
            label = "INCOMPLETE" if summary["incomplete"] else ("INVENTORY COMPLETE" if summary["mode"] == "offline" else "SCAN COMPLETE")
            self.console.print(Panel(table, title=label, border_style="red" if summary["incomplete"] else "cyan"))
            if self.hidden:
                self.console.print(f"{self.hidden:,} additional details omitted from terminal display; use --format jsonl for the full report.", style="dim")
        self.stream.flush()
        self.closed = True
