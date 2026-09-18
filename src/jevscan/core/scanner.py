"""Bounded producer/process-parser/async-evaluator pipeline.

A fixed number of file evaluators consume parsed snapshots. Each owns its request
plans and result attribution; only one file of results is retained per evaluator.
"""

import asyncio
import multiprocessing
import os
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
from jevscan.core.models import Diagnostic, EventSink, FileJob, ParsedFile, Severity, Summary, emit_diagnostic
from jevscan.core.parser import parse_batch, require_parser_runtime
from jevscan.core.planning import Planner

ParseFunction = Callable[[list[FileJob]], Awaitable[list[ParsedFile]]]


def worker_count(config: Config) -> int:
    return config.scan.jobs or min(8, os.cpu_count() or 1)


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
    live: bool,
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
) -> None:
    while (parsed := await queue.get()) is not None:
        planner = Planner(ContextBuilder(parsed), loaded.config)
        selected = {check.target.id for check in planner.checks if check.target.scope == "unit"}
        summary.units_skipped += len(parsed.units) - len(selected)
        await evaluate_file(planner, client, cache, sink, summary)


async def pipeline(
    targets: list[Path],
    loaded: LoadedConfig,
    parse: ParseFunction,
    sink: EventSink,
    summary: Summary,
    client: JevClient | None = None,
    cache: AnswerCache | None = None,
) -> None:
    config = loaded.config
    parsers = worker_count(config)
    evaluators = config.jev.concurrency if client else 0
    file_queue: asyncio.Queue[list[FileJob] | None] = asyncio.Queue(maxsize=parsers * 2)
    work_queue: asyncio.Queue[ParsedFile | None] = asyncio.Queue(maxsize=config.scan.queue_size)

    async def parse_stage() -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(_produce(targets, loaded, file_queue, parsers, sink, summary))
            for _ in range(parsers):
                group.create_task(_parse_worker(file_queue, work_queue, parse, client is not None, sink, summary))
        for _ in range(evaluators):
            await work_queue.put(None)

    async with asyncio.TaskGroup() as group:
        group.create_task(parse_stage())
        if client:
            for _ in range(evaluators):
                group.create_task(_evaluate_worker(work_queue, loaded, client, cache, sink, summary))


def _exception_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_exception_message(child) for child in exc.exceptions))
    return f"{type(exc).__name__}: {exc}"


async def run_scan(
    targets: list[Path],
    loaded: LoadedConfig,
    sink: EventSink,
    *,
    offline: bool = False,
    no_cache: bool = False,
    api_key: str = "",
    base_url: str = "https://api.typesafe.ai",
) -> Summary:
    summary = Summary(mode="offline" if offline else "live")
    started = time.monotonic()
    client = None
    pool = None
    try:
        require_parser_runtime()
        async with AsyncExitStack() as resources:
            if not offline:
                client = await resources.enter_async_context(JevClient(loaded.config.jev, api_key, base_url=base_url))
            cache = None
            if client and loaded.config.cache.enabled and not no_cache:
                path = loaded.root / loaded.config.cache.path
                cache = await resources.enter_async_context(AnswerCache(path, loaded.config.cache.ttl_seconds))
            # spawn avoids inheriting an event loop, HTTP sockets, SQLite, or parser state on macOS/Linux.
            pool = ProcessPoolExecutor(
                max_workers=worker_count(loaded.config), mp_context=multiprocessing.get_context("spawn")
            )
            loop = asyncio.get_running_loop()

            async def parse(jobs: list[FileJob]) -> list[ParsedFile]:
                return await loop.run_in_executor(
                    pool, parse_batch, jobs, loaded.config.scan.max_file_bytes, loaded.config.scan.max_units_per_file
                )

            await pipeline(targets, loaded, parse, sink, summary, client, cache)
    except asyncio.CancelledError:
        emit_diagnostic(
            sink, summary, Diagnostic("", "cancelled", "scan interrupted; results are incomplete", Severity.ERROR)
        )
        raise
    except Exception as exc:  # noqa: BLE001 -- CLI boundary preserves an incomplete report on operational failure
        emit_diagnostic(sink, summary, Diagnostic("", "scan-failed", _exception_message(exc), Severity.ERROR))
    finally:
        if pool:
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
        summary.requests = client.requests if client else 0
        summary.elapsed_seconds = round(time.monotonic() - started, 3)
        sink.emit({"event": "summary", **asdict(summary)})
    return summary
