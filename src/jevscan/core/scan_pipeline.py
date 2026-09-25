"""Producer, parser, and evaluator stage scheduling for one scan."""

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Iterator
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
from jevscan.core.models import Diagnostic, EventSink, FileJob, ParsedFile, Summary, emit_diagnostic
from jevscan.core.planning import Planner
from jevscan.core.retrieval import SourceIndex
from jevscan.core.scan_events import record_evaluation_plan, record_parsed_file, report_plan

ParseFunction = Callable[[list[FileJob]], Awaitable[list[ParsedFile]]]


def worker_count(config: Config) -> int:
    return config.scan.jobs or min(8, os.cpu_count() or 1)


def parser_worker_initializer() -> None:
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


async def _parse_stage(
    targets: list[Path],
    loaded: LoadedConfig,
    parse: ParseFunction,
    file_queue: asyncio.Queue[list[FileJob] | None],
    work_queue: asyncio.Queue[ParsedFile | None],
    parsers: int,
    evaluators: int,
    live: bool,
    plan_only: bool,
    sink: EventSink,
    summary: Summary,
) -> None:
    async with asyncio.TaskGroup() as group:
        group.create_task(_produce(targets, loaded, file_queue, parsers, sink, summary))
        for _ in range(parsers):
            group.create_task(
                _parse_worker(file_queue, work_queue, parse, loaded, live, plan_only, sink, summary)
            )
    for _ in range(evaluators):
        await work_queue.put(None)


async def _evaluation_stage(
    work_queue: asyncio.Queue[ParsedFile | None],
    loaded: LoadedConfig,
    client: JevClient,
    cache: AnswerCache | None,
    evaluators: int,
    sink: EventSink,
    summary: Summary,
    index: SourceIndex | None,
    calibration: TokenCalibration,
    capture: FinalJudgmentSink | None,
) -> None:
    async with asyncio.TaskGroup() as group:
        for _ in range(evaluators):
            group.create_task(
                _evaluate_worker(work_queue, loaded, client, cache, sink, summary, index, calibration, capture)
            )


def _source_index(loaded: LoadedConfig, client: JevClient | None) -> SourceIndex | None:
    enrichment = loaded.config.enrichment
    if client is None or not enrichment.enabled or enrichment.mode == "off":
        return None
    return SourceIndex(loaded.root, loaded.config.scan, enrichment)


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
    evaluators = config.jev.concurrency if client is not None else 0
    file_queue: asyncio.Queue[list[FileJob] | None] = asyncio.Queue(maxsize=parsers * 2)
    work_queue: asyncio.Queue[ParsedFile | None] = asyncio.Queue(maxsize=config.scan.queue_size)

    async with asyncio.TaskGroup() as group:
        group.create_task(
            _parse_stage(
                targets,
                loaded,
                parse,
                file_queue,
                work_queue,
                parsers,
                evaluators,
                client is not None,
                plan_only,
                sink,
                summary,
            )
        )
        if client is not None:
            group.create_task(
                _evaluation_stage(
                    work_queue,
                    loaded,
                    client,
                    cache,
                    evaluators,
                    sink,
                    summary,
                    _source_index(loaded, client),
                    TokenCalibration(),
                    capture,
                )
            )


