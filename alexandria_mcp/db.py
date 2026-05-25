"""SQLite connection management.

v0 only needs a read-only handle for the diagnostic `ping` tool.
A separate writable handle is opened lazily in later steps for the
sidecar-write protocol (`BEGIN IMMEDIATE` as cross-process
rendezvous lock per the V3 sketch).

Connections are created on demand and cached per-thread (FastMCP
may dispatch tools from worker threads, and `sqlite3.Connection`
isn't thread-safe by default)."""

import sqlite3
import threading

from . import config


_tls = threading.local()


def get_ro_connection():
    """Return a read-only connection, cached per-thread."""
    conn = getattr(_tls, "ro_conn", None)
    if conn is not None:
        return conn
    uri = "file:{}?mode=ro".format(config.db_path())
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # In WAL mode reads don't normally block on writers, but during
    # a checkpoint there's a brief window where they can. Match
    # the GUI connection's 5 s wait so MCP tool calls don't pop
    # `database is locked` for a sub-second race.
    conn.execute("PRAGMA busy_timeout = 5000")
    _tls.ro_conn = conn
    return conn
