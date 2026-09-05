"""The MCP server must serve one catalogue, not halves of two.

Found 2026-09-04 by asking the running server where it was pointed:

    library_root: .../Documents/Alexandria/moorhen
    db_path:      .../state/Alexandria/library.e0a8.db   (178 papers)

but moorhen's database is .../state/Alexandria/moorhen/library.e0a8.db
and holds 81. So it was reading the *default* catalogue's rows while
resolving sidecar and PDF paths against the *moorhen* library root.

`library_root()` went through `prefs.get_library_root()`, which
follows the current catalogue. `db_path()` returned
`index.DEFAULT_DB_PATH`, a constant that always means the default
catalogue. The two agreed only when the current catalogue happened
to be the default. The server is not read-only, so a write tool
would have written into the wrong library.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

config = pytest.importorskip("alexandria_mcp.config")
from alexandria import index, prefs


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for v in ("ALEXANDRIA_LIBRARY_ROOT", "ALEXANDRIA_DB",
              "ALEXANDRIA_LIBRARY", "ALEXANDRIA_READONLY"):
        monkeypatch.delenv(v, raising=False)


def _catalogue(monkeypatch, name, root):
    monkeypatch.setattr(prefs, "get_current_catalogue_name",
                        lambda: name)
    monkeypatch.setattr(prefs, "get_catalogue",
                        lambda n: {"name": n, "library_root": root}
                        if n == name else None)


# ---- the bug ---------------------------------------------------------

def test_a_named_catalogue_gets_its_own_database(monkeypatch):
    _catalogue(monkeypatch, "moorhen", "/lib/moorhen")
    assert config.db_path() == index.db_path_for_catalogue("moorhen")
    assert config.db_path() != index.DEFAULT_DB_PATH


def test_root_and_database_name_the_same_catalogue(monkeypatch):
    """The invariant the mismatch broke. Reading one catalogue's rows
    while resolving another's paths gives plausible-looking wrong
    answers rather than an error."""
    _catalogue(monkeypatch, "moorhen", "/lib/moorhen")
    where = config.resolved()
    assert where["catalogue"] == "moorhen"
    assert where["library_root"] == "/lib/moorhen"
    assert where["db_path"] == index.db_path_for_catalogue("moorhen")


def test_the_default_catalogue_keeps_the_legacy_location(monkeypatch):
    """Existing single-catalogue libraries must not move."""
    _catalogue(monkeypatch, "default", "/lib/default")
    assert config.db_path() == index.DEFAULT_DB_PATH


# ---- the env vars the embedded terminal sets -------------------------

def test_an_explicit_database_still_wins(monkeypatch):
    _catalogue(monkeypatch, "moorhen", "/lib/moorhen")
    monkeypatch.setenv("ALEXANDRIA_DB", "/tmp/explicit.db")
    assert config.db_path() == "/tmp/explicit.db"


def test_an_explicit_root_still_wins(monkeypatch):
    _catalogue(monkeypatch, "moorhen", "/lib/moorhen")
    monkeypatch.setenv("ALEXANDRIA_LIBRARY_ROOT", "/tmp/explicit")
    assert config.library_root() == "/tmp/explicit"


def test_an_unknown_catalogue_falls_back_rather_than_failing(monkeypatch):
    monkeypatch.setattr(prefs, "get_current_catalogue_name",
                        lambda: "gone")
    monkeypatch.setattr(prefs, "get_catalogue", lambda n: None)
    assert config.db_path()
    assert config.library_root()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
