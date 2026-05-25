"""Funder name + award ID → registry URL.

A small dispatch table that knows where each funder publishes
their grant records. For matches, builds a deep link straight to
the award; for misses, falls back to a DuckDuckGo search for the
award ID plus funder name (almost always a one-click landing).

Match strategy: case-insensitive substring against the lowercased
funder display name. First match in `_DISPATCH` wins, so order
the table specific-first. Funder names come straight from
OpenAlex, so they're the long official forms ("Biotechnology and
Biological Sciences Research Council", not "BBSRC")."""

import urllib.parse


# Registry URL templates. {award_id} gets the URL-encoded award ID.
_GTR     = "https://gtr.ukri.org/search/project?term={award_id}"
_NIH     = "https://reporter.nih.gov/search/results?projectNumbers={award_id}"
_NSF     = "https://www.nsf.gov/awardsearch/showAward?AWD_ID={award_id}"
_OSTI    = "https://www.osti.gov/search/semantic:{award_id}"
_CORDIS  = "https://cordis.europa.eu/search?q='{award_id}'"

# Funder-name pattern → URL template. Order matters: list more
# specific names first so e.g. "National Institute of General
# Medical Sciences" matches NIH before any "national institute"
# generic catches it.
_DISPATCH = [
    # ---- UKRI family + UK affiliates → Gateway to Research --------
    ("biotechnology and biological sciences research council", _GTR),
    ("natural environment research council",                   _GTR),
    ("engineering and physical sciences research council",     _GTR),
    ("medical research council",                               _GTR),
    ("science and technology facilities council",              _GTR),
    ("arts and humanities research council",                   _GTR),
    ("economic and social research council",                   _GTR),
    ("innovate uk",                                            _GTR),
    ("ukri",                                                   _GTR),
    ("wellcome trust",                                         _GTR),
    ("wellcome",                                               _GTR),
    ("royal society",                                          _GTR),
    ("cancer research uk",                                     _GTR),
    ("british heart foundation",                               _GTR),
    # ---- US NIH and its institutes → NIH RePORTER ----------------
    ("national institutes of health",                          _NIH),
    ("national institute of general medical sciences",         _NIH),
    ("national institute of allergy and infectious diseases",  _NIH),
    ("national cancer institute",                              _NIH),
    ("national heart, lung, and blood institute",              _NIH),
    ("national institute of diabetes",                         _NIH),
    ("national institute of",                                  _NIH),  # generic catch
    # ---- US NSF → NSF Award Search -------------------------------
    ("national science foundation",                            _NSF),
    # ---- US DoE + national labs → OSTI search --------------------
    ("u.s. department of energy",                              _OSTI),
    ("department of energy",                                   _OSTI),
    ("office of science",                                      _OSTI),
    ("argonne national laboratory",                            _OSTI),
    ("lawrence berkeley national laboratory",                  _OSTI),
    ("oak ridge national laboratory",                          _OSTI),
    ("brookhaven national laboratory",                         _OSTI),
    # ---- EU / EC → CORDIS ----------------------------------------
    ("european research council",                              _CORDIS),
    ("european commission",                                    _CORDIS),
    ("horizon europe",                                         _CORDIS),
    ("horizon 2020",                                           _CORDIS),
]


def funding_url(funder, award_id):
    """Return a clickable URL for `(funder, award_id)`, or a
    DuckDuckGo search URL as the universal fallback. Returns None
    only when both funder and award_id are empty/None — i.e. when
    there is nothing to search for at all."""
    fname = (funder or "").strip()
    aid = (award_id or "").strip()
    if not (fname or aid):
        return None
    low = fname.lower()
    for pattern, template in _DISPATCH:
        if pattern in low and aid:
            return template.format(award_id=urllib.parse.quote(aid, safe=""))
    # Fallback: DuckDuckGo search for the award ID + funder. Quoting
    # both terms forces a literal-string match, which usually lands
    # on the canonical project page.
    parts = []
    if aid:
        parts.append('"{}"'.format(aid))
    if fname:
        parts.append('"{}"'.format(fname))
    q = " ".join(parts)
    return "https://duckduckgo.com/?q=" + urllib.parse.quote(q, safe="")
