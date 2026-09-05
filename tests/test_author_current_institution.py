"""The author header must show where they are *now*.

Reported 2026-09-04: Alexander Zawaira's page showed "MRC Laboratory
of Molecular Biology" in the header while the sidebar row for the
same person said "Taizhou University", and his recent papers are
plant science in China. OpenAlex has it right — MRC LMB is 2006-2006,
the earliest of his seven affiliations, and Taizhou is 2023-2024.

The header label is seeded from `authorship["institution"]`, which is
whatever the paper you opened him from happened to say — click a 2006
paper and you get his 2006 address. `_populate_affiliations` then
declined to correct it, because it only wrote the label when the
label was still invisible. The sidebar had no such guard, which is
why the two disagreed on screen.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

author_works = pytest.importorskip("alexandria.author_works")


class _FakeLabel:
    """Records what the header line was told to show."""

    def __init__(self, markup=None):
        self.markup = markup
        self.visible = markup is not None

    def set_markup(self, m):
        self.markup = m

    def set_visible(self, v):
        self.visible = v

    def get_visible(self):
        return self.visible


class _FakePage:
    """Enough of AuthorPage to run `_populate_affiliations`."""

    def __init__(self, seeded=None):
        self._sub_inst_lbl = _FakeLabel(seeded)
        self._current_institution = None
        self.told_the_sidebar = []
        self._on_institution = self.told_the_sidebar.append

    populate = author_works.AuthorPage._populate_affiliations


# Zawaira's real affiliation history, most-recent first as
# `metrics.fetch_author_profile` sorts it.
ROWS = [{"display_name": "Taizhou University",
         "year_min": 2023, "year_max": 2024}]

FULL = ROWS + [
    {"display_name": "University of Cape Town",
     "year_min": 2009, "year_max": 2010},
    {"display_name": "MRC Laboratory of Molecular Biology",
     "year_min": 2006, "year_max": 2006},
]


def test_the_profile_corrects_a_stale_seeded_institution():
    """The bug: opened from a 2006 paper, the header kept saying
    MRC LMB even after OpenAlex said Taizhou."""
    page = _FakePage(seeded="MRC Laboratory of Molecular Biology")
    page.populate(ROWS)
    assert "Taizhou University" in page._sub_inst_lbl.markup
    assert "MRC" not in page._sub_inst_lbl.markup


def test_it_still_fills_an_empty_header():
    """Callers that pass no institution — Discover, collaborator
    chips — must still get one."""
    page = _FakePage(seeded=None)
    page.populate(ROWS)
    assert "Taizhou University" in page._sub_inst_lbl.markup
    assert page._sub_inst_lbl.get_visible()


def test_the_sidebar_is_told_the_same_thing():
    """Header and sidebar row disagreeing is what made this visible."""
    page = _FakePage(seeded="MRC Laboratory of Molecular Biology")
    page.populate(ROWS)
    assert page.told_the_sidebar == ["Taizhou University"]
    assert "Taizhou University" in page._sub_inst_lbl.markup


def test_the_most_recent_of_several_wins():
    """The order is load bearing: fetch_author_profile sorts most
    recent first, and the header takes the head of that list."""
    assert author_works.current_affiliation(FULL)["display_name"] == \
        "Taizhou University"


def test_an_empty_history_has_no_current_affiliation():
    assert author_works.current_affiliation([]) is None
    assert author_works.current_affiliation(None) is None


def test_a_nameless_leading_row_is_skipped_not_shown():
    assert author_works.current_affiliation(
        [{"display_name": None}] + FULL)["display_name"] == \
        "Taizhou University"


def test_the_current_institution_is_remembered_for_the_photo_search():
    page = _FakePage(seeded=None)
    page.populate(ROWS)
    assert page._current_institution == "Taizhou University"


def test_no_affiliations_leaves_the_seeded_value_alone():
    """OpenAlex knows nothing: the paper's own address is better
    than blanking the line."""
    page = _FakePage(seeded="Some Institute")
    page.populate([])
    assert page._sub_inst_lbl.markup == "Some Institute"


def test_a_nameless_affiliation_does_not_blank_the_line():
    page = _FakePage(seeded="Some Institute")
    page.populate([{"display_name": None, "year_min": 2020,
                    "year_max": 2024}])
    assert page._sub_inst_lbl.markup == "Some Institute"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
