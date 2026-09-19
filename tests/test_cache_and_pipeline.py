import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from jevscan.core.cache import AnswerCache, cache_key, judgment_cache_key
from jevscan.core.client import JevClient
from jevscan.core.config import Config, LoadedConfig
from jevscan.core.models import FileJob, Kind, ParsedFile, Summary, Unit
from jevscan.core.scanner import pipeline


class Sink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


async def synthetic_parse(jobs: list[FileJob]) -> list[ParsedFile]:
    # A pipeline contract fixture, not a replacement parser or a test of grammar correctness.
    await asyncio.sleep(0.001)
    files = []
    for job in jobs:
        source = b"def work():\n    return 1\n"
        unit = Unit(
            f"{job.display_path}:0:function",
            job.display_path,
            job.language,
            Kind.FUNCTION,
            "work",
            "work",
            None,
            0,
            len(source),
            1,
            2,
            "def work():",
            True,
        )
        files.append(ParsedFile(job.display_path, job.language, source, (unit,)))
    return files


async def test_pipeline_concurrency_cache_and_streamed_results(tmp_path: Path, config: Config) -> None:
    for index in range(80):
        (tmp_path / f"f{index}.py").write_text("def work(): return 1\n")
    active = peak = requests = peak_tasks = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak, requests, peak_tasks
        active += 1
        peak = max(peak, active)
        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
        requests += 1
        await asyncio.sleep(0.002)
        active -= 1
        assert "def work" in json.loads(request.content)["state"]["documents"][0]["content"]
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {name: {"type": "noul", "noul": 0.95} for name in json.loads(request.content)["questions"]},
            },
        )

    loaded = LoadedConfig(config, tmp_path, "test")
    async with AnswerCache(tmp_path / "cache.sqlite3", 3600) as cache:
        for cached in (False, True):
            sink = Sink()
            summary = Summary("live")
            async with JevClient(config.jev, "test", transport=httpx.MockTransport(handle)) as client:
                await pipeline([tmp_path], loaded, synthetic_parse, sink, summary, client, cache)
                assert client.requests == (0 if cached else 80)
            assert summary.units_evaluated == 80
            assert summary.units_cached == (80 if cached else 0)
            assert summary.findings["warning"] == 80
            assert len([e for e in sink.events if e["event"] == "evaluation"]) == 80
    assert 1 < peak <= config.jev.concurrency
    assert peak_tasks < 24  # Fixed workers, not one task per one of the 80 units.
    assert requests == 80


async def test_offline_pipeline_never_evaluates(tmp_path: Path, config: Config) -> None:
    (tmp_path / "x.py").write_text("def work(): return 1\n")
    sink, summary = Sink(), Summary("offline")
    await pipeline([tmp_path], LoadedConfig(config, tmp_path, "test"), synthetic_parse, sink, summary)
    assert summary.units_found == 1
    assert summary.units_evaluated == 0
    assert any(event["event"] == "unit" for event in sink.events)
    assert summary.exit_code("warning") == 0


async def test_cache_key_includes_endpoint_model_source_and_questions(tmp_path: Path) -> None:
    key = cache_key("https://api.typesafe.ai", b'{"model":"v1","source":"secret"}')
    assert key != cache_key("https://api.typesafe.ai", b'{"model":"v2","source":"secret"}')
    assert key != cache_key("https://other.example", b'{"model":"v1","source":"secret"}')
    async with AnswerCache(tmp_path / "cache.sqlite3", 3600) as cache:
        assert await cache.get(key) is None
        await cache.put(key, b'{"answers":{}}')
        assert await cache.get(key) == b'{"answers":{}}'
    assert b"secret" not in (tmp_path / "cache.sqlite3").read_bytes()


async def test_judgment_cache_identity_and_v1_database_upgrade(tmp_path: Path) -> None:
    state = b'{"documents":[{"content":"secret"}]}'
    question = b'{"type":"noul","instructions":"q"}'
    key = judgment_cache_key("https://api.typesafe.ai", "jev-1.13", state, question)
    assert key != judgment_cache_key("https://api.typesafe.ai", "jev-1.14", state, question)
    assert key != judgment_cache_key("https://api.typesafe.ai", "jev-1.13", state + b"x", question)
    assert key != judgment_cache_key("https://api.typesafe.ai", "jev-1.13", state, question + b"x")

    path = tmp_path / "cache.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE answers (key TEXT PRIMARY KEY, created REAL NOT NULL, body BLOB NOT NULL)")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    async with AnswerCache(path, 3600) as cache:
        assert await cache.get_judgment(key) is None
        await cache.put_judgment(key, b'{"model":"jev-1.13","answer":{"type":"noul","noul":0.2}}')
        assert await cache.get_judgment(key) is not None

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM judgments").fetchone()[0] == 1
    connection.close()
    assert b"secret" not in path.read_bytes()


async def test_api_failure_cancels_pipeline_instead_of_marking_units_clean(tmp_path: Path, config: Config) -> None:
    for index in range(20):
        (tmp_path / f"f{index}.py").write_text("")

    async def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    sink, summary = Sink(), Summary("live")
    async with JevClient(config.jev, "test", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(
                pipeline([tmp_path], LoadedConfig(config, tmp_path, "test"), synthetic_parse, sink, summary, client),
                timeout=3,
            )
        assert client.requests <= config.jev.concurrency
    assert summary.units_evaluated == 0
    assert summary.units_failed > 0


async def test_cancellation_waits_for_active_discovery_thread(tmp_path, config, monkeypatch) -> None:
    import threading

    from jevscan.core import scanner
    from jevscan.core.config import LoadedConfig
    from jevscan.core.models import FileJob, Summary

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def slow_discover(*_args):
        try:
            entered.set()
            release.wait(timeout=2)
            yield FileJob("sample.py", "sample.py", "python", "python")
        finally:
            closed.set()

    monkeypatch.setattr(scanner, "discover", slow_discover)
    task = asyncio.create_task(
        scanner._produce(
            [tmp_path],
            LoadedConfig(config, tmp_path, "test"),
            asyncio.Queue(2),
            1,
            Sink(),
            Summary(mode="offline"),
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
