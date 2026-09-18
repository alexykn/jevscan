"""Bounded producer/process-parser/async-evaluator pipeline.

Only a fixed number of parse futures and evaluator tasks exist. Source files and results
are consumed incrementally; neither code units nor report entries accumulate for a repository.
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
from typing import Any, Protocol

from jevscan.core.cache import AnswerCache, cache_key
from jevscan.core.client import JevClient, RequestTooLarge, findings_from, validate_response
from jevscan.core.config import Config, LoadedConfig
from jevscan.core.context import ContextBuilder, WorkItem
from jevscan.core.discovery import discover
from jevscan.core.models import Diagnostic, FileJob, ParsedFile, Severity, Summary
from jevscan.core.parser import parse_batch, require_parser_runtime


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


ParseFunction = Callable[[list[FileJob]], Awaitable[list[ParsedFile]]]


def worker_count(config: Config) -> int:
    return config.scan.jobs or min(8, os.cpu_count() or 1)


def _diagnostic(sink: EventSink, summary: Summary, diagnostic: Diagnostic) -> None:
    summary.diagnostics += 1
    summary.incomplete |= diagnostic.incomplete
    sink.emit({"event": "diagnostic", **asdict(diagnostic)})


def _next_batch(iterator: Iterator[FileJob | Diagnostic], size: int) -> list[FileJob | Diagnostic]:
    return list(islice(iterator, size))


async def _produce(targets: list[Path], loaded: LoadedConfig, queue: asyncio.Queue[list[FileJob] | None],
                   workers: int, sink: EventSink, summary: Summary) -> None:
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
                    _diagnostic(sink, summary, item)
                else:
                    jobs.append(item)
                    summary.files_discovered += 1
            if jobs:
                await queue.put(jobs)
    finally:
        iterator.close()
    for _ in range(workers):
        await queue.put(None)


async def _parse_worker(queue: asyncio.Queue[list[FileJob] | None], work_queue: asyncio.Queue[WorkItem | None],
                        config: Config, parse: ParseFunction, live: bool, sink: EventSink, summary: Summary) -> None:
    while (jobs := await queue.get()) is not None:
        files = await parse(jobs)
        for parsed in files:
            for diagnostic in parsed.diagnostics:
                _diagnostic(sink, summary, diagnostic)
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
            builder = ContextBuilder(parsed, config)
            for unit in parsed.units:
                if not builder.rules_for(unit):
                    summary.units_skipped += 1
                    continue
                if unit.end_byte - unit.start_byte > config.scan.max_unit_bytes:
                    summary.units_skipped += 1
                    _diagnostic(sink, summary, Diagnostic(unit.path, "unit-size-limit",
                        f"{unit.qualified_name}: exceeds scan.max_unit_bytes ({config.scan.max_unit_bytes}); nested units can still be evaluated",
                        line=unit.start_line))
                    continue
                await work_queue.put(builder.build(unit))


async def _evaluate_worker(queue: asyncio.Queue[WorkItem | None], client: JevClient, cache: AnswerCache | None,
                           sink: EventSink, summary: Summary) -> None:
    while (work := await queue.get()) is not None:
        try:
            body = client.body_for(work)
        except RequestTooLarge as exc:
            summary.units_skipped += 1
            _diagnostic(sink, summary, Diagnostic(work.unit.path, "request-size-limit", str(exc), line=work.unit.start_line))
            continue
        key = cache_key(client.base_url, body)
        try:
            raw = await cache.get(key) if cache else None
            if raw is not None:
                response = validate_response(raw, work.rules)
                summary.units_cached += 1
            else:
                response = await client.evaluate(body, work.rules)
                summary.input_tokens += response.usage.input_tokens or 0
                summary.output_tokens += response.usage.output_tokens or 0
                if cache:
                    await cache.put(key, response.model_dump_json().encode())
            findings = findings_from(work, response)
            summary.units_evaluated += 1
            for finding in findings:
                summary.findings[str(finding.severity)] += 1
            sink.emit({"event": "evaluation", "unit": work.unit.metadata(), "cached": raw is not None,
                       "model": response.model, "answers": response.model_dump(mode="json")["answers"],
                       "findings": [asdict(finding) for finding in findings]})
        except Exception:
            summary.units_failed += 1
            raise  # Fail the scan; do not turn an API/cache outage into thousands of fake clean units.


async def pipeline(targets: list[Path], loaded: LoadedConfig, parse: ParseFunction, sink: EventSink,
                   summary: Summary, client: JevClient | None = None, cache: AnswerCache | None = None) -> None:
    config = loaded.config
    parsers = worker_count(config)
    evaluators = config.jev.concurrency if client else 0
    file_queue: asyncio.Queue[list[FileJob] | None] = asyncio.Queue(maxsize=parsers * 2)
    work_queue: asyncio.Queue[WorkItem | None] = asyncio.Queue(maxsize=config.scan.queue_size)

    async def parse_stage() -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(_produce(targets, loaded, file_queue, parsers, sink, summary))
            for _ in range(parsers):
                group.create_task(_parse_worker(file_queue, work_queue, config, parse, client is not None, sink, summary))
        for _ in range(evaluators):
            await work_queue.put(None)

    async with asyncio.TaskGroup() as group:
        group.create_task(parse_stage())
        if client:
            for _ in range(evaluators):
                group.create_task(_evaluate_worker(work_queue, client, cache, sink, summary))


def _exception_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(_exception_message(child) for child in exc.exceptions))
    return f"{type(exc).__name__}: {exc}"


async def run_scan(targets: list[Path], loaded: LoadedConfig, sink: EventSink, *, offline: bool = False,
                   no_cache: bool = False, api_key: str = "", base_url: str = "https://api.typesafe.ai") -> Summary:
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
            pool = ProcessPoolExecutor(max_workers=worker_count(loaded.config), mp_context=multiprocessing.get_context("spawn"))
            loop = asyncio.get_running_loop()

            async def parse(jobs: list[FileJob]) -> list[ParsedFile]:
                return await loop.run_in_executor(pool, parse_batch, jobs, loaded.config.scan.max_file_bytes,
                                                  loaded.config.scan.max_units_per_file)

            await pipeline(targets, loaded, parse, sink, summary, client, cache)
    except asyncio.CancelledError:
        _diagnostic(sink, summary, Diagnostic("", "cancelled", "scan interrupted; results are incomplete", Severity.ERROR))
        raise
    except Exception as exc:
        _diagnostic(sink, summary, Diagnostic("", "scan-failed", _exception_message(exc), Severity.ERROR))
    finally:
        if pool:
            await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
        summary.requests = client.requests if client else 0
        summary.elapsed_seconds = round(time.monotonic() - started, 3)
        sink.emit({"event": "summary", **asdict(summary)})
    return summary
