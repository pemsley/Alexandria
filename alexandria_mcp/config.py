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


def resolved():
    """`{catalogue, library_root, db_path}`, all from one reading of
    the current catalogue.

    Resolved together on purpose. These were derived independently:
    `library_root` followed the current catalogue while `db_path`
    returned the constant `index.DEFAULT_DB_PATH`, so on any
    catalogue but the default the server read one library's rows and
    resolved the other library's file paths — 178 papers of the
    default catalogue answered against moorhen's directory, with
    write tools enabled. Nothing errored; the answers were simply
    about the wrong library."""
    name = prefs.get_current_catalogue_name()
    root = os.environ.get("ALEXANDRIA_LIBRARY_ROOT")
    if not root:
        cat = prefs.get_catalogue(name)
        root = (cat or {}).get("library_root") or prefs.get_library_root()
    db = os.environ.get("ALEXANDRIA_DB")
    if not db:
        try:
            db = index.db_path_for_catalogue(name)
        except Exception:
            db = index.DEFAULT_DB_PATH
    return {"catalogue": name, "library_root": root, "db_path": db}


def library_root():
    return resolved()["library_root"]


def db_path():
    return resolved()["db_path"]


def readonly():
    """When set, write tools (none yet in v0) refuse to run. Useful
    for handing the server to a colleague to query the library
    safely."""
    return bool(os.environ.get("ALEXANDRIA_READONLY"))
