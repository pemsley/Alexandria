"""Maintainer's contact email used in the polite-pool User-Agent for
OpenAlex / CrossRef. Encoded so the source isn't trivially scraped by
GitHub email harvesters."""

import base64
import getpass
import os

# base64-encoded so it doesn't appear verbatim in the source.
_DEFAULT_B64 = b"cGVtc2xleUBnbWFpbC5jb20="


def maintainer_email():
    """Return the contact email. Override via $PDFORG_MAILTO so other
    users / forks don't accidentally identify as me to the polite pool."""
    override = os.environ.get("PDFORG_MAILTO")
    if override:
        return override
    return base64.b64decode(_DEFAULT_B64).decode("ascii")


def comment_author():
    """Display name stamped on highlights / comments. v1: OS username
    (override via $PDFORG_AUTHOR). A future Preferences entry will let
    the user set a friendlier display name."""
    override = os.environ.get("PDFORG_AUTHOR")
    if override:
        return override
    try:
        return getpass.getuser()
    except Exception:
        return "anonymous"
