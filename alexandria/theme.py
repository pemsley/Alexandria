"""Theme-derived colour choices.

Pure data — no GTK imports — so the choices can be unit-tested and
reused. Widgets ask `_is_dark_theme()` (browse.py) for the resolved
light/dark state and then look the colours up here.
"""

# Embedded VTE terminal. The backgrounds are tuned against the
# Adwaita surfaces either side of the panel: pure #000 reads as a
# slab cut into the dark window, and the light background matches
# the cards' view-bg. The foregrounds must be set with them — VTE's
# default foreground is a light grey meant for its own black
# background, which on our white one is barely readable.
_TERMINAL = {
    # is_dark: (foreground, background)
    True:  ("#e3e3e3", "#1e1e1e"),
    False: ("#1d1d1d", "#ffffff"),
}


def terminal_colours(is_dark):
    """(foreground, background) hex strings for the embedded
    terminal in the given theme state."""
    return _TERMINAL[bool(is_dark)]
