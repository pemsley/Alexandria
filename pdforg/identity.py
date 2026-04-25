"""Maintainer's contact email used in the polite-pool User-Agent for
OpenAlex / CrossRef. Encoded so the source isn't trivially scraped by
GitHub email harvesters."""

import base64
import os

# base64-encoded so it doesn't appear verbatim in the source.
_DEFAULT_B64 = b"YWxleGFuZHJpYUBleGFtcGxlLm9yZw=="


def maintainer_email():
    """Return the contact email. Override via $PDFORG_MAILTO so other
    users / forks don't accidentally identify as me to the polite pool."""
    override = os.environ.get("PDFORG_MAILTO")
    if override:
        return override
    return base64.b64decode(_DEFAULT_B64).decode("ascii")
