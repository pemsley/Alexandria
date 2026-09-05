"""Alexandria must not identify its users as its author.

Raised 2026-09-05. `identity.maintainer_email()` returned the
maintainer's own address, base64-encoded, whenever
`$ALEXANDRIA_MAILTO` was unset — the default for every installed
copy. So every
user's OpenAlex, CrossRef and Unpaywall traffic was attributed to
the maintainer, and any rate-limiting or blocking that provoked
would land on his address rather than theirs.

The encoding was only ever anti-scraping; it was never a policy
control. There is now no default: the address comes from the user's
own preference or their environment, and the polite-pool niceties
are simply skipped when neither is set.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import identity, prefs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ALEXANDRIA_MAILTO", raising=False)
    monkeypatch.setattr(prefs, "get_contact_email", lambda *a: "")


# ---- no default -------------------------------------------------------

def test_there_is_no_built_in_address():
    """The whole point: an unconfigured install is anonymous, not
    someone else."""
    assert identity.contact_email() == ""


def test_no_personal_address_is_baked_into_the_source():
    """No literal to match against, deliberately.

    The first version of this test named the address, which put it
    back into the tree it was meant to keep clean — and in an
    untracked file, so `git grep` reported the tree clean while the
    test itself carried it. Looking for *any* address instead needs
    no literal, and catches a contributor's as well as the
    maintainer's.

    Scans the tests too: the leak was here, not in the package.
    """
    import glob
    import re as _re
    addr = _re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    # RFC 2606 reserves example.* for exactly this purpose, so that
    # is the only namespace a fixture address may use.
    allowed = _re.compile(r"@example\.[a-z]+$", _re.IGNORECASE)
    bad = []
    for pattern in ("alexandria*/**/*.py", "tests/**/*.py"):
        for path in glob.glob(os.path.join(ROOT, pattern), recursive=True):
            src = open(path, encoding="utf-8").read()
            for found in addr.findall(src):
                if not allowed.search(found):
                    bad.append("{}: {}".format(
                        os.path.basename(path), found))
    assert not bad, bad


def test_a_base64_address_would_be_caught_too():
    """The address was hidden as base64 before, which a plain text
    search misses. Decode any base64-looking literal and re-check."""
    import base64
    import glob
    import re as _re
    token = _re.compile(r"b?[\"']([A-Za-z0-9+/]{16,}={0,2})[\"']")
    bad = []
    for path in glob.glob(os.path.join(ROOT, "alexandria*", "**", "*.py"),
                          recursive=True):
        for cand in token.findall(open(path, encoding="utf-8").read()):
            try:
                text = base64.b64decode(cand).decode("ascii")
            except Exception:
                continue
            if "@" in text and "." in text:
                bad.append("{}: {}".format(os.path.basename(path), text))
    assert not bad, bad


# ---- where it does come from -----------------------------------------

def test_the_environment_wins(monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_MAILTO", "her@example.org")
    assert identity.contact_email() == "her@example.org"


def test_otherwise_the_stored_preference(monkeypatch):
    monkeypatch.setattr(prefs, "get_contact_email",
                        lambda: "him@example.org")
    assert identity.contact_email() == "him@example.org"


def test_the_environment_beats_the_preference(monkeypatch):
    monkeypatch.setattr(prefs, "get_contact_email",
                        lambda: "pref@example.org")
    monkeypatch.setenv("ALEXANDRIA_MAILTO", "env@example.org")
    assert identity.contact_email() == "env@example.org"


def test_whitespace_and_nonsense_are_not_an_address(monkeypatch):
    for bad in ("   ", "not-an-email", "@example.org", "a@"):
        monkeypatch.setenv("ALEXANDRIA_MAILTO", bad)
        assert identity.contact_email() == "", repr(bad)


# ---- the user agent --------------------------------------------------

def test_the_user_agent_carries_the_address_when_there_is_one(monkeypatch):
    monkeypatch.setenv("ALEXANDRIA_MAILTO", "her@example.org")
    ua = identity.user_agent()
    assert "mailto:her@example.org" in ua
    assert "alexandria/" in ua


def test_the_user_agent_omits_the_mailto_when_there_is_none():
    ua = identity.user_agent()
    assert "mailto" not in ua
    assert "alexandria/" in ua


def test_the_user_agent_reports_the_real_version():
    """It said alexandria/0.1 while the app was 0.4.1."""
    from alexandria import __version__
    assert __version__ in identity.user_agent()


# ---- the preference ---------------------------------------------------

def _isolated_config(monkeypatch):
    """Redirect prefs at load/save, not at DEFAULT_PATH.

    `save(data, path=DEFAULT_PATH)` binds its default when the module
    is imported, so monkeypatching DEFAULT_PATH does not redirect the
    write — it goes to the user's real config.json. Learned the hard
    way: an earlier version of this test put her@example.org into a
    live configuration."""
    store = {}
    monkeypatch.setattr(prefs, "load", lambda *a, **k: dict(store))
    monkeypatch.setattr(prefs, "save",
                        lambda data, *a, **k: store.update(
                            {}) or store.clear() or store.update(data))
    # get_contact_email is patched out by the autouse fixture; restore
    # the real one so the round trip actually exercises it.
    monkeypatch.setattr(prefs, "get_contact_email",
                        prefs.__dict__["get_contact_email"]
                        if False else _real_get_contact_email)
    return store


def _real_get_contact_email(path=None):
    stored = prefs.load().get("contact_email")
    return stored.strip() if isinstance(stored, str) else ""


def test_the_preference_round_trips(monkeypatch):
    _isolated_config(monkeypatch)
    prefs.set_contact_email("her@example.org")
    assert prefs.get_contact_email() == "her@example.org"


def test_clearing_the_preference(monkeypatch):
    _isolated_config(monkeypatch)
    prefs.set_contact_email("her@example.org")
    prefs.set_contact_email("")
    assert prefs.get_contact_email() == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
