"""Application-level preferences.

Stored in $XDG_CONFIG_HOME/pdforg/config.json.
The library root can always be overridden by the PDFORG_LIBRARY env var."""

import json
import os

_XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
    os.path.expanduser("~"), ".config")
DEFAULT_PATH = os.path.join(_XDG_CONFIG, "pdforg", "config.json")

_DEFAULT_LIBRARY = os.path.expanduser("~/pdfs")


def load(path=DEFAULT_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {}


def save(data, path=DEFAULT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def get_library_root():
    """Env var > config file > ~/pdfs."""
    env = os.environ.get("PDFORG_LIBRARY")
    if env:
        return env
    stored = load().get("library_root")
    if stored and isinstance(stored, str):
        return stored
    return _DEFAULT_LIBRARY
