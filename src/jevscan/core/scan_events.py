"""Scan-stage event and summary accounting, separate from worker scheduling."""

import asyncio
import time
from dataclasses import asdict

from jevscan.core.client import JevClient
from jevscan.core.config import Config
from jevscan.core.context import ContextBuilder
from jevscan.core.models import Diagnostic, EventSink, ParsedFile, Severity, Summary, emit_diagnostic
from jevscan.core.planning import Planner


def record_parsed_file(parsed: ParsedFile, sink: EventSink, summary: Summary) -> bool:
    for diagnostic in parsed.diagnostics:
        emit_diagnostic(sink, summary, diagnostic)
    if parsed.failed:
        summary.files_failed += 1
        return False
    summary.files_parsed += 1
    summary.units_found += len(parsed.units)
    sink.emit({"event": "file", "path": parsed.path, "language": parsed.language, "units": len(parsed.units)})
    return True


def report_plan(parsed: ParsedFile, config: Config, sink: EventSink, summary: Summary) -> None:
    planner = Planner(ContextBuilder(parsed), config)
    requests = list(planner.plan())
    planned_tokens = 0
    planned_state_bytes = 0
    for request in requests:
        _, total_tokens, _ = planner.estimate(request.evidence, request.checks)
        planned_tokens += total_tokens
        planned_state_bytes += len(request.state)
    checks = len(planner.checks) + len(planner.omissions)
    applicability_skips = sum(len(skipped) for skipped in planner.applicability_skips.values())
    summary.planned_checks += checks
    summary.planned_requests += len(requests)
    summary.planned_input_tokens += planned_tokens
    summary.planned_state_bytes += planned_state_bytes
    summary.checks_skipped += len(planner.omissions)
    summary.applicability_skips += applicability_skips
    summary.not_applicable += applicability_skips
    sink.emit({
        "event": "plan",
        "path": parsed.path,
        "checks": checks,
        "requests": len(requests),
        "omitted_checks": len(planner.omissions),
        "applicability_skips": applicability_skips,
        "applicability_details": planner.applicability_skips,
        "estimated_input_tokens": planned_tokens,
        "state_bytes": planned_state_bytes,
    })


def record_evaluation_plan(planner: Planner, config: Config, sink: EventSink, summary: Summary) -> None:
    if planner.full_file_limited and planner.omissions:
        limit = config.scan.max_full_file_lines
        emit_diagnostic(
            sink,
            summary,
            Diagnostic(
                planner.context.parsed.path,
                "file-size-limit",
                f"{planner.context.file.end_line} lines exceeds the configured full-file analysis limit "
                f"of {limit}; {len(planner.omissions)} full-file-context checks were not evaluated",
                Severity.ERROR,
            ),
        )
    selected = {check.target.id for check in planner.checks if check.target.scope == "unit"}
    selected.update(item.check.target.id for item in planner.omissions if item.check.target.scope == "unit")
    selected.update(target_id for target_id in planner.applicability_skips if target_id != planner.context.file.id)
    summary.units_skipped += len(planner.context.parsed.units) - len(selected)


async def report_progress(client: JevClient, sink: EventSink, summary: Summary, started: float) -> None:
    while True:
        await asyncio.sleep(1.0)
        sink.emit({
            "event": "progress",
            "requests": client.requests,
            "completed_requests": client.completed_requests,
            "cache_hits": summary.cache_hits,
            "input_tokens": summary.input_tokens,
            "reserved_input_tokens": client.estimated_input_tokens,
            "estimated_cost": client.estimated_cost,
            "elapsed_seconds": round(time.monotonic() - started, 1),
        })


def report_summary(
    summary: Summary,
    client: JevClient | None,
    config: Config,
    sink: EventSink,
    started: float,
    *,
    plan_only: bool,
) -> None:
    summary.requests = client.requests if client else 0
    if client:
        summary.retry_attempts = client.retry_attempts
        summary.estimated_input_tokens = client.estimated_input_tokens
        summary.estimated_cost = round(client.estimated_cost, 6)
        summary.reported_cost = round(summary.input_tokens * config.budget.input_cost_per_million / 1_000_000, 6)
    elif plan_only:
        summary.estimated_input_tokens = summary.planned_input_tokens
        summary.estimated_cost = round(
            summary.planned_input_tokens * config.budget.input_cost_per_million / 1_000_000, 6
        )
    summary.elapsed_seconds = round(time.monotonic() - started, 3)
    sink.emit({"event": "summary", **asdict(summary)})
