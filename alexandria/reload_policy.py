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
