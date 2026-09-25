"""Lossless machine reports plus delegated human-readable rendering."""

import json
from typing import Any, TextIO

from jevscan.cli.terminal import Terminal
from jevscan.cli.text_report import TextReport

REPORT_SCHEMA_VERSION = 9


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
        self.stream = stream
        self.format = format_name
        self.first = True
        self.closed = False
        self.terminal = Terminal(stream, width)
        self.text = (
            TextReport(
                stream,
                self.terminal,
                metadata,
                max_display=max_display,
                verbose=verbose,
            )
            if format_name == "text"
            else None
        )
        self._start_machine(metadata)

    def _start_machine(self, metadata: dict[str, Any]) -> None:
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

    def _line(self, event: dict[str, Any]) -> None:
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def _json_event(self, event: dict[str, Any]) -> None:
        if not self.first:
            self.stream.write(",")
        self.first = False
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    def _machine_event(self, event: dict[str, Any]) -> None:
        if self.format == "jsonl":
            self._line(event)
        elif self.format == "json":
            self._json_event(event)

    def _finish(self, event: dict[str, Any]) -> None:
        summary = {key: value for key, value in event.items() if key != "event"}
        if self.format == "json":
            self.stream.write('],"summary":' + json.dumps(summary, ensure_ascii=False) + "}\n")
        elif self.format == "jsonl":
            self._line(event)
        else:
            assert self.text is not None
            self.text.summary(summary)
        self.stream.flush()
        self.closed = True

    def emit(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        kind = event["event"]
        if kind == "progress":
            if self.text is not None:
                self.text.progress(event)
            return
        if kind == "summary":
            self._finish(event)
            return
        if self.text is not None:
            self.text.emit(event)
        else:
            self._machine_event(event)
