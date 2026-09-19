"""Bounded producer/process-parser/async-evaluator pipeline.

A fixed number of file evaluators consume parsed snapshots. Each owns its request
plans and result attribution; only one file of results is retained per evaluator.
"""

import asyncio
import multiprocessing
import os
import signal
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import AsyncExitStack
from dataclasses import asdict
from itertools import islice
from pathlib import Path

from jevscan.core.cache import AnswerCache
from jevscan.core.client import JevClient
from jevscan.core.config import Config, LoadedConfig
from jevscan.core.context import ContextBuilder
from jevscan.core.discovery import discover
from jevscan.core.evaluation import evaluate_file
from jevscan.core.model_limits import TokenCalibration
from jevscan.core.models import Diagnostic, EventSink, FileJob, ParsedFile, Severity, Summary, emit_diagnostic
from jevscan.core.parser import parse_batch, require_parser_runtime
from jevscan.core.planning import Planner
from jevscan.core.protocol import BudgetExhaustedError
from jevscan.core.retrieval import SourceIndex

ParseFunction = Callable[[list[FileJob]], Awaitable[list[ParsedFile]]]


def worker_count(config: Config) -> int:
    return config.scan.jobs or min(8, os.cpu_count() or 1)


def _ignore_sigint() -> None:
    """The parent owns Ctrl-C; parser children exit through executor shutdown."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _next_batch(iterator: Iterator[FileJob | Diagnostic], size: int) -> list[FileJob | Diagnostic]:
    return list(islice(iterator, size))


async def _produce(
    targets: list[Path],
    loaded: LoadedConfig,
    queue: asyncio.Queue[list[FileJob] | None],
    workers: int,
    sink: EventSink,
    summary: Summary,
) -> None:
    iterator = discover(targets, loaded.root, loaded.config.scan)
    try:
        while True:
            # Shield the owning filesystem operation: never close a generator while its
            # worker thread is still executing it after cancellation of this producer.
            fetch = asyncio.create_task(asyncio.to_thread(_next_batch, iterator, loaded.config.scan.batch_size))
            try:
                batch = await asyncio.shield(fetch)
            except asyncio.CancelledError:
                await asyncio.gather(fetch, return_exceptions=True)
                raise
            if not batch:
                break
            jobs = []
            for item in batch:
                if isinstance(item, Diagnostic):
                    emit_diagnostic(sink, summary, item)
                else:
                    jobs.append(item)
                    summary.files_discovered += 1
            if jobs:
                await queue.put(jobs)
    finally:
        iterator.close()
    for _ in range(workers):
        await queue.put(None)


async def _parse_worker(
    queue: asyncio.Queue[list[FileJob] | None],
    work_queue: asyncio.Queue[ParsedFile | None],
    parse: ParseFunction,
    loaded: LoadedConfig,
    live: bool,
    plan_only: bool,
    sink: EventSink,
    summary: Summary,
) -> None:
    while (jobs := await queue.get()) is not None:
        files = await parse(jobs)
        for parsed in files:
            for diagnostic in parsed.diagnostics:
                emit_diagnostic(sink, summary, diagnostic)
            if parsed.failed:
                summary.files_failed += 1
                continue
            summary.files_parsed += 1
            summary.units_found += len(parsed.units)
            sink.emit({"event": "file", "path": parsed.path, "language": parsed.language, "units": len(parsed.units)})
            if plan_only:
                planner = Planner(ContextBuilder(parsed), loaded.config)
                requests = list(planner.plan())
                planned_tokens = 0
                planned_state_bytes = 0
                for request in requests:
                    _, total_tokens, _ = planner.estimate(request.evidence, request.checks)
                    planned_tokens += total_tokens
                    planned_state_bytes += len(request.evidence.encoded)
                checks = len(planner.checks) + len(planner.omissions)
                summary.planned_checks += checks
                summary.planned_requests += len(requests)
                summary.planned_input_tokens += planned_tokens
                summary.planned_state_bytes += planned_state_bytes
                summary.checks_skipped += len(planner.omissions)
                sink.emit({
                    "event": "plan",
                    "path": parsed.path,
                    "checks": checks,
                    "requests": len(requests),
                    "omitted_checks": len(planner.omissions),
                    "estimated_input_tokens": planned_tokens,
                    "state_bytes": planned_state_bytes,
                })
                continue
            if not live:
                for unit in parsed.units:
                    sink.emit({"event": "unit", "unit": unit.metadata()})
                continue
            await work_queue.put(parsed)


async def _evaluate_worker(
    queue: asyncio.Queue[ParsedFile | None],
    loaded: LoadedConfig,
    client: JevClient,
    cache: AnswerCache | None,
    sink: EventSink,
    summary: Summary,
    index: SourceIndex | None,
    calibration: TokenCalibration | None,
) -> None:
    while (parsed := await queue.get()) is not None:
        planner = Planner(ContextBuilder(parsed), loaded.config, calibration)
        if planner.full_file_limited and planner.omissions:
            limit = loaded.config.scan.max_full_file_lines
            emit_diagnostic(
                sink,
                summary,
                Diagnostic(
                    parsed.path,
                    "file-size-limit",
                    f"{planner.context.file.end_line} lines exceeds the configured full-file analysis limit "
                    f"of {limit}; {len(planner.omissions)} full-file-context checks were not evaluated",
                    Severity.ERROR,
                ),
            )
        selected = {check.target.id for check in planner.checks if check.target.scope == "unit"}
        selected.update(item.check.target.id for item in planner.omissions if item.check.target.scope == "unit")
        summary.units_skipped += len(parsed.units) - len(selected)
        await evaluate_file(planner, client, cache, sink, summary, index)


async def pipeline(
    targets: list[Path],
    loaded: LoadedConfig,
    parse: ParseFunction,
    sink: EventSink,
    summary: Summary,
    client: JevClient | None = None,
    cache: AnswerCache | None = None,
    *,
    plan_only: bool = False,
) -> None:
    config = loaded.config
    parsers = worker_count(config)
    evaluators = config.jev.concurrency if client else 0
    index = (
        SourceIndex(loaded.root, config.scan, config.enrichment)
        if client and config.enrichment.enabled and config.enrichment.mode != "off"
        else None
    )
    calibration = TokenCalibration() if client else None
    file_queue: asyncio.Queue[list[FileJob] | None] = asyncio.Queue(maxsize=parsers * 2)
    work_queue: asyncio.Queue[ParsedFile | None] = asyncio.Queue(maxsize=config.scan.queue_size)

    async def parse_stage() -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(_produce(targets, loaded, file_queue, parsers, sink, summary))
            for _ in range(parsers):
                group.create_task(
                    _parse_worker(file_queue, work_queue, parse, loaded, client is not None, plan_only, sink, summary)
                )
        for _ in range(evaluators):
            await work_queue.put(None)

    async with asyncio.TaskGroup() as group:
        group.create_task(parse_stage())
        if client:
            for _ in range(evaluators):
                group.create_task(
                    _evaluate_worker(work_queue, loaded, client, cache, sink, summary, index, calibration)
                )


def _exception_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_exception_message(child) for child in exc.exceptions))
    return f"{type(exc).__name__}: {exc}"


def _contains_exception(exc: BaseException, kind: type[BaseException]) -> bool:
    if isinstance(exc, kind):
        return True
    return isinstance(exc, BaseExceptionGroup) and any(_contains_exception(child, kind) for child in exc.exceptions)


async def run_scan(
    targets: list[Path],
    loaded: LoadedConfig,
    sink: EventSink,
    *,
    offline: bool = False,
    plan_only: bool = False,
    no_cache: bool = False,
    api_key: str = "",
    base_url: str = "https://api.typesafe.ai",
) -> Summary:
    summary = Summary(mode="offline" if offline else "plan" if plan_only else "live")
    started = time.monotonic()
    client = None
    pool = None
    try:
        require_parser_runtime()
        async with AsyncExitStack() as resources:
            if not offline and not plan_only:
                client = await resources.enter_async_context(
                    JevClient(
                        loaded.config.jev,
                        api_key,
                        base_url=base_url,
                        budget=loaded.config.budget,
                        bytes_per_token=loaded.config.evaluation.bytes_per_token,
                        token_reserve=loaded.config.evaluation.token_reserve,
                    )
                )
            cache = None
            if client and loaded.config.cache.enabled and not no_cache:
                path = loaded.root / loaded.config.cache.path
                cache = await resources.enter_async_context(AnswerCache(path, loaded.config.cache.ttl_seconds))
            # spawn avoids inheriting an event loop, HTTP sockets, SQLite, or parser state on macOS/Linux.
            pool = ProcessPoolExecutor(
                max_workers=worker_count(loaded.config),
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_ignore_sigint,
            )
            loop = asyncio.get_running_loop()

            async def parse(jobs: list[FileJob]) -> list[ParsedFile]:
                return await loop.run_in_executor(
                    pool, parse_batch, jobs, loaded.config.scan.max_file_bytes, loaded.config.scan.max_units_per_file
                )

            if client is None:
                await pipeline(targets, loaded, parse, sink, summary, client, cache, plan_only=plan_only)
            else:

                async def progress() -> None:
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

                progress_task = asyncio.create_task(progress())
                try:
                    await pipeline(targets, loaded, parse, sink, summary, client, cache, plan_only=plan_only)
                finally:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
    except asyncio.CancelledError:
        emit_diagnostic(
            sink, summary, Diagnostic("", "cancelled", "scan interrupted; results are incomplete", Severity.ERROR)
        )
        raise
    except Exception as exc:  # noqa: BLE001 -- CLI boundary preserves an incomplete report on operational failure
        code = "budget-exhausted" if _contains_exception(exc, BudgetExhaustedError) else "scan-failed"
        emit_diagnostic(sink, summary, Diagnostic("", code, _exception_message(exc), Severity.ERROR))
    finally:
        if pool:
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
        summary.requests = client.requests if client else 0
        if client:
            summary.retry_attempts = client.retry_attempts
            summary.estimated_input_tokens = client.estimated_input_tokens
            summary.estimated_cost = round(client.estimated_cost, 6)
            summary.reported_cost = round(
                summary.input_tokens * loaded.config.budget.input_cost_per_million / 1_000_000, 6
            )
        elif plan_only:
            summary.estimated_input_tokens = summary.planned_input_tokens
            summary.estimated_cost = round(
                summary.planned_input_tokens * loaded.config.budget.input_cost_per_million / 1_000_000, 6
            )
        summary.elapsed_seconds = round(time.monotonic() - started, 3)
        sink.emit({"event": "summary", **asdict(summary)})
    return summary
