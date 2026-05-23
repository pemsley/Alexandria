"""Application-level preferences.

Stored in $XDG_CONFIG_HOME/Alexandria/config.json.
The library root can always be overridden by the ALEXANDRIA_LIBRARY env var."""

import json
import os

_XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
    os.path.expanduser("~"), ".config")
DEFAULT_PATH = os.path.join(_XDG_CONFIG, "Alexandria", "config.json")


def _default_library():
    """Default library root for fresh installs.

    Uses `$XDG_DOCUMENTS_DIR` (per the user's locale and any custom
    user-dirs.dirs setup) so this works under a Flatpak with only
    `--filesystem=xdg-documents` granted. The folder name is
    `Alexandria` — explicit ownership is clearer than a generic
    `Papers` name when the user opens their Documents folder."""
    try:
        from gi.repository import GLib
        docs = GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_DOCUMENTS)
    except Exception:
        docs = None
    if not docs:
        docs = os.path.expanduser("~/Documents")
    return os.path.join(docs, "Alexandria")


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
    """Env var > stored config > XDG-Documents/Alexandria."""
    env = os.environ.get("ALEXANDRIA_LIBRARY")
    if env:
        return env
    stored = load().get("library_root")
    if stored and isinstance(stored, str):
        return stored
    return _default_library()


def get_coot_path():
    """Path to the `coot` executable for the right-click 'Open in
    Coot' action on PDB chips. Stored config > $COOT env var > None
    (caller falls back to plain 'coot' on PATH).

    Set in ~/.config/Alexandria/config.json as e.g.
        "coot_path": "/home/paule/precious/.../bin/coot"
    """
    stored = load().get("coot_path")
    if stored and isinstance(stored, str):
        return stored
    env = os.environ.get("COOT")
    if env:
        return env
    return None
