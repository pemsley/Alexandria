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

    result = _PLACEHOLDER_RE.sub(_restore, escaped)
    # Malformed source markup can yield unbalanced tags Pango rejects;
    # degrade to fully-escaped plain text rather than letting
    # set_markup() raise.
    if not _markup_parses(result):
        return GLib.markup_escape_text(original)
    return result
