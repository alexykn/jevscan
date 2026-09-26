"""Persistent answer cache; SQLite work runs on one dedicated thread, not the event loop."""

import asyncio
import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Self

from jevscan import __version__
from jevscan.core.protocol import PROMPT_VERSION


class CacheError(RuntimeError):
    """The cache could not be read or written safely."""


def cache_key(endpoint: str, body: bytes) -> str:
    prefix = f"jevscan:{__version__}:prompt:{PROMPT_VERSION}:{endpoint}\n".encode()
    return hashlib.sha256(prefix + body).hexdigest()


def judgment_cache_key(endpoint: str, model: str, state: bytes, question: bytes) -> str:
    prefix = f"jevscan:judgment:prompt:{PROMPT_VERSION}:{endpoint}:{model}\n".encode()
    return hashlib.sha256(prefix + state + b"\n" + question).hexdigest()


class AnswerCache:
    def __init__(self, path: Path, ttl_seconds: int) -> None:
        self.path = path
        self.ttl = ttl_seconds
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jevscan-cache")
        self.connection: sqlite3.Connection | None = None

    async def _call(self, fn: Any, *args: Any) -> Any:
        try:
            return await asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)
        except (sqlite3.Error, OSError) as exc:
            raise CacheError(f"cannot use cache {self.path}: {exc}; use --no-cache to bypass it") from exc

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2}:
            raise CacheError(f"unsupported cache schema {version}; use a new cache path")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, created REAL NOT NULL, body BLOB NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS judgments (key TEXT PRIMARY KEY, created REAL NOT NULL, body BLOB NOT NULL)"
        )
        self.connection.execute("PRAGMA user_version=2")
        if self.ttl:
            cutoff = time.time() - self.ttl
            self.connection.execute("DELETE FROM answers WHERE created < ?", (cutoff,))
            self.connection.execute("DELETE FROM judgments WHERE created < ?", (cutoff,))

    def _get(self, key: str) -> bytes | None:
        assert self.connection is not None
        row = self.connection.execute("SELECT created, body FROM answers WHERE key = ?", (key,)).fetchone()
        if row is None or (self.ttl and row[0] < time.time() - self.ttl):
            return None
        return row[1]

    def _put(self, key: str, body: bytes) -> None:
        assert self.connection is not None
        self.connection.execute(
            "INSERT OR REPLACE INTO answers (key, created, body) VALUES (?, ?, ?)", (key, time.time(), body)
        )

    def _get_judgment(self, key: str) -> bytes | None:
        assert self.connection is not None
        row = self.connection.execute("SELECT created, body FROM judgments WHERE key = ?", (key,)).fetchone()
        if row is None or (self.ttl and row[0] < time.time() - self.ttl):
            return None
        return row[1]

    def _put_judgment(self, key: str, body: bytes) -> None:
        assert self.connection is not None
        self.connection.execute(
            "INSERT OR REPLACE INTO judgments (key, created, body) VALUES (?, ?, ?)", (key, time.time(), body)
        )

    def _judgment_rows(self, keys: tuple[str, ...]) -> list[tuple[str, float, bytes]]:
        assert self.connection is not None
        placeholders = ",".join("?" for _ in keys)
        return self.connection.execute(
            f"SELECT key, created, body FROM judgments WHERE key IN ({placeholders})",  # noqa: S608 -- placeholders only
            keys,
        ).fetchall()

    def _fresh_judgments(self, rows: list[tuple[str, float, bytes]]) -> dict[str, bytes]:
        cutoff = time.time() - self.ttl if self.ttl else None
        return {key: body for key, created, body in rows if cutoff is None or created >= cutoff}

    def _get_judgments(self, keys: tuple[str, ...]) -> dict[str, bytes]:
        if not keys:
            return {}
        return self._fresh_judgments(self._judgment_rows(keys))

    def _put_judgments(self, entries: tuple[tuple[str, bytes], ...]) -> None:
        assert self.connection is not None
        if not entries:
            return
        created = time.time()
        self.connection.executemany(
            "INSERT OR REPLACE INTO judgments (key, created, body) VALUES (?, ?, ?)",
            ((key, created, body) for key, body in entries),
        )

    def _close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    async def get(self, key: str) -> bytes | None:
        return await self._call(self._get, key)

    async def put(self, key: str, body: bytes) -> None:
        await self._call(self._put, key, body)

    async def get_judgment(self, key: str) -> bytes | None:
        return await self._call(self._get_judgment, key)

    async def put_judgment(self, key: str, body: bytes) -> None:
        await self._call(self._put_judgment, key, body)

    async def get_judgments(self, keys: tuple[str, ...]) -> dict[str, bytes]:
        return await self._call(self._get_judgments, keys)

    async def put_judgments(self, entries: tuple[tuple[str, bytes], ...]) -> None:
        await self._call(self._put_judgments, entries)

    async def __aenter__(self) -> Self:
        try:
            await self._call(self._open)
        except BaseException:
            await self._call(self._close)
            self.executor.shutdown(wait=True)
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            await self._call(self._close)
        finally:
            self.executor.shutdown(wait=True)
