"""Alexandria MCP server — exposes the personal PDF library to any
MCP-capable client (Claude CLI / Desktop, future clients).

Lives in a separate process from the GTK app, talks MCP over stdio.
Designed per the V3 sketch (`chat-stuff/ALEXANDRIA_MCP_SKETCH_V3.md`):
sidecar is source of truth, SQLite index is a regenerated cache;
reads go through the DB for speed, writes go through the sidecar.

v0 skeleton: one diagnostic `ping` tool and a read-only DB connection.
Real read tools land in subsequent steps."""

__version__ = "0.0.1"
