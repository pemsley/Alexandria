"""A summary can be machine-written or hand-written.

`summary.model` names a model; `summary.author` names a person.
The chip must say which — "AI summary" vs plain "Summary" — because
in a library full of machine-learning papers a bare "AI" badge
reads as a topic tag, not as provenance.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import markup

AI = {"text": "t", "model": "claude-opus-5", "source": "jats",
      "generated_at": "2026-08-30T10:00:00Z"}
HUMAN = {"text": "t", "author": "Paul Emsley",
         "generated_at": "2026-08-30T10:00:00Z"}


# ---- chip label ---------------------------------------------------

def test_model_written_summary_is_labelled_ai():
    assert markup.summary_chip_label(AI) == "AI summary"


def test_hand_written_summary_is_labelled_plainly():
    assert markup.summary_chip_label(HUMAN) == "Summary"


def test_human_authorship_wins_over_a_model_field():
    """A person who edits and signs a machine draft takes
    responsibility for it: attribute the human."""
    both = dict(AI, author="Paul Emsley")
    assert markup.summary_chip_label(both) == "Summary"


def test_unattributed_summary_is_not_claimed_as_ai():
    assert markup.summary_chip_label({"text": "t"}) == "Summary"
    assert markup.summary_chip_label(None) == "Summary"


# ---- attribution line ---------------------------------------------

def test_attribution_names_the_person():
    line = markup.summary_attribution(HUMAN)
    assert "Paul Emsley" in line
    assert "2026-08-30" in line
    assert "claude" not in line.lower()


def test_attribution_names_the_model_and_tier():
    line = markup.summary_attribution(AI)
    assert "claude-opus-5" in line
    assert "jats" in line


def test_attribution_prefers_the_human_when_both_present():
    line = markup.summary_attribution(dict(AI, author="Paul Emsley"))
    assert line.startswith("Paul Emsley")
    # The machine draft is still disclosed, not hidden.
    assert "claude-opus-5" in line


def test_attribution_degrades_without_fields():
    assert markup.summary_attribution({"text": "t"}) == "Unattributed"
    assert markup.summary_attribution(None) == "Unattributed"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
