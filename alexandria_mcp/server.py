"""FastMCP app and tool registrations.

v0: one diagnostic tool (`ping`) that confirms the server is
connected to the expected library and reports basic counts. Real
read tools (`search_library`, `find_by_dois`, `get_papers`,
`get_sidecars`) land in subsequent steps."""

import os

from mcp.server.fastmcp import FastMCP

from . import __version__, config, db


mcp = FastMCP("Alexandria")


@mcp.tool()
def ping() -> dict:
    """Diagnostic: confirm the MCP server is up and report which
    Alexandria library it's pointed at.

    Returns:
        library_root: absolute path to the PDF library directory.
        db_path: absolute path to the SQLite index file.
        db_exists: whether the index file is present on disk.
        paper_count: total rows in `papers` (NULL if db missing).
        readonly: whether write tools are disabled.
        mcp_server_version: version of the alexandria_mcp package.

    Use this first in any session to verify the server sees the
    library you expect."""
    out = {
        "library_root": config.library_root(),
        "db_path": config.db_path(),
        "db_exists": os.path.isfile(config.db_path()),
        "paper_count": None,
        "readonly": config.readonly(),
        "mcp_server_version": __version__,
    }
    if out["db_exists"]:
        try:
            conn = db.get_ro_connection()
            row = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()
            out["paper_count"] = int(row["n"])
        except Exception as e:
            out["paper_count_error"] = str(e)
    return out
