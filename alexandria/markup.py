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

# Pango-supported inline tags we accept verbatim. Everything outside
# this list is escaped.
_SAFE_INLINE_TAGS = ("i", "b", "u", "s", "em", "strong",
                     "sub", "sup", "small", "tt")
_SAFE_TAG_RE = re.compile(
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

    return _PLACEHOLDER_RE.sub(_restore, escaped)
