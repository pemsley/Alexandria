"""Library-wide labels for the four Mark colours.

The user assigns meanings (e.g. "Must Read!", "Harvard group") to the
red / orange / green / cyan circles. Stored in a small JSON file under
$XDG_CONFIG_HOME/Alexandria/marks.json. Per-paper data still lives in
the sidecars; this is purely UI presentation."""

import json
import os

MARK_COLORS = ("red", "orange", "green", "cyan")

_XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
    os.path.expanduser("~"), ".config")
DEFAULT_PATH = os.path.join(_XDG_CONFIG, "Alexandria", "marks.json")


def load(path=DEFAULT_PATH):
    """Return a dict {color: label} with empty defaults filled in for
    every known colour."""
    out = {c: "" for c in MARK_COLORS}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for c in MARK_COLORS:
                v = data.get(c)
                if isinstance(v, str):
                    out[c] = v
    except (FileNotFoundError, ValueError, OSError):
        pass
    return out


def save(labels, path=DEFAULT_PATH):
    """Write the labels dict atomically. Only known colour keys are
    persisted."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    clean = {c: (labels.get(c) or "") for c in MARK_COLORS}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def label_for(color, labels):
    """Return the user-set label for `color`, or "" if none."""
    if not color:
        return ""
    return (labels or {}).get(color, "") or ""


def display_for(color, fallback_name, labels):
    """Combine '<fallback_name>' with the user label, e.g. 'Red — Must Read!'.
    If no label is set, return just the fallback."""
    label = label_for(color, labels)
    if label:
        return "{} — {}".format(fallback_name, label)
    return fallback_name
