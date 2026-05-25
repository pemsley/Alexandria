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
    """Library root for the *current* catalogue (or the legacy
    single-catalogue setup). Env var > current catalogue's stored
    library_root > legacy top-level `library_root` > XDG default.

    Thin wrapper over the catalogue API for callers that only need
    "where do PDFs live right now" without threading a catalogue
    name through."""
    env = os.environ.get("ALEXANDRIA_LIBRARY")
    if env:
        return env
    cat = get_catalogue(get_current_catalogue_name())
    if cat and cat.get("library_root"):
        return cat["library_root"]
    return _default_library()


# ---- Catalogues ----------------------------------------------------
#
# Multi-directory libraries. A catalogue is `{name, library_root}`;
# the config file carries a `catalogues` list and a
# `current_catalogue` pointer to the one last opened.
#
# Upgrade story for pre-catalogue configs (v0.1.0 and earlier):
# they only have a top-level `library_root`. `get_catalogues()`
# synthesises a single catalogue called `default` from that value
# (or the XDG default when that's missing too). The config file is
# not rewritten on read; the synthesised default stays implicit
# until `add_catalogue` is called to save a real list.

_DEFAULT_CATALOGUE = "default"


def _legacy_library_root():
    """Pre-catalogue stored value, used by `get_catalogues` when
    synthesising the implicit default. Doesn't fall through the
    catalogue lookup (which would recurse)."""
    env = os.environ.get("ALEXANDRIA_LIBRARY")
    if env:
        return env
    stored = load().get("library_root")
    if stored and isinstance(stored, str):
        return stored
    return _default_library()


def get_catalogues():
    """Return the list of catalogues, oldest first.

    Always returns at least one entry — synthesises a `default`
    catalogue from the legacy `library_root` config field when no
    `catalogues` list is present."""
    data = load()
    cats = data.get("catalogues")
    if isinstance(cats, list) and cats:
        out = []
        for c in cats:
            if (isinstance(c, dict)
                    and isinstance(c.get("name"), str)
                    and isinstance(c.get("library_root"), str)):
                out.append({"name": c["name"],
                            "library_root": c["library_root"]})
        if out:
            return out
    return [{"name": _DEFAULT_CATALOGUE,
             "library_root": _legacy_library_root()}]


def get_catalogue(name):
    """Return the catalogue dict matching `name`, or None."""
    for c in get_catalogues():
        if c["name"] == name:
            return c
    return None


def get_current_catalogue_name():
    """Name of the catalogue to open by default on launch.
    Falls back to the first catalogue when the stored value
    points at a removed entry."""
    stored = load().get("current_catalogue")
    if isinstance(stored, str) and get_catalogue(stored):
        return stored
    return get_catalogues()[0]["name"]


def set_current_catalogue(name):
    """Persist `name` as the catalogue to open by default next
    launch. No-op when the catalogue doesn't exist."""
    if not get_catalogue(name):
        return
    data = load()
    data["current_catalogue"] = name
    save(data)


def add_catalogue(name, library_root):
    """Add a new catalogue. Returns True on success, False if the
    name is taken / either argument is invalid."""
    if not name or not isinstance(name, str):
        return False
    if not library_root or not isinstance(library_root, str):
        return False
    cats = list(get_catalogues())
    if any(c["name"] == name for c in cats):
        return False
    cats.append({"name": name, "library_root": library_root})
    data = load()
    data["catalogues"] = cats
    save(data)
    return True


def remove_catalogue(name):
    """Remove `name` from the catalogues list. Returns True on
    success, False if the name doesn't exist or removing it would
    leave the list empty (we always keep at least one — the
    synthesised `default` reappears if the list is empty but
    that's still one). Doesn't touch the catalogue's
    `library_root` folder on disk (that's the user's data) or
    its per-catalogue DB cache (regenerable; remove out-of-band
    if you want it gone). Clears `current_catalogue` if it
    pointed at the removed entry."""
    if not name or not isinstance(name, str):
        return False
    cats = list(get_catalogues())
    new_cats = [c for c in cats if c["name"] != name]
    if len(new_cats) == len(cats):
        return False  # not present
    if not new_cats:
        return False  # would leave nothing
    data = load()
    data["catalogues"] = new_cats
    if data.get("current_catalogue") == name:
        data["current_catalogue"] = new_cats[0]["name"]
    save(data)
    return True


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
