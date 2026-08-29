#!/usr/bin/env python3
"""Stand-alone reference-resolution check.

Parses a PDF's bibliography and runs every entry through the *same*
resolution logic the viewer's reference popover uses, printing a
per-reference report so you can see what resolves and what doesn't.

Usage:
    python3 check_refs.py [path-to.pdf]

With no argument it defaults to jumper2021highly.pdf in the current
library root.
"""

import os
import sys
import time

from alexandria import references_pdf, metrics, prefs
from alexandria.viewer import (
    _split_entry_text, _author_surnames, _looks_personal)


def resolve_doi_only(entry):
    """The DOI-finding half of the viewer's resolution path, returning
    (doi, path) so we can see which strategy hit. Mirrors the order in
    viewer.PdfViewerWindow._resolve_reference_blocking; metadata is then
    fetched separately via metrics.resolve_doi, exactly as the popover
    does."""
    doi = entry.get("doi")
    if doi:
        return doi, "entry-doi"
    if entry.get("surname") and entry.get("year"):
        doi = metrics.find_doi_by_author_year(
            entry["surname"], entry["year"], journal=entry.get("journal"))
        if doi:
            return doi, "author-year"
    doi = metrics.find_doi_by_citation(entry.get("text") or "")
    if doi:
        return doi, "crossref-biblio"
    authors_str, year, title, journal = _split_entry_text(
        entry.get("text") or "")
    if title:
        surnames = (_author_surnames(authors_str)
                    if _looks_personal(authors_str) else [])
        doi = metrics.find_doi(
            title, year=year, author_names=surnames, journal=journal or None)
        if doi:
            return doi, "title-search"
    return None, None


def main(argv):
    if len(argv) > 1:
        pdf = argv[1]
    else:
        pdf = os.path.join(prefs.get_library_root(), "jumper2021highly.pdf")
    if not os.path.exists(pdf):
        sys.exit("No such PDF: {}".format(pdf))

    print("PDF:", pdf)
    entries = references_pdf.parse_bibliography(pdf)
    print("Parsed {} bibliography entries\n".format(len(entries)))

    # Fetch each resolved DOI's metadata the SAME way the reference
    # popover does — metrics.resolve_doi (OpenAlex first, Crossref
    # fallback) — so this reproduces exactly what the app shows, including
    # the Crossref fallback that keeps references resolving when OpenAlex
    # is rate-limited. We tag each hit with its source (OA / CR) so you
    # can see when OpenAlex is carrying it vs. when Crossref stepped in.
    ok_oa = ok_cr = miss = 0
    for e in entries:
        n = e.get("n")
        doi, path = resolve_doi_only(e)
        if not doi:
            print("[{:>3}] {:<9} {}".format(
                n, "MISS", (e.get("text") or "")[:70]))
            miss += 1
            continue
        meta = metrics.resolve_doi(doi)
        title = (meta or {}).get("title")
        if not title:
            print("[{:>3}] {:<9} {:<38} (neither OpenAlex nor Crossref "
                  "has it)".format(n, "MISS-DOI", doi))
            miss += 1
            continue
        # openalex_id is None only when the data came from the Crossref
        # fallback — a direct read of "who answered".
        if meta.get("openalex_id"):
            ok_oa += 1
            src = "OK/OA"
        else:
            ok_cr += 1
            src = "OK/CR"
        print("[{:>3}] {:<9} {:<38} {}".format(n, src, doi, title[:48]))

    print("\n{} OK (OpenAlex), {} OK (Crossref fallback), {} MISS  "
          "of {} entries".format(ok_oa, ok_cr, miss, len(entries)))
    print("→ the app's popover shows info for {} of {} references.".format(
        ok_oa + ok_cr, len(entries)))
    if metrics._openalex_paused_until > time.monotonic():
        print("\nNote: OpenAlex is currently rate-limited (over quota), so the "
              "Crossref fallback is doing the work. That's expected and fine — "
              "the popover still resolves.")


if __name__ == "__main__":
    main(sys.argv)
