"""Pango markup helpers shared across the GUI.

Paper titles in CrossRef / OpenAlex / publisher metadata routinely
contain inline formatting tags: italics for species names
(<i>Azotobacter vinelandii</i>), bold for emphasis, sub/superscript
for chemistry. We want those tags rendered as formatting, but we
also need to escape any *other* `<`, `>`, `&` so a stray angle
bracket can't crash Pango.

`safe_pango_markup(text)` is the single entry point: feed it a raw
string from a metadata source, hand the result to
`Gtk.Label.set_markup()`.
"""

import re

from gi.repository import GLib

try:
    import gi
    gi.require_version("Pango", "1.0")
    from gi.repository import Pango
except Exception:  # pragma: no cover - Pango is always present in the GUI
    Pango = None

# Pango-supported inline tags we accept verbatim. Everything outside
# this list is escaped.
_SAFE_INLINE_TAGS = ("i", "b", "u", "s", "em", "strong",
                     "sub", "sup", "small", "tt")
# The capture pass also protects the exact small-caps span we emit
# from <scp> (see _translate_scp) plus its closing </span>, so they
# survive escaping. We only ever generate this span ourselves —
# source metadata uses <scp>, not <span> — so matching the literal
# open tag (not arbitrary attributes) keeps untrusted span attributes
# from leaking through.
_SAFE_TAG_RE = re.compile(
    r'<span variant="smallcaps">|</span>|'
    r"</?(?:" + "|".join(_SAFE_INLINE_TAGS) + r")\s*/?>", re.IGNORECASE)

# Pad missing spaces around inline tags: when a tag butts up against a
# word character, insert a single space. Handles both opening tags
# preceded by a word ("the<i>X") and closing tags followed by one
# ("X</i>foo"). Punctuation is left alone.
_PAD_OPEN_RE = re.compile(
    r"(\w)(<(?:" + "|".join(_SAFE_INLINE_TAGS) + r")\b[^>]*>)", re.IGNORECASE)
_PAD_CLOSE_RE = re.compile(
    r"(</(?:" + "|".join(_SAFE_INLINE_TAGS) + r")\s*>)(\w)", re.IGNORECASE)


def _pad_inline_tags(text):
    text = _PAD_OPEN_RE.sub(r"\1 \2", text)
    text = _PAD_CLOSE_RE.sub(r"\1 \2", text)
    return text


# Publisher metadata (esp. JATS-derived titles from CrossRef) uses
# <scp> for small-caps — e.g. "<scp>EM</scp>", "<scp>ATPase</scp>".
# Pango has no <scp> tag, but renders small-caps via
# <span variant="smallcaps">…</span>. Translate to that, done before
# the whitelist capture below so the span is protected from escaping.
# Crossref and OpenAlex hand back abstracts as namespaced JATS
# fragments: "<jats:p>The Gly/N-degron pathway …<jats:italic>Oryza
# sativa</jats:italic>…". Map the inline ones onto their Pango
# equivalents, turn paragraph breaks into blank lines, and drop
# every other jats: tag while keeping its text — a reader wants the
# prose, not the schema.
_JATS_INLINE = {
    "italic": "i", "bold": "b", "sub": "sub", "sup": "sup",
    "monospace": "tt", "underline": "u", "strike": "s",
    "sc": "scp",            # small-caps: handed to _translate_scp
}
_JATS_TAG_RE = re.compile(r"</?jats:([a-z-]+)\s*/?>", re.IGNORECASE)
_JATS_P_CLOSE_RE = re.compile(r"</jats:p>\s*(?=<jats:p>)",
                              re.IGNORECASE)


# Crossref hands back pretty-printed XML, so titles and abstracts
# carry the indentation with them:
#   'Crystal structure of rice\n          <scp>L</scp>\n          -galactose'
# Left alone that renders as three lines with a gap before the
# hyphen. Collapse every whitespace run to one space, then close
# the gaps that leaves around punctuation and brackets.
_WS_RUN_RE = re.compile(r"\s+")
_WS_BEFORE_PUNCT_RE = re.compile(r" +([-\u2013\u2014,;:.!?)\]}])")
_WS_AFTER_OPEN_RE = re.compile(r"([(\[{]) +")


def _collapse_source_whitespace(text):
    text = _WS_RUN_RE.sub(" ", text)
    text = _WS_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _WS_AFTER_OPEN_RE.sub(r"\1", text)
    return text.strip()


def _translate_jats(text):
    if "jats:" not in text.lower():
        return text
    # Paragraph boundaries first, so consecutive <jats:p> blocks
    # don't run together once the tags are gone.
    text = _JATS_P_CLOSE_RE.sub("\n\n", text)

    def _one(m):
        name = m.group(1).lower()
        mapped = _JATS_INLINE.get(name)
        if mapped is None:
            return ""                      # keep the text, drop the tag
        closing = m.group(0).startswith("</")
        return "</{}>".format(mapped) if closing else "<{}>".format(mapped)

    return _JATS_TAG_RE.sub(_one, text)


_SCP_PAIR_RE = re.compile(r"<scp>(.*?)</scp>", re.IGNORECASE | re.DOTALL)
_SCP_ANY_RE = re.compile(r"</?scp\s*>", re.IGNORECASE)

_SMALLCAPS_OPEN = '<span variant="smallcaps">'
_SMALLCAPS_CLOSE = "</span>"


def _translate_scp(text):
    # Convert balanced <scp>…</scp> to a small-caps span, then drop any
    # orphan tags left over (CrossRef/JATS titles sometimes carry an
    # unmatched </scp>, which would otherwise leave an unbalanced span
    # and make Pango reject the whole string).
    text = _SCP_PAIR_RE.sub(_SMALLCAPS_OPEN + r"\1" + _SMALLCAPS_CLOSE, text)
    return _SCP_ANY_RE.sub("", text)


def _markup_parses(s):
    """True if `s` is valid Pango markup. Malformed source metadata
    (e.g. an orphan </scp> in a CrossRef/JATS title) can survive the
    whitelist as an unbalanced tag, which Pango rejects — and
    set_markup() would then raise. Callers use this to fall back to
    plain escaped text instead of crashing."""
    if Pango is None:
        return True
    try:
        Pango.parse_markup(s, -1, "\x00")
        return True
    except Exception:
        return False


# Two private-use Unicode codepoints (Basic Multilingual Plane PUA,
# U+E000–U+F8FF). They don't appear in real metadata strings, so we
# can safely use them as opening/closing markers around the indices
# of captured (whitelisted) inline tags during escape/restore.
_PLACEHOLDER_OPEN = ""
_PLACEHOLDER_CLOSE = ""
_PLACEHOLDER_RE = re.compile(_PLACEHOLDER_OPEN + r"(\d+)" + _PLACEHOLDER_CLOSE)


def safe_pango_markup(text):
    """Escape `text` for Pango markup, preserving a whitelist of inline
    formatting tags (<i>, <b>, <sub>, <sup>, ...). Everything else —
    stray '<', '>', '&', etc. — is escaped. Returns a string that's
    safe to pass to Gtk.Label.set_markup()."""
    if not text:
        return ""
    original = text
    # OpenAlex/JATS titles sometimes arrive with markup *entity-encoded*
    # (e.g. "&lt;scp&gt;RELION&lt;/scp&gt;", "GABA&lt;sub&gt;A&lt;/sub&gt;")
    # rather than as real tags. Decode those four entities first so the
    # tag-translation + whitelist pass below sees real <scp>/<sub>/...
    # The whole string is re-escaped afterwards, so this is safe.
    text = (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&amp;", "&"))
    # Collapse the source's XML formatting *before* tag translation,
    # so the paragraph breaks _translate_jats inserts survive.
    text = _collapse_source_whitespace(text)
    text = _translate_jats(text)
    text = _translate_scp(text)
    text = _pad_inline_tags(text)
    placeholders = []

    def _capture(m):
        placeholders.append(m.group(0))
        return "{}{}{}".format(
            _PLACEHOLDER_OPEN, len(placeholders) - 1, _PLACEHOLDER_CLOSE)

    protected = _SAFE_TAG_RE.sub(_capture, text)
    escaped = GLib.markup_escape_text(protected)

    def _restore(m):
        return placeholders[int(m.group(1))]

    # Dropped tags (<jats:p> and friends) can leave the string
    # starting or ending with the space that stood beside them.
    result = _PLACEHOLDER_RE.sub(_restore, escaped).strip()
    # Malformed source markup can yield unbalanced tags Pango rejects;
    # degrade to fully-escaped plain text rather than letting
    # set_markup() raise.
    if not _markup_parses(result):
        return GLib.markup_escape_text(original)
    return result


def summary_chip_label(summary):
    """"AI summary" for a machine-written one, plain "Summary"
    otherwise. A bare "AI" badge sitting among PRE / CC-BY / Gold OA
    reads as a topic tag — "this paper is about AI" — which in a
    library of machine-learning papers is the likelier reading."""
    if summary and summary.get("author"):
        return "Summary"
    if summary and summary.get("model"):
        return "AI summary"
    return "Summary"


def summary_attribution(summary):
    """One-line provenance for a sidecar summary:

        "Paul Emsley · 2026-08-30"                     (hand-written)
        "claude-opus-5 · from jats · 2026-08-30"       (machine)

    A person who signs a machine draft takes responsibility for it,
    so `author` leads — but the model is still disclosed rather than
    hidden. Always returns something, so the UI can never present an
    unattributed summary as if it were the author's abstract."""
    if not summary:
        return "Unattributed"
    bits = []
    author = summary.get("author")
    model = summary.get("model")
    if author:
        bits.append(str(author))
        if model:
            bits.append("edited from {}".format(model))
    elif model:
        bits.append(str(model))
        if summary.get("source"):
            bits.append("from {}".format(summary["source"]))
    if summary.get("generated_at"):
        bits.append(str(summary["generated_at"])[:10])
    return " · ".join(bits) if bits else "Unattributed"


# Markdown subset for summaries. Anything richer (tables, links,
# nested lists) is left as literal text rather than half-rendered:
# a summary is a few paragraphs, and a wrong tag is worse than a
# visible asterisk.
_MD_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_MD_ITALIC_RE = re.compile(
    r"(?<![\w*])[*_](?=\S)([^*_\n]+?)(?<=\S)[*_](?![\w*])")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(?=\S)")


def markdown_to_pango(text):
    """Render the Markdown subset summaries are written in as Pango
    markup: **bold**, *italic* / _italic_, `code`, `#` headings and
    `-` / `*` bullets. Everything else is escaped literal text.

    Escaping happens first, so a stray `<script>` in the source can
    never become a tag; the markers we act on survive escaping
    untouched. Output that Pango still rejects degrades to fully
    escaped plain text — never a crash, never a stray tag."""
    if not text:
        return ""
    out_lines = []
    for line in GLib.markup_escape_text(text).split("\n"):
        heading = _MD_HEADING_RE.match(line)
        if heading:
            out_lines.append("<b>{}</b>".format(heading.group(1)))
            continue
        # Bullets first: a leading "* " is a list marker, not the
        # opening of an emphasis span.
        line = _MD_BULLET_RE.sub(r"\1• ", line)
        line = _MD_CODE_RE.sub(r"<tt>\1</tt>", line)
        line = _MD_BOLD_RE.sub(r"<b>\1</b>", line)
        line = _MD_ITALIC_RE.sub(r"<i>\1</i>", line)
        out_lines.append(line)
    result = "\n".join(out_lines)
    if not _markup_parses(result):
        return GLib.markup_escape_text(text)
    return result
