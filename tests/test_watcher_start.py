"""LibraryWatcher.start() must create a missing library root and
watch it — a deleted/fresh library is a normal state (the user
starts clean; the browser extension then recreates the directory),
and a watcher that silently declines to start means nothing ever
imports. No display needed: Gio file monitors work headless.

Runnable as `python3 -m tests.test_watcher_start` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import watcher as watcher_mod


def test_start_creates_missing_root(tmp_path):
    root = str(tmp_path / "not-yet" / "Alexandria")
    w = watcher_mod.LibraryWatcher(str(tmp_path / "db.sqlite3"), root)
    try:
        w.start()
        assert os.path.isdir(root), "start() should create the root"
        assert w.monitor is not None, "monitor should be running"
    finally:
        w.stop()


def test_start_on_existing_root_still_works(tmp_path):
    root = str(tmp_path)
    w = watcher_mod.LibraryWatcher(str(tmp_path / "db.sqlite3"), root)
    try:
        w.start()
        assert w.monitor is not None
    finally:
        w.stop()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
