"""A duplicate citation key must not corrupt or silently lose an entry.

Found 2026-09-05 comparing a real 86-entry file with Alexandria's
export of it: 86 in, 85 out, and the one survivor's `howpublished`
had become `://github.com/facebook/react`.

Both faults were the same fault. The file has two `@misc{react,`
entries; bibtexparser accepts the first and rejects the second as a
DuplicateBlockKeyBlock — correctly, since a .bib cannot carry two
entries under one key. The rejected block then fell to
`_record_from_failed_block`, a regex salvage that does *not* run the
LaTeX middleware, so `\\url{https://…}` lost its braces to become
`\\urlhttps://…` and was then eaten as an unknown command.

bibtexparser was blameless throughout: given that block on its own
it decodes `\\url{…}` and `{ATP}` perfectly. The block's only defect
is its key. So re-parse it in isolation under a unique key, and tell
the user the key changed — a citation key is what they type in a
manuscript, and renaming one silently can break a \\cite with no
trace.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import bibtex

DUPES = """@misc{react,
  author       = {{Meta Platforms, Inc.}},
  title        = {React: A {JavaScript} library for building user interfaces},
  year         = {2013},
  howpublished = {\\url{https://github.com/facebook/react}}
}

@misc{react,
  author       = {{Meta Platforms, Inc.}},
  title        = {React: The library for web and native user interfaces},
  year         = {2025},
  url          = {https://react.dev/},
  urldate      = {2024-05-15}
}
"""


# ---- nothing is lost --------------------------------------------------

def test_both_entries_survive():
    recs = bibtex.parse(DUPES)
    assert len(recs) == 2, "the second entry used to be dropped"


def test_the_keys_are_made_unique():
    keys = [r["bibtex_key"] for r in bibtex.parse(DUPES)]
    assert len(set(keys)) == 2
    assert "react" in keys


def test_the_first_entry_keeps_the_original_key():
    """Whatever the user already cites resolves to the entry that was
    there first."""
    recs = bibtex.parse(DUPES)
    assert recs[0]["bibtex_key"] == "react"
    assert recs[0]["year"] == 2013


# ---- and nothing is corrupted ----------------------------------------

def test_the_salvaged_entry_is_decoded_properly():
    """The whole point: the rejected block is valid BibTeX apart from
    its key, so it must go through the same decoding as any other."""
    recs = bibtex.parse(DUPES)
    first = recs[0]
    assert first["bibtex_extra"]["howpublished"] == \
        "https://github.com/facebook/react"


def test_braces_inside_a_salvaged_title_are_handled():
    recs = bibtex.parse(DUPES)
    assert recs[0]["title"] == \
        "React: A JavaScript library for building user interfaces"


def test_the_other_fields_of_the_renamed_entry_survive():
    recs = bibtex.parse(DUPES)
    renamed = [r for r in recs if r["bibtex_key"] != "react"][0]
    assert renamed["bibtex_extra"]["url"] == "https://react.dev/"
    assert renamed["bibtex_extra"]["urldate"] == "2024-05-15"
    assert renamed["year"] == 2025


# ---- the rename is reported, not silent ------------------------------

def test_the_record_records_the_key_it_used_to_have():
    """Durable, so it can be surfaced on the card and in the editor
    long after the import toast has gone."""
    renamed = [r for r in bibtex.parse(DUPES)
               if r["bibtex_key"] != "react"][0]
    assert renamed["bibtex_key_was"] == "react"


def test_an_untouched_entry_carries_no_rename_marker():
    recs = bibtex.parse("@article{solo, title={T}, year={2020}}")
    assert recs[0].get("bibtex_key_was") is None


def test_three_entries_sharing_one_key():
    text = DUPES + DUPES.split("\n\n")[1].replace("2025", "2026")
    keys = [r["bibtex_key"] for r in bibtex.parse(text)]
    assert len(keys) == len(set(keys)) == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
