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
        if version not in {0, 1}:
            raise CacheError(f"unsupported cache schema {version}; use a new cache path")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, created REAL NOT NULL, body BLOB NOT NULL)"
        )
        self.connection.execute("PRAGMA user_version=1")
        if self.ttl:
            self.connection.execute("DELETE FROM answers WHERE created < ?", (time.time() - self.ttl,))

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

    def _close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    async def get(self, key: str) -> bytes | None:
        return await self._call(self._get, key)

    async def put(self, key: str, body: bytes) -> None:
        await self._call(self._put, key, body)

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
