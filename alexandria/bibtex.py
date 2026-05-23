"""BibTeX parse / write at the data layer (no GUI).

A `record` here is a Python dict shaped like:

    {
        "bibtex_key":   "smith2024foo",
        "bibtex_type":  "article",
        "title":        "A Pithy Title",
        "authors":      ["Jane Smith", "John Doe"],   # display-form
        "year":         2024,
        "journal":      "Journal of Things",
        "doi":          "10.1234/abc.5678",
        "file":         "/abs/path/to/foo.pdf",       # or None
        "bibtex_extra": {"volume": "5", "pages": "123-130", ...},
    }

`parse(text_or_path)` turns BibTeX into a list of records.
`write(records)` turns records back into BibTeX text.

Known v1 lossy behaviour:

* Inner braces in field values (case protection like `{ATP}`) are
  stripped on parse and not re-added on write.
* Author names are split on " and " then converted to display order
  (`Smith, Jane` → `Jane Smith`); the conversion is a "split on the
  first comma" heuristic and may misorder compound surnames such as
  `van der Waals, Johannes`.
* Multi-line field values are collapsed to single spaces.
* Comments / `@preamble{...}` / `@string{...}` are dropped.

`parse(write(records)) == records` is the round-trip invariant we
rely on; the *literal* output text will usually differ from the input
because of the cleanups above.
"""

import os
import re

import bibtexparser
from bibtexparser import middlewares as bm


# Field keys whose value we lift onto the top level of a record;
# everything else lands in `bibtex_extra`.
_PROMOTED_KEYS = ("title", "author", "year", "journal", "doi", "file")


def _strip_outer_quotes(s):
    if len(s) >= 2 and s[0] == "{" and s[-1] == "}":
        return s[1:-1]
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_WS_RE = re.compile(r"\s+")


def _clean_value(raw):
    """Strip outer delimiters, drop internal braces, normalise
    whitespace. The result is a plain string suitable for storage."""
    if raw is None:
        return None
    s = _strip_outer_quotes(raw.strip())
    # Iteratively peel inner brace groups (handles nested braces).
    prev = None
    while prev != s:
        prev = s
        s = _BRACE_RE.sub(r"\1", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _parse_year(value):
    if not value:
        return None
    m = re.search(r"\d{4}", value)
    return int(m.group(0)) if m else None


def _split_authors(value):
    """Split a BibTeX `author` field on ` and ` (case-insensitive,
    word-bounded). Each name is converted from `Surname, First M.`
    to display form `First M. Surname`. Returns [] when empty."""
    if not value:
        return []
    parts = re.split(r"\s+\band\b\s+", value, flags=re.IGNORECASE)
    return [_lastfirst_to_display(p.strip()) for p in parts if p.strip()]


def _lastfirst_to_display(name):
    """Convert `Surname, First M.` to `First M. Surname`. Names with
    no comma are returned unchanged. Names with a leading literal
    comma (`{Smith, Jr.}, John` style) aren't fully handled and are
    left as-is — those are rare and round-tripping would need lvonl
    structure preservation."""
    if "," not in name:
        return name
    last, first = name.split(",", 1)
    last = last.strip()
    first = first.strip()
    if not first:
        return last
    return "{} {}".format(first, last)


def _display_to_lastfirst(name):
    """Inverse of `_lastfirst_to_display`. The heuristic is "last
    whitespace token is the surname"; it covers `Jane Smith` →
    `Smith, Jane` but not e.g. `Johannes van der Waals`."""
    if not name:
        return name
    parts = name.strip().split()
    if len(parts) < 2:
        return name
    return "{}, {}".format(parts[-1], " ".join(parts[:-1]))


def _normalise_file_field(raw):
    """JabRef / Zotero often write `file = {:path:pdf}` (description-
    less prefix and trailing type tag). Extract just the path.
    Multi-file fields (separated by `;`) keep only the first PDF."""
    if not raw:
        return None
    candidates = raw.split(";")
    for c in candidates:
        # JabRef syntax:  description:path:filetype
        bits = c.split(":")
        if len(bits) >= 2:
            # Sometimes leading colon means empty description.
            for b in bits[1:]:
                b = b.strip()
                if b.lower().endswith(".pdf"):
                    return b
        c = c.strip()
        if c.lower().endswith(".pdf"):
            return c
    # Fallback: return the first cleaned segment.
    first = candidates[0].strip()
    return first or None


def _record_from_entry(entry):
    """Build a sidecar-style record dict from a bibtexparser v2
    Entry object."""
    raw = {f.key.lower(): f.value for f in entry.fields}

    def clean(v):
        return _strip_latex_commands(_clean_value(v))

    rec = {
        "bibtex_key": entry.key,
        "bibtex_type": entry.entry_type,
        "title": clean(raw.get("title")),
        "authors": _split_authors(clean(raw.get("author")) or ""),
        "year": _parse_year(clean(raw.get("year"))),
        "journal": clean(raw.get("journal") or raw.get("booktitle")),
        "doi": clean(raw.get("doi")),
        "file": _normalise_file_field(clean(raw.get("file"))),
        "bibtex_extra": {},
    }
    for k, v in raw.items():
        if k in _PROMOTED_KEYS or k == "booktitle":
            continue
        cleaned = clean(v)
        if cleaned is not None and cleaned != "":
            rec["bibtex_extra"][k] = cleaned
    return rec


_PARSE_MIDDLEWARES = [
    bm.LatexDecodingMiddleware(),     # \"o → ö, \&  → &, \ldots → …, ...
    bm.NormalizeFieldKeys(),          # Title → title, AUTHOR → author
]

# Cheap LaTeX-command stripper for things LatexDecodingMiddleware doesn't
# touch: font commands like `\it Coot`, `\emph{X}`, `\textit{X}`. We don't
# render typography in the sidecar, so just drop the command and keep the
# text. (Full LaTeX rendering is out of scope.)
_LATEX_CMD_BRACED_RE = re.compile(
    r"\\(?:emph|textit|textbf|textsl|textsc|texttt|textrm|textsf|mathrm|mathit|mathbf|mathsf|mathtt)\s*\{([^{}]*)\}")
_LATEX_CMD_INLINE_RE = re.compile(
    r"\\(?:it|sl|bf|tt|rm|sf|sc|em|emph)\b\s*")


def _strip_latex_commands(s):
    if not s:
        return s
    prev = None
    while prev != s:
        prev = s
        s = _LATEX_CMD_BRACED_RE.sub(r"\1", s)
    return _LATEX_CMD_INLINE_RE.sub("", s)


def _record_from_failed_block(block):
    """Salvage an entry that was rejected for having duplicate field
    keys (e.g. two `url = {...}` lines). We keep the first occurrence
    of each field. Returns a record dict, or None if we can't even
    extract the @type{key,...}."""
    raw = getattr(block, "raw", None) or ""
    head = re.match(r"\s*@(\w+)\s*\{\s*([^,\s]+)\s*,",
                    raw, re.DOTALL)
    if not head:
        return None
    entry_type = head.group(1).lower()
    entry_key = head.group(2)
    body = raw[head.end():]
    # Strip a trailing closing brace if present.
    body = body.rstrip().rstrip("}")
    # Walk fields. A field is `name = value` where `value` is a
    # brace-balanced `{...}` or quoted string. This is a small parser
    # rather than a regex because field values can contain commas.
    fields = {}
    i, n = 0, len(body)
    while i < n:
        # Skip whitespace and stray commas.
        while i < n and body[i] in " \t\n\r,":
            i += 1
        if i >= n:
            break
        m = re.match(r"([A-Za-z_][\w-]*)\s*=\s*", body[i:])
        if not m:
            break
        key = m.group(1).lower()
        i += m.end()
        # Capture the value: either {...} (brace-balanced), "..." (string),
        # or bare token.
        if i < n and body[i] == "{":
            depth = 0
            j = i
            while j < n:
                c = body[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            value = body[i + 1:j - 1]
            i = j
        elif i < n and body[i] == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 1
            value = body[i + 1:j]
            i = j + 1
        else:
            j = i
            while j < n and body[j] not in ",\n":
                j += 1
            value = body[i:j].strip()
            i = j
        if key not in fields:
            fields[key] = value

    raw_to_clean = lambda v: _strip_latex_commands(_clean_value(v))
    rec = {
        "bibtex_key": entry_key,
        "bibtex_type": entry_type,
        "title": raw_to_clean(fields.get("title")),
        "authors": _split_authors(raw_to_clean(fields.get("author")) or ""),
        "year": _parse_year(raw_to_clean(fields.get("year"))),
        "journal": raw_to_clean(fields.get("journal")
                                or fields.get("booktitle")),
        "doi": raw_to_clean(fields.get("doi")),
        "file": _normalise_file_field(raw_to_clean(fields.get("file"))),
        "bibtex_extra": {},
    }
    for k, v in fields.items():
        if k in _PROMOTED_KEYS or k == "booktitle":
            continue
        cleaned = raw_to_clean(v)
        if cleaned:
            rec["bibtex_extra"][k] = cleaned
    return rec


def parse(text_or_path):
    """Parse a BibTeX string or a path to a `.bib` file. Returns a
    list of records (see module docstring for the schema). Entries
    rejected by the strict parser (e.g. duplicate field keys) are
    salvaged from `failed_blocks` with the first value of each
    repeated field kept."""
    if isinstance(text_or_path, (bytes, bytearray)):
        text_or_path = text_or_path.decode("utf-8")
    if isinstance(text_or_path, str) and "\n" not in text_or_path \
            and len(text_or_path) < 4096 and os.path.isfile(text_or_path):
        with open(text_or_path, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = text_or_path
    lib = bibtexparser.parse_string(text, append_middleware=_PARSE_MIDDLEWARES)
    records = [_record_from_entry(e) for e in lib.entries]
    for blk in (lib.failed_blocks or []):
        rec = _record_from_failed_block(blk)
        if rec:
            records.append(rec)
    return records


# ---- Writing -------------------------------------------------------


def _format_value(v):
    """Wrap a value in BibTeX braces. Only escape the bare minimum;
    we trust ourselves not to feed in raw `}` characters."""
    if v is None:
        return "{}"
    s = str(v)
    return "{" + s + "}"


def _record_field_order(rec):
    """Yield (field_name, value) pairs in a stable, readable order."""
    if rec.get("title"):
        yield "title", rec["title"]
    if rec.get("authors"):
        yield "author", " and ".join(
            _display_to_lastfirst(a) for a in rec["authors"])
    if rec.get("year"):
        yield "year", str(rec["year"])
    if rec.get("journal"):
        yield "journal", rec["journal"]
    if rec.get("doi"):
        yield "doi", rec["doi"]
    if rec.get("file"):
        # JabRef-style. Description left empty.
        yield "file", ":{}:pdf".format(rec["file"])
    extras = rec.get("bibtex_extra") or {}
    for k in sorted(extras):
        yield k, extras[k]


def write_record(rec):
    """Render a single record as a BibTeX entry (string)."""
    bk = rec.get("bibtex_key") or "untitled"
    bt = rec.get("bibtex_type") or "misc"
    lines = ["@{}{{{},".format(bt, bk)]
    pairs = list(_record_field_order(rec))
    width = max((len(k) for k, _ in pairs), default=0)
    for i, (k, v) in enumerate(pairs):
        sep = "," if i < len(pairs) - 1 else ""
        lines.append("  {:<{w}} = {}{}".format(
            k, _format_value(v), sep, w=width))
    lines.append("}")
    return "\n".join(lines)


def write(records):
    """Render a list of records to a BibTeX string."""
    return "\n\n".join(write_record(r) for r in records) + "\n"
