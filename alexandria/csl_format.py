"""Render a sidecar dict as a formatted citation, in any vendored
CSL style. Thin wrapper over `citeproc-py`.

Public surface:

    list_styles()  -> [{key, label, path}, ...]   for menu wiring
    format_citation(rec, style_key, mode="bibliography")  -> str

`mode`:
  * "bibliography" — the long form that goes in a References list
    (`Smith, J., Doe, A. (2024). Title. Journal, 12(3), 45-67.`)
  * "citation" — the short in-text marker (`(Smith & Doe, 2024)`,
    `[1]`, etc., depending on the style)

The vendored `.csl` files live in `alexandria/styles/` (CC-BY-SA 3.0,
from github.com/citation-style-language/styles). Users will be
able to drop additional `.csl` files into `~/.config/Alexandria/
styles/` later (not yet implemented)."""

import os

from citeproc import (CitationStylesStyle, CitationStylesBibliography,
                      Citation, CitationItem, formatter)
from citeproc.source.json import CiteProcJSON

from . import csl


# What the menu shows for each vendored style. Key matches the file
# stem in alexandria/styles/<key>.csl. Order is intentional (most-asked
# first).
_STYLES = [
    {"key": "apa",                 "label": "APA"},
    {"key": "vancouver",           "label": "Vancouver"},
    {"key": "nature",              "label": "Nature"},
    {"key": "chicago-author-date", "label": "Chicago (author-date)"},
]


def _styles_dir():
    return os.path.join(os.path.dirname(__file__), "styles")


def list_styles():
    """Return the available styles as a list of {key, label, path}
    dicts. Drops entries whose .csl file is missing."""
    out = []
    for s in _STYLES:
        p = os.path.join(_styles_dir(), s["key"] + ".csl")
        if os.path.isfile(p):
            out.append({"key": s["key"], "label": s["label"], "path": p})
    return out


def format_citation(rec, style_key, mode="bibliography"):
    """Render a sidecar `rec` as a formatted citation string in the
    style identified by `style_key` (one of the keys returned by
    `list_styles`).

    `mode="bibliography"` returns the long form (default).
    `mode="citation"` returns the in-text marker."""
    style_path = os.path.join(_styles_dir(), style_key + ".csl")
    if not os.path.isfile(style_path):
        raise FileNotFoundError(
            "Unknown style {!r} (no {}.csl in {})".format(
                style_key, style_key, _styles_dir()))

    item = csl.sidecar_to_csl(rec)
    item_id = item["id"]

    # citeproc takes the CSL data via CiteProcJSON; the bibliography
    # is then a small in-memory document that we register one citation
    # against, then render.
    bib_source = CiteProcJSON([item])
    style = CitationStylesStyle(style_path, validate=False)
    plain = formatter.plain
    bib = CitationStylesBibliography(style, bib_source, plain)
    cite = Citation([CitationItem(item_id)])
    bib.register(cite)

    if mode == "citation":
        # cite() returns a list of formatted citations — one per
        # registered Citation. We have exactly one.
        out = bib.cite(cite, lambda *a, **k: None)
        return str(out)

    # Default — bibliography form.
    rendered = bib.bibliography()
    if not rendered:
        return ""
    # Each entry is a list of inline pieces; str() collapses them.
    return str(rendered[0])
