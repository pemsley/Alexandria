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


def _looks_corporate(name):
    """Whether a name should be written as an unparsed unit.

    A corporate author reaches us either flagged by the parser (from
    `{{…}}` in the source) or typed by hand. The giveaways are a
    legal-form suffix or an organisation word: guessing wrong costs
    only a pair of braces, whereas guessing wrong the other way
    turns "Meta Platforms, Inc." into "Platforms, Inc. Meta"."""
    n = (name or "").strip()
    if not n:
        return False
    low = n.lower()
    return any(w in low for w in (
        " inc", " inc.", " ltd", " llc", " gmbh", " corp", " co.",
        "consortium", "collaboration", "institute", "university",
        "laboratory", "foundation", "society", "committee",
        "organization", "organisation", "group", "project", "team"))


def _display_to_lastfirst(name, corporate=False):
    """Inverse of `_lastfirst_to_display`. The heuristic is "last
    whitespace token is the surname"; it covers `Jane Smith` →
    `Smith, Jane` but not e.g. `Johannes van der Waals`.

    A corporate name is returned brace-wrapped and untouched: that is
    BibTeX for "one name, do not parse this", and without it the next
    reader splits it at the comma exactly as we once did."""
    if not name:
        return name
    if corporate or _looks_corporate(name):
        return "{" + name.strip() + "}"
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
        # Decoded but not yet reordered — `_restore_corporate_authors`
        # needs these, because the raw re-parse it works from has not
        # been through the LaTeX middleware. Removed there.
        "_authors_decoded": [
            p.strip() for p in re.split(
                r"\s+\band\b\s+", clean(raw.get("author")) or "",
                flags=re.IGNORECASE) if p.strip()],
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

# The same, minus the LaTeX decoding — see `_VERBATIM_FIELDS`.
_RAW_MIDDLEWARES = [bm.NormalizeFieldKeys()]

# Fields where LaTeX decoding does harm rather than good. Decoding is
# right for prose (`\"o` → ö) but these are machine-readable strings
# in which LaTeX's special characters are ordinary text: `&` is an
# alignment character to LaTeX, so a URL query string came back with
# its `&` replaced by a space — a broken link. And `--` is an en dash
# to LaTeX, so `pages = {123--130}` became `123–130`, changing 35 of
# the 53 page ranges in a real file on import. Found 2026-09-02.
_VERBATIM_FIELDS = frozenset((
    "url", "doi", "eprint", "file", "isbn", "issn",
    "archiveprefix", "primaryclass", "urldate",
))

# Inside a verbatim field the only LaTeX left worth undoing is the
# backslash escaping of BibTeX's own specials — `a\&b` really does
# mean `a&b`. Everything else stays as written.
_VERBATIM_UNESCAPE_RE = re.compile(r"\\([&%$#_{}])")

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


def _split_at_depth_zero(value):
    """Split a BibTeX name list on ` and ` outside any braces, so a
    corporate name containing the word survives intact."""
    parts, buf, depth = [], [], 0
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if depth == 0 and value[i:i + 5].lower() == " and ":
            parts.append("".join(buf))
            buf = []
            i += 5
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _corporate_flags(raw_author):
    """Which names in a raw `author` value are brace-wrapped.

    `{{Meta Platforms, Inc.}}` is BibTeX for "one name, do not parse
    it". By the time the LaTeX middleware has run the braces are
    gone, and `Meta Platforms, Inc.` then looks like `Surname, First`
    — which is how it came back out as `Platforms, Inc. Meta`."""
    return [p.startswith("{") and p.endswith("}")
            for p in _split_at_depth_zero(raw_author or "")]


def _restore_verbatim_fields(text, records):
    """Put back the undecoded value of every `_VERBATIM_FIELDS` entry.

    Parsing a second time without the LaTeX middleware is cheaper and
    far less fragile than trying to undo its substitutions, which are
    not reversible — a space could have been a `&` or could always
    have been a space."""
    try:
        raw = bibtexparser.parse_string(
            text, append_middleware=_RAW_MIDDLEWARES)
    except Exception:
        return
    by_key = {e.key: e for e in raw.entries}
    for rec in records:
        entry = by_key.get(rec.get("bibtex_key"))
        if entry is None:
            continue
        # Before the extra-field loop: a record can have no
        # bibtex_extra at all and still have a corporate author.
        _restore_corporate_authors(entry, rec)
        extra = rec.get("bibtex_extra")
        if not extra:
            continue
        for name in list(extra):
            if name.lower() not in _VERBATIM_FIELDS:
                continue
            field = entry.fields_dict.get(name)
            if field is not None and field.value is not None:
                extra[name] = _VERBATIM_UNESCAPE_RE.sub(
                    r"\1", _clean_value(field.value))


def _restore_corporate_authors(entry, rec):
    """Undo the Surname-comma-First reordering for names the file
    braced as a unit. Uses the raw field to decide which, and the
    decoded names for the text, falling back to leaving them alone
    when the two cannot be lined up — which happens only for a
    corporate name that itself contains " and "."""
    field = entry.fields_dict.get("author")
    names = rec.get("authors") or []
    if field is None or not names:
        return
    flags = _corporate_flags(field.value)
    if len(flags) != len(names) or not any(flags):
        return
    # The decoded, un-reordered names, stashed by
    # `_record_from_entry`: the entry we hold here came from the raw
    # re-parse and so has not been through the LaTeX middleware.
    decoded = rec.get("_authors_decoded") or []
    if len(decoded) != len(flags):
        return
    out = []
    for is_corp, name, dec in zip(flags, names, decoded):
        out.append(_clean_value(dec) if is_corp else name)
    rec["authors"] = out
    rec["corporate_authors"] = [n for f, n in zip(flags, out) if f]


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
    _restore_verbatim_fields(text, records)
    seen = {r.get("bibtex_key") for r in records if r.get("bibtex_key")}
    for blk in (lib.failed_blocks or []):
        rec = _reparse_failed_block(blk, seen)
        if rec is None:
            rec = _record_from_failed_block(blk)
        if rec:
            key = rec.get("bibtex_key")
            if key:
                seen.add(key)
            records.append(rec)
    # Drop the scratch key after the failed blocks too, or a salvaged
    # record carries it into the sidecar.
    for r in records:
        r.pop("_authors_decoded", None)
    return records


def _unique_key(key, taken):
    """`react` → `react_2`, matching `bibtex_export._dedup_key`."""
    if key not in taken:
        return key
    n = 2
    while "{}_{}".format(key, n) in taken:
        n += 1
    return "{}_{}".format(key, n)


def _reparse_failed_block(block, taken):
    r"""Parse a rejected block properly, under a key that is free.

    A DuplicateBlockKeyBlock is valid BibTeX whose *only* defect is
    that its key is already used, so the regex salvage in
    `_record_from_failed_block` — which does not run the LaTeX
    middleware — mangles it needlessly: `\url{https://…}` loses its
    braces to become `\urlhttps://…` and is then eaten as an unknown
    command. Re-parsing the block on its own gives exactly what the
    entry would have produced had its key been unique.

    The record carries `bibtex_key_was` so the rename can be
    surfaced. A citation key is what the user types in a manuscript;
    changing one silently could break a \cite with no trace.
    Returns None if the block cannot be re-parsed, leaving the
    caller to fall back to the salvage."""
    raw = getattr(block, "raw", None) or ""
    original = getattr(block, "key", None)
    head = re.match(r"(\s*@\w+\s*\{\s*)([^,\s]+)(\s*,)", raw)
    if not head:
        return None
    original = original or head.group(2)
    new_key = _unique_key(original, taken)
    patched = raw[:head.start()] + head.group(1) + new_key + \
        head.group(3) + raw[head.end():]
    try:
        lib = bibtexparser.parse_string(
            patched, append_middleware=_PARSE_MIDDLEWARES)
    except Exception:
        return None
    if not lib.entries:
        return None
    rec = _record_from_entry(lib.entries[0])
    _restore_verbatim_fields(patched, [rec])
    if new_key != original:
        rec["bibtex_key_was"] = original
    return rec


# ---- Writing -------------------------------------------------------


# An unescaped `%` starts a comment that runs to the end of the line,
# and a field is written on one line — so a raw per-cent sign silently
# truncates its own value and everything after it, closing brace
# included. Found 2026-09-02: two abstracts in a real 76-entry file
# lost two thirds of their text to "78% sequence identity". The
# lookbehind leaves an already-escaped `\%` alone.
_BARE_PERCENT_RE = re.compile(r"(?<!\\)%")


# A page range is spelled `123--130` in BibTeX — an en dash, which
# TeX writes as two hyphens. Any single dash form is converted: the
# parser normalises `--` to an en dash on the way in (the CSL and RIS
# exporters depend on that), and OpenAlex hands back a plain hyphen,
# so by the time a range reaches here it may be spelled any of three
# ways. `--+` in the alternation keeps an already-correct `713--730`
# from becoming `713----730`.
_PAGE_DASH_RE = re.compile(r"\s*(?:-{2,}|[-–—])\s*")


def _format_value(v, field=None):
    """Wrap a value in BibTeX braces, escaping what would otherwise
    end the value early and restoring BibTeX's own spellings."""
    if v is None:
        return "{}"
    s = str(v)
    if field and field.lower() == "pages":
        s = _PAGE_DASH_RE.sub("--", s)
    return "{" + _BARE_PERCENT_RE.sub(r"\\%", s) + "}"


def _record_field_order(rec):
    """Yield (field_name, value) pairs in a stable, readable order."""
    if rec.get("title"):
        yield "title", rec["title"]
    if rec.get("authors"):
        corporate = set(rec.get("corporate_authors") or [])
        yield "author", " and ".join(
            _display_to_lastfirst(a, corporate=(a in corporate))
            for a in rec["authors"])
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
            k, _format_value(v, k), sep, w=width))
    lines.append("}")
    return "\n".join(lines)


def write(records):
    """Render a list of records to a BibTeX string."""
    return "\n\n".join(write_record(r) for r in records) + "\n"
