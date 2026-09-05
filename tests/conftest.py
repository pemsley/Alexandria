"""Suite-wide isolation from the developer's real data.

Alexandria keeps two things outside any library, shared by every
catalogue: author photos under `$XDG_DATA_HOME/Alexandria/` and the
authors database under `$XDG_STATE_HOME/Alexandria/`. Both are
resolved from the environment at call time, so redirecting these two
variables is enough to keep a test run out of the real ones.

This is not hypothetical tidiness: before the fixture existed, the
suite wrote `A123.png` into a real author-image store, and any test
calling `index.open_db` would have attached the real trail.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_xdg_dirs(tmp_path_factory, monkeypatch):
    # Its own directory, not a subdirectory of the test's `tmp_path`:
    # tests that assert on the exact contents of `tmp_path` would
    # otherwise see these.
    base = tmp_path_factory.mktemp("xdg")
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME"):
        d = base / var.lower()
        os.makedirs(d, exist_ok=True)
        monkeypatch.setenv(var, str(d))
    yield
