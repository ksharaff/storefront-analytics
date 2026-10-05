"""Thin PostgreSQL helper. Deliberately small — no ORM.

An ORM would buy us little here: the sync writes a handful of fixed upserts
and the metrics layer is hand-written analytical SQL, neither of which an ORM
improves.
"""

from contextlib import contextmanager

import psycopg

from app.config import DATABASE_URL


@contextmanager
def connect():
    """Open a connection, commit on success, roll back on any exception.

    Usage:
        with connect() as conn:
            conn.execute("INSERT ...")
    """
    conn = psycopg.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_one(sql: str, params: tuple = ()):
    """Run a query and return its first row (or None). For quick checks."""
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def get_sync_state(key: str, default=None):
    """Read a value from the sync_state scratchpad."""
    row = fetch_one("SELECT value FROM sync_state WHERE key = %s", (key,))
    return row[0] if row else default


def set_sync_state(key: str, value: str) -> None:
    """Write (or overwrite) a value in the sync_state scratchpad."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sync_state (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_at = now()
            """,
            (key, value),
        )
