"""SQLite connection shared by both loaders."""

from pathlib import Path
import sqlite3
import time


def connect(path, timeout=60):
    """Open a WAL database in autocommit mode; callers run BEGIN IMMEDIATE themselves.

    WAL lets readers see the last committed state while a run writes. Switching a
    fresh file to WAL can fail with "locked" when two runs start at once, without
    going through SQLite's busy timeout, so that one step is retried by hand.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    deadline = time.monotonic() + timeout
    while True:
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            break
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc) or time.monotonic() > deadline:
                connection.close()
                raise
            time.sleep(0.05)
    connection.execute('PRAGMA synchronous=FULL')
    return connection
