"""One scan invocation: external resources, cancellation, progress, and final reporting.

Stage queues and worker scheduling live in :mod:`jevscan.core.scan_pipeline`.
Parsing, planning, evaluation, and report accounting retain their own owners.
"""

import asyncio
import multiprocessing
import time
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from jevscan.core.cache import AnswerCache
from jevscan.core.capture import FinalJudgmentSink
from jevscan.core.client import JevClient
from jevscan.core.config import LoadedConfig
from jevscan.core.models import Diagnostic, EventSink, FileJob, ParsedFile, Severity, Summary, emit_diagnostic
from jevscan.core.parser import parse_batch, require_parser_runtime
from jevscan.core.protocol import BudgetExhaustedError
from jevscan.core.scan_events import report_progress, report_summary
from jevscan.core.scan_pipeline import parser_worker_initializer, pipeline, worker_count


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

    async def open(self, stack: AsyncExitStack, *, live: bool, no_cache: bool, api_key: str, base_url: str) -> None:
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
            initializer=parser_worker_initializer,
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
