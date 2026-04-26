"""sidecar dict → CSL JSON converter.

CSL JSON is the lingua franca of citation processors: citeproc-py /
citeproc-js take a `.csl` style file plus a CSL JSON item and emit a
formatted citation. It also imports cleanly into Zotero and a few
other tools.

This module is the data-layer half of the "Cite this paper as…"
feature. The UI half (right-click → Copy as APA) lives elsewhere
and consumes the dicts produced here.

Reference: CSL JSON schema — https://github.com/citation-style-language/schema
"""

import re


# BibTeX type → CSL JSON type. CSL types are an open enum; the values
# below cover what the project actually carries today. Anything else
# falls back to article-journal-when-we-have-a-journal else document.
_CSL_TYPE_FROM_BIBTEX = {
    "article":         "article-journal",
    "inproceedings":   "paper-conference",
    "conference":      "paper-conference",
    "incollection":    "chapter",
    "inbook":          "chapter",
    "book":            "book",
    "phdthesis":       "thesis",
    "mastersthesis":   "thesis",
    "techreport":      "report",
    "manual":          "document",
    "misc":            "document",
    "unpublished":     "manuscript",
}


def _csl_type_for(rec):
    bt = (rec.get("bibtex_type") or "").strip().lower()
    if bt in _CSL_TYPE_FROM_BIBTEX:
        return _CSL_TYPE_FROM_BIBTEX[bt]
    return "article-journal" if rec.get("journal") else "document"


def _split_author(name):
    """Turn a display-form author string into a CSL name object.

    CSL names are `{family, given}` dicts (with optional
    `non-dropping-particle`, `dropping-particle`, `suffix`, or a
    `literal` for unparseable names). We use the same heuristic as
    `bibtex_export` and `ris_export` — last whitespace-separated
    token is surname — which misorders compound surnames like
    'van der Waals'. Names already in 'Family, Given' form pass
    through. Strings with no whitespace become `{literal: name}`."""
    if not name:
        return None
    name = name.strip()
    if "," in name:
        family, _, given = name.partition(",")
        family = family.strip()
        given = given.strip()
        if family:
            d = {"family": family}
            if given:
                d["given"] = given
            return d
    parts = name.split()
    if len(parts) <= 1:
        return {"literal": name}
    surname = parts[-1]
    given = " ".join(parts[:-1])
    return {"family": surname, "given": given}


def _normalise_pages(pages):
    """Normalise a 'pages' value to CSL's single 'page' field. en/em
    dashes become plain hyphens so style files render consistently."""
    if not pages:
        return None
    s = str(pages).strip()
    if not s:
        return None
    return re.sub(r"[–—]", "-", s)


def sidecar_to_csl(rec, item_id=None):
    """Convert a sidecar dict to a single CSL JSON item dict.

    `item_id` is the CSL item identifier — needs to be unique within
    a citeproc run when feeding multiple items at once. We default to
    the DOI, falling back to the bibtex_key, falling back to a fixed
    string. For single-item "Cite this paper as…" use the default is
    fine."""
    csl = {}
    csl["id"] = (item_id or rec.get("doi") or rec.get("bibtex_key")
                 or "item")
    csl["type"] = _csl_type_for(rec)

    if rec.get("title"):
        csl["title"] = rec["title"]

    authors = []
    for a in (rec.get("authors") or []):
        n = _split_author(a)
        if n:
            authors.append(n)
    if authors:
        csl["author"] = authors

    if rec.get("year") is not None:
        # CSL date format: {date-parts: [[YYYY, MM?, DD?]]}. We only
        # carry year on most sidecars, so a single-element inner list
        # is what we emit. citeproc accepts year-only happily.
        try:
            csl["issued"] = {"date-parts": [[int(rec["year"])]]}
        except (TypeError, ValueError):
            pass

    if rec.get("journal"):
        # `container-title` is the umbrella for "what the work appeared
        # in" — journal name for articles, book title for chapters,
        # conference proceedings for papers, etc. Good default for our
        # data, since we only carry the one field.
        csl["container-title"] = rec["journal"]

    if rec.get("doi"):
        csl["DOI"] = rec["doi"]

    extra = rec.get("bibtex_extra") or {}
    if extra.get("volume"):
        csl["volume"] = str(extra["volume"])
    issue = extra.get("number") or extra.get("issue")
    if issue:
        csl["issue"] = str(issue)
    page = _normalise_pages(extra.get("pages"))
    if page:
        csl["page"] = page
    if extra.get("publisher"):
        csl["publisher"] = str(extra["publisher"])
    pub_place = extra.get("address") or extra.get("publisher-place")
    if pub_place:
        csl["publisher-place"] = str(pub_place)
    if extra.get("url"):
        csl["URL"] = str(extra["url"])

    abs_text = rec.get("abstract") or extra.get("abstract")
    if abs_text:
        csl["abstract"] = abs_text

    # Keywords: CSL uses a single `keyword` string, comma-separated.
    # Prefer a BibTeX-extra "keywords" entry when present; fall back
    # to OpenAlex auto_keywords.
    kws = []
    kw_field = extra.get("keywords")
    if isinstance(kw_field, str):
        kws = [k.strip() for k in re.split(r"[,;]", kw_field) if k.strip()]
    elif isinstance(kw_field, list):
        kws = [str(k).strip() for k in kw_field if k]
    elif rec.get("auto_keywords"):
        kws = [str(k).strip() for k in rec["auto_keywords"] if k]
    if kws:
        csl["keyword"] = ", ".join(kws)

    return csl


def sidecar_to_csl_array(rec, item_id=None):
    """Return a CSL JSON *array* with one item. citeproc and most CSL
    consumers expect a top-level array; this is the shape to feed
    them or to write to a `.json` file."""
    return [sidecar_to_csl(rec, item_id=item_id)]
