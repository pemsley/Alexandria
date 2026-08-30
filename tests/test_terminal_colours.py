"""The embedded terminal's colours (alexandria.theme).

_build_terminal used to set only the background, leaving VTE's
default light-grey foreground — fine on VTE's own black, unreadable
on the white background we ask for in light mode. Both ends must be
set together, and the pair must actually be legible.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import theme


def _channel(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_str):
    h = hex_str.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * _channel(r) + 0.7152 * _channel(g)
            + 0.0722 * _channel(b))


def _contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_returns_both_ends():
    for is_dark in (True, False):
        fg, bg = theme.terminal_colours(is_dark)
        assert fg.startswith("#") and len(fg) == 7
        assert bg.startswith("#") and len(bg) == 7


def test_light_theme_is_dark_text_on_light_ground():
    fg, bg = theme.terminal_colours(False)
    assert _luminance(fg) < _luminance(bg)


def test_dark_theme_is_light_text_on_dark_ground():
    fg, bg = theme.terminal_colours(True)
    assert _luminance(fg) > _luminance(bg)


def test_both_schemes_are_comfortably_legible():
    """WCAG AAA for body text is 7:1; terminal text is small and
    dense, so hold both schemes to it."""
    for is_dark in (True, False):
        fg, bg = theme.terminal_colours(is_dark)
        ratio = _contrast(fg, bg)
        assert ratio >= 7.0, "{} scheme contrast {:.1f}:1".format(
            "dark" if is_dark else "light", ratio)


def test_backgrounds_match_the_established_choices():
    """The backgrounds were already tuned against the Adwaita
    surfaces either side; only the foreground was missing."""
    assert theme.terminal_colours(True)[1] == "#1e1e1e"
    assert theme.terminal_colours(False)[1] == "#ffffff"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
