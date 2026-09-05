"""When the card list may rebuild.

Every library change schedules a full rebuild of every card a short
debounce later. That is right for a single change and wrong for a
burst: during a bulk import it means one rebuild per imported file,
each competing with the import threads for the GIL and for SQLite.
Measured 2026-08-30 while importing 184 PDFs, the main loop froze
for 4.7 s with only 33 papers in the library — and the cost of a
rebuild grows with the library, so it gets worse as the import runs.

Rationing delays a rebuild; it never cancels one. The list always
catches up, at most MIN_INTERVAL_MS behind.
"""

DEBOUNCE_MS = 300
MIN_INTERVAL_MS = 5000

# Typing is a different kind of burst from a filesystem event, and
# the reader is waiting on the result — 300 ms between letters reads
# as lag. Fast typing runs at roughly 100 ms per character, so this
# is long enough to swallow a word and short enough to feel prompt.
SEARCH_DEBOUNCE_MS = 150

# Below this, the list is left unfiltered. A one- or two-character
# query matches almost everything, so it returns the full row limit
# and rebuilds every card — the most expensive query of the sequence
# and the least useful, which is what made the second and third
# keystrokes feel slow.
SEARCH_MIN_CHARS = 3


def search_query(text, min_chars=SEARCH_MIN_CHARS):
    """The query to actually run for what is in the search box, or
    None to show the unfiltered list.

    Returning None rather than the short string matters: the
    alternative is leaving the previous result on screen, so deleting
    a query back to one letter would strand the user in a filtered
    view they can no longer see the reason for.

    `min_chars` is lowered by callers that set the box themselves —
    a filter chip naming a two-letter surname is a deliberate search,
    not someone part-way through typing."""
    s = (text or "").strip()
    if len(s) < min_chars:
        return None
    return s


def reload_delay_ms(now, last_reload_at, debounce_ms=DEBOUNCE_MS,
                    min_interval_ms=MIN_INTERVAL_MS,
                    import_busy=False):
    """Milliseconds to wait before the next rebuild, or None to skip
    rebuilding entirely for now.

    `now` and `last_reload_at` are monotonic seconds;
    `last_reload_at` is None when nothing has rebuilt yet.

    While a bulk import is running the answer is None: the progress
    bar is the feedback, and a rebuild started mid-import competes
    with the import threads for the GIL and loses badly — 32 s to
    rebuild 136 cards, work that takes 0.5 s uncontended, and it
    starves the import in turn. The import's completion path
    rebuilds once at the end."""
    if import_busy:
        return None
    if last_reload_at is None:
        return debounce_ms
    since_ms = (now - last_reload_at) * 1000.0
    if since_ms < 0:
        return debounce_ms
    if since_ms >= min_interval_ms:
        return debounce_ms
    return int(max(debounce_ms, min_interval_ms - since_ms))
