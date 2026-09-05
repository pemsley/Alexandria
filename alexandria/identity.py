"""Who this copy of Alexandria says it is when it talks to an API.

OpenAlex and CrossRef ask for a contact address so they can get in
touch about unusual traffic, and give politer rate limits to
requests that carry one; Unpaywall requires one outright.

There is deliberately **no built-in address**. Until 2026-09-05 this
module returned the maintainer's own, base64-encoded, whenever
`$ALEXANDRIA_MAILTO` was unset — which is the default for every
installed copy, so every user's traffic was attributed to him and
any rate-limiting it provoked would have landed on his address. The
encoding was anti-scraping, never a policy control.
"""

import getpass
import os
import re

from . import __version__

# Deliberately loose: enough to catch a typo or a stray placeholder,
# not an attempt to validate RFC 5322.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def contact_email():
    """The address to identify with, or '' when the user has not set
    one. `$ALEXANDRIA_MAILTO` first, then the stored preference."""
    from . import prefs
    for candidate in (os.environ.get("ALEXANDRIA_MAILTO"),
                      prefs.get_contact_email()):
        value = (candidate or "").strip()
        if value and _EMAIL_RE.match(value):
            return value
    return ""


def user_agent():
    """User-Agent for outbound API calls, naming the contact address
    when there is one. Without it the request still goes out — it
    just gets common-pool treatment rather than the polite pool."""
    email = contact_email()
    if email:
        return "alexandria/{} (mailto:{})".format(__version__, email)
    return "alexandria/{}".format(__version__)


def maintainer_email():
    """Deprecated alias for `contact_email`, kept so a stale caller
    fails politely rather than with an AttributeError."""
    return contact_email()


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
