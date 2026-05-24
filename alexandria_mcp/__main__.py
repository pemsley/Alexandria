"""Entry point: `python -m alexandria_mcp`.

The MCP venv (where `mcp` / FastMCP is installed) doesn't have
Alexandria installed as a package — it reads the source tree at
the path set on PYTHONPATH. The launcher script alongside this
package (`bin/alexandria-mcp`) sets that PYTHONPATH and execs this
module."""

from .server import mcp


def main():
    # FastMCP picks the transport from the run() argument. stdio is
    # what Claude CLI / Desktop expect; no other transports for v0.
    mcp.run("stdio")


if __name__ == "__main__":
    main()
