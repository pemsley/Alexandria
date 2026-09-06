#!/usr/bin/env python3
"""Show exactly what Unpaywall says about a DOI, and what we keep.

`pdf_fetch` reports "Unpaywall: nothing" for two quite different
reasons — the request failed, or it succeeded and carried no
downloadable PDF — and the progress line cannot tell them apart.
This prints the raw exchange so it is obvious which.

    tools/unpaywall-check.py 10.1107/S2059798325001251 [more DOIs...]
    tools/unpaywall-check.py --library [N]   # first N DOIs in the library

The contact address is read the way the app reads it and is shown
redacted; it is sent to Unpaywall because their API requires it.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alexandria import identity, metrics, pdf_fetch, prefs  # noqa: E402


def redact(email):
    if not email or "@" not in email:
        return "<none>"
    user, _, host = email.partition("@")
    return (user[:1] or "?") + "***@" + host


def raw_lookup(doi, email):
    """The HTTP exchange, unmediated by metrics' error swallowing."""
    url = "{}/{}?email={}".format(
        metrics.UNPAYWALL_BASE,
        urllib.parse.quote(doi, safe=""),
        urllib.parse.quote(email))
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": identity.user_agent(),
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            return (resp.status, body, time.monotonic() - t0, None)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return (e.code, body, time.monotonic() - t0, str(e))
    except Exception as e:
        return (None, "", time.monotonic() - t0, str(e))


def report(doi, email):
    print("=" * 72)
    print(doi)
    status, body, secs, err = raw_lookup(doi, email)
    print("  HTTP {}  in {:.2f}s{}".format(
        status, secs, "  ({})".format(err) if err else ""))
    if status != 200:
        print("  body: " + (body[:300].replace("\n", " ") or "<empty>"))
        return
    data = json.loads(body)
    locs = data.get("oa_locations") or []
    with_pdf = [l for l in locs if l.get("url_for_pdf")]
    print("  is_oa={}  oa_status={}  oa_locations={}  with url_for_pdf={}"
          .format(data.get("is_oa"), data.get("oa_status"),
                  len(locs), len(with_pdf)))
    for l in locs:
        print("    - host={:<11} version={:<18} pdf={}".format(
            str(l.get("host_type")), str(l.get("version")),
            (l.get("url_for_pdf") or "NONE — landing page only")[:44]))
    kept = pdf_fetch._unpaywall_pdf_urls(doi)
    print("  what pdf_fetch keeps: {}".format(len(kept)))
    for u in kept:
        print("    -> " + u[:64])
    if locs and not kept:
        print("  VERDICT: Unpaywall answered, but no location offered a "
              "direct PDF link.")
    elif not locs:
        print("  VERDICT: Unpaywall has no OA location for this DOI "
              "(is_oa={}).".format(data.get("is_oa")))
    else:
        print("  VERDICT: usable PDF URL(s) found.")


def library_dois(limit):
    from alexandria import index
    out = []
    for cat in [c["name"] for c in prefs.get_catalogues()]:
        conn = index.open_db(index.db_path_for_catalogue(cat))
        for r in conn.execute(
                "SELECT doi FROM papers WHERE doi IS NOT NULL"):
            if r["doi"] and r["doi"] not in out:
                out.append(r["doi"])
            if len(out) >= limit:
                return out
    return out


def main(argv):
    email = identity.contact_email()
    print("contact email: {}   (user-agent: {})".format(
        redact(email), identity.user_agent()))
    if not email:
        print("\nNo contact address configured, so the app skips "
              "Unpaywall entirely.\nSet `contact_email` in "
              "~/.config/Alexandria/config.json, or use\n"
              "Preferences → Online services.")
        return 1
    args = argv[1:] or ["--library", "10"]
    if args[0] == "--library":
        n = int(args[1]) if len(args) > 1 else 10
        dois = library_dois(n)
    else:
        dois = args
    for doi in dois:
        report(doi, email)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
