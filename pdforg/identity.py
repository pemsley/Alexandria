"""Maintainer's contact email used in the polite-pool User-Agent for
OpenAlex / CrossRef. Encoded so the source isn't trivially scraped by
GitHub email harvesters."""

import base64
import getpass
import os

# base64-encoded so it doesn't appear verbatim in the source.
_DEFAULT_B64 = b"YWxleGFuZHJpYUBleGFtcGxlLm9yZw=="


def maintainer_email():
    """Return the contact email. Override via $ALEXANDRIA_MAILTO so other
    users / forks don't accidentally identify as me to the polite pool."""
    override = os.environ.get("ALEXANDRIA_MAILTO")
    if override:
        return override
    return base64.b64decode(_DEFAULT_B64).decode("ascii")


def comment_author():
    """Display name stamped on highlights / comments.

    Precedence: $ALEXANDRIA_AUTHOR env var > stored Preferences value
    (`comment_author` key in config.json) > OS username >
    'anonymous'. Same env-var-first convention as
    `prefs.get_library_root`.

    Existing comments are not retroactively rewritten when this
    setting changes — only newly-created and newly-edited comments
    use the new value."""
    override = os.environ.get("ALEXANDRIA_AUTHOR")
    if override:
        return override
    try:
        from . import prefs
        stored = (prefs.load().get("comment_author") or "").strip()
        if stored:
            return stored
    except Exception:
        pass
    try:
        return getpass.getuser()
    except Exception:
        return "anonymous"
