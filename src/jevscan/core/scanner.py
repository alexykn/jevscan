"""Bounded discovery, process-parser, and async-evaluator stages.

This module owns queues, workers, and invocation lifetimes. Stage accounting is
in scan_events; parsing, planning, and evaluation retain their own contracts.
"""

import asyncio
import multiprocessing
import os
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from itertools import islice
from pathlib import Path

from jevscan.core.cache import AnswerCache
from jevscan.core.capture import FinalJudgmentSink
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
from jevscan.core.scan_events import (
    record_evaluation_plan,
    record_parsed_file,
    report_plan,
    report_progress,
    report_summary,
)

ParseFunction = Callable[[list[FileJob]], Awaitable[list[ParsedFile]]]


def worker_count(config: Config) -> int:
    return config.scan.jobs or min(8, os.cpu_count() or 1)


def _ignore_sigint() -> None:
    """The parent owns Ctrl-C; parser children exit through executor shutdown."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _next_batch(iterator: Iterator[FileJob | Diagnostic], size: int) -> list[FileJob | Diagnostic]:
    return list(islice(iterator, size))


async def _fetch_batch(iterator: Iterator[FileJob | Diagnostic], size: int) -> list[FileJob | Diagnostic]:
    # The owning generator must not close while its thread is still advancing it.
    fetch = asyncio.create_task(asyncio.to_thread(_next_batch, iterator, size))
    try:
        return await asyncio.shield(fetch)
    except asyncio.CancelledError:
        await asyncio.gather(fetch, return_exceptions=True)
        raise


def _discovered_jobs(batch: list[FileJob | Diagnostic], sink: EventSink, summary: Summary) -> list[FileJob]:
    jobs = []
    for item in batch:
        if isinstance(item, Diagnostic):
            emit_diagnostic(sink, summary, item)
        else:
            jobs.append(item)
            summary.files_discovered += 1
    return jobs


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
        while batch := await _fetch_batch(iterator, loaded.config.scan.batch_size):
            jobs = _discovered_jobs(batch, sink, summary)
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
        for parsed in await parse(jobs):
            if not record_parsed_file(parsed, sink, summary):
                continue
            if plan_only:
                report_plan(parsed, loaded.config, sink, summary)
            elif live:
                await work_queue.put(parsed)
            else:
                for unit in parsed.units:
                    sink.emit({"event": "unit", "unit": unit.metadata()})


async def _evaluate_worker(
    queue: asyncio.Queue[ParsedFile | None],
    loaded: LoadedConfig,
    client: JevClient,
    cache: AnswerCache | None,
    sink: EventSink,
    summary: Summary,
    index: SourceIndex | None,
    calibration: TokenCalibration | None,
    capture: FinalJudgmentSink | None,
) -> None:
    while (parsed := await queue.get()) is not None:
        planner = Planner(ContextBuilder(parsed), loaded.config, calibration)
        record_evaluation_plan(planner, loaded.config, sink, summary)
        await evaluate_file(planner, client, cache, sink, summary, index, capture)


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
    capture: FinalJudgmentSink | None = None,
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
                    _evaluate_worker(work_queue, loaded, client, cache, sink, summary, index, calibration, capture)
                )


def _exception_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_exception_message(child) for child in exc.exceptions))
    return f"{type(exc).__name__}: {exc}"


def _contains_exception(exc: BaseException, kind: type[BaseException]) -> bool:
    if isinstance(exc, kind):
        return True
    return isinstance(exc, BaseExceptionGroup) and any(_contains_exception(child, kind) for child in exc.exceptions)


class _ScanResources:
    """Own the client/cache contexts and process pool for one invocation."""

    def __init__(self, loaded: LoadedConfig) -> None:
        self.loaded = loaded
        self.client: JevClient | None = None
        self.cache: AnswerCache | None = None
        self.pool: ProcessPoolExecutor | None = None

    async def open(
        self, stack: AsyncExitStack, *, live: bool, no_cache: bool, api_key: str, base_url: str
    ) -> None:
        config = self.loaded.config
        if live:
            self.client = await stack.enter_async_context(
                JevClient(
                    config.jev,
                    api_key,
                    base_url=base_url,
                    budget=config.budget,
                    bytes_per_token=config.evaluation.bytes_per_token,
                    token_reserve=config.evaluation.token_reserve,
                )
            )
        if self.client and config.cache.enabled and not no_cache:
            path = self.loaded.root / config.cache.path
            self.cache = await stack.enter_async_context(AnswerCache(path, config.cache.ttl_seconds))
        # spawn avoids inheriting event-loop, HTTP, SQLite, and parser state.
        self.pool = ProcessPoolExecutor(
            max_workers=worker_count(config),
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_ignore_sigint,
        )

    async def parse(self, jobs: list[FileJob]) -> list[ParsedFile]:
        assert self.pool is not None
        scan = self.loaded.config.scan
        return await asyncio.get_running_loop().run_in_executor(
            self.pool, parse_batch, jobs, scan.max_file_bytes, scan.max_units_per_file
        )

    async def close_pool(self) -> None:
        if self.pool is not None:
            await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)


@asynccontextmanager
async def _progress_reporting(
    client: JevClient | None, sink: EventSink, summary: Summary, started: float
) -> AsyncIterator[None]:
    if client is None:
        yield
        return
    task = asyncio.create_task(report_progress(client, sink, summary, started))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
    capture: FinalJudgmentSink | None = None,
) -> Summary:
    summary = Summary(mode="offline" if offline else "plan" if plan_only else "live")
    started = time.monotonic()
    runtime = _ScanResources(loaded)
    try:
        require_parser_runtime()
        async with AsyncExitStack() as resources:
            await runtime.open(
                resources,
                live=not offline and not plan_only,
                no_cache=no_cache,
                api_key=api_key,
                base_url=base_url,
            )
            async with _progress_reporting(runtime.client, sink, summary, started):
                await pipeline(
                    targets,
                    loaded,
                    runtime.parse,
                    sink,
                    summary,
                    runtime.client,
                    runtime.cache,
                    plan_only=plan_only,
                    capture=capture,
                )
    except asyncio.CancelledError:
        emit_diagnostic(
            sink, summary, Diagnostic("", "cancelled", "scan interrupted; results are incomplete", Severity.ERROR)
        )
        raise
    except Exception as exc:  # noqa: BLE001 -- Preserve an incomplete report on operational failure.
        code = "budget-exhausted" if _contains_exception(exc, BudgetExhaustedError) else "scan-failed"
        emit_diagnostic(sink, summary, Diagnostic("", code, _exception_message(exc), Severity.ERROR))
    finally:
        await runtime.close_pool()
        report_summary(summary, runtime.client, loaded.config, sink, started, plan_only=plan_only)
    return summary
