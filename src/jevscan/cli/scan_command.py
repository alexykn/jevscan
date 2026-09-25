"""Composition of one CLI scan invocation.

Argument parsing and process-level error handling stay in cli.main. This
module prepares validated scan inputs and owns report/capture lifetimes.
"""

import asyncio
import os
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jevscan import __version__
from jevscan.cli.render import Reporter
from jevscan.cli.scan_options import (
    git_selected_paths,
    validate_calibration_output,
    validate_report_output,
    validate_scan_options,
)
from jevscan.core.capture import FinalJudgmentRecorder
from jevscan.core.config import LoadedConfig
from jevscan.core.models import Summary
from jevscan.core.scanner import run_scan, worker_count


@dataclass(frozen=True, slots=True)
class PreparedScan:
    paths: list[Path]
    loaded: LoadedConfig
    metadata: dict[str, Any]
    base_url: str


def _mode(args: Any) -> str:
    if args.offline:
        return "offline"
    return "plan" if args.plan else "live"


def _metadata(paths: list[Path], loaded: LoadedConfig, args: Any) -> dict[str, Any]:
    return {
        "version": __version__,
        "root": str(loaded.root),
        "config": loaded.source,
        "mode": _mode(args),
        "model": loaded.config.jev.model,
        "parser_processes": worker_count(loaded.config),
        "concurrency": 0 if args.offline else loaded.config.jev.concurrency,
        "targets": [str(path) for path in paths],
    }


def prepare_scan(paths: list[Path], loaded: LoadedConfig, args: Any) -> PreparedScan:
    selected_paths = git_selected_paths(loaded.root, paths, staged=args.staged) if args.changed or args.staged else paths
    validate_scan_options(loaded.config, args)
    validate_report_output(args.output, selected_paths, loaded.source)
    validate_calibration_output(args.calibration_output, args.output, selected_paths, loaded.source)
    base_url = os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.typesafe.ai"
    return PreparedScan(selected_paths, loaded, _metadata(selected_paths, loaded, args), base_url)


def _recorder(prepared: PreparedScan, args: Any) -> FinalJudgmentRecorder | None:
    if args.calibration_output is None:
        return None
    return FinalJudgmentRecorder(
        args.calibration_output,
        prepared.loaded.config.jev.model,
        prepared.base_url,
        prepared.metadata,
    )


def _run(prepared: PreparedScan, args: Any, reporter: Reporter, recorder: FinalJudgmentRecorder | None) -> Summary:
    return asyncio.run(
        run_scan(
            prepared.paths,
            prepared.loaded,
            reporter,
            offline=args.offline,
            plan_only=args.plan,
            no_cache=args.no_cache,
            api_key=os.environ.get("TYPESAFE_API_KEY", ""),
            base_url=prepared.base_url,
            capture=recorder,
        )
    )


def execute_scan(prepared: PreparedScan, args: Any) -> int:
    recorder = _recorder(prepared, args)
    try:
        context = args.output.open("w", encoding="utf-8") if args.output else nullcontext(sys.stdout)
        with context as output:
            reporter = Reporter(
                output,
                args.format,
                prepared.metadata,
                max_display=args.max_display,
                verbose=args.verbose,
            )
            summary = _run(prepared, args, reporter, recorder)
            if recorder is not None:
                recorder.mark_complete(
                    not summary.incomplete,
                    "complete" if not summary.incomplete else "incomplete",
                )
        return summary.exit_code(args.fail_on)
    finally:
        if recorder is not None:
            recorder.close()
