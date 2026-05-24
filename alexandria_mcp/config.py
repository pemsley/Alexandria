"""Resolve runtime configuration.

Order of precedence for each setting:
  1. Explicit env var (ALEXANDRIA_*).
  2. The same default Alexandria's GUI uses (via `alexandria.prefs`
     and `alexandria.index`).

The env-var path is the one Alexandria's embedded VTE sets for any
shell it spawns, so launching `claude` from inside Alexandria
automatically points the MCP server at the right library and DB."""

import os

from alexandria import index, prefs


def library_root():
    v = os.environ.get("ALEXANDRIA_LIBRARY_ROOT")
    if v:
        return v
    return prefs.get_library_root()


def db_path():
    v = os.environ.get("ALEXANDRIA_DB")
    if v:
        return v
    return index.DEFAULT_DB_PATH


def readonly():
    """When set, write tools (none yet in v0) refuse to run. Useful
    for handing the server to a colleague to query the library
    safely."""
    return bool(os.environ.get("ALEXANDRIA_READONLY"))
