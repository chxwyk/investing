"""Small stdlib fallback used when aiosqlite is unavailable.

Production installs aiosqlite. This fallback keeps diagnostics and unit tests runnable in
minimal Python environments. Database calls are short and still serialized by the engine.
"""

from __future__ import annotations

import sqlite3
from typing import Any

Row = sqlite3.Row


class Cursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    async def fetchone(self) -> sqlite3.Row | None:
        return self._cursor.fetchone()

    async def fetchall(self) -> list[sqlite3.Row]:
        return self._cursor.fetchall()


class Connection:
    def __init__(self, path: str) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)

    @property
    def row_factory(self) -> Any:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._connection.row_factory = value

    async def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Cursor:
        return Cursor(self._connection.execute(sql, parameters))

    async def executescript(self, script: str) -> Cursor:
        return Cursor(self._connection.executescript(script))

    async def commit(self) -> None:
        self._connection.commit()

    async def rollback(self) -> None:
        self._connection.rollback()

    async def close(self) -> None:
        self._connection.close()


async def connect(path: str) -> Connection:
    return Connection(path)

