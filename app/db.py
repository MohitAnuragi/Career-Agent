"""Connection pool and schema bootstrap."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_pools: dict[str, ConnectionPool] = {}
SCHEMA = Path(__file__).with_name("schema.sql")


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


def init_schema(url: str) -> None:
    # The vector extension must exist before register_vector can find the type,
    # so bootstrap on a plain connection first.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))


def get_pool(url: str | None = None) -> ConnectionPool:
    url = url or get_settings().database_url
    if url not in _pools:
        init_schema(url)
        _pools[url] = ConnectionPool(
            url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=True,
        )
    return _pools[url]


@contextmanager
def connection(url: str | None = None) -> Iterator[psycopg.Connection]:
    with get_pool(url).connection() as conn:
        yield conn


def close_pools() -> None:
    for pool in _pools.values():
        pool.close()
    _pools.clear()
