"""Tests for metrics.infer_collaborator_roles — the PI/Group
collaborator-relationship heuristic. Pure logic, no network/GTK.

Runnable as `python3 -m tests.test_collaborator_roles` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


def _a(name, position, oid, inst):
    return {"name": name, "position": position, "orcid": None,
            "openalex_id": oid, "institution": inst}


def _work(authorships, doi=None, title="t", year=2020):
    return {"doi": doi, "title": title, "year": year,
            "authorships": authorships}


TARGET = "A1"   # the viewed author


def test_clear_pi_vote():
    # Coauthor A2 is last author, same institution -> PI.
    w = _work([_a("Bob Junior", "first", TARGET, "Scripps"),
               _a("Carol Middle", "middle", "A9", "Elsewhere"),
               _a("Pat Leader", "last", "A2", "Scripps")])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"] == {"role": "pi", "votes": 1, "against": 0,
                           "institution": "Scripps"}


def test_clear_group_vote():
    # Target is last author -> coauthor A2 was in target's group.
    w = _work([_a("Pat Junior", "first", "A2", "Scripps"),
               _a("Bob Leader", "last", TARGET, "Scripps")])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "group"
    assert roles["A2"]["votes"] == 1


def test_different_institutions_no_entry():
    w = _work([_a("Bob J", "first", TARGET, "Oxford"),
               _a("Pat L", "last", "A2", "Scripps")])
    assert metrics.infer_collaborator_roles(TARGET, [[w]]) == {}


def test_missing_institution_no_entry():
    w = _work([_a("Bob J", "first", TARGET, None),
               _a("Pat L", "last", "A2", None)])
    assert metrics.infer_collaborator_roles(TARGET, [[w]]) == {}


def test_both_middle_no_entry():
    w = _work([_a("Ann First", "first", "A9", "Scripps"),
               _a("Bob Mid", "middle", TARGET, "Scripps"),
               _a("Pat Mid", "middle", "A2", "Scripps"),
               _a("Zed Last", "last", "A8", "Scripps")])
    assert "A2" not in metrics.infer_collaborator_roles(TARGET, [[w]])


def test_tie_no_entry():
    w1 = _work([_a("Bob J", "first", TARGET, "Scripps"),
                _a("Pat L", "last", "A2", "Scripps")], doi="10.1/x")
    w2 = _work([_a("Pat J", "first", "A2", "Scripps"),
                _a("Bob L", "last", TARGET, "Scripps")], doi="10.1/y")
    assert metrics.infer_collaborator_roles(TARGET, [[w1, w2]]) == {}


def test_majority_wins_with_against_count():
    pi = [_work([_a("Bob J", "first", TARGET, "Scripps"),
                 _a("Pat L", "last", "A2", "Scripps")],
                doi="10.1/{}".format(i)) for i in range(2)]
    grp = [_work([_a("Pat J", "first", "A2", "Scripps"),
                  _a("Bob L", "last", TARGET, "Scripps")], doi="10.1/g")]
    roles = metrics.infer_collaborator_roles(TARGET, [pi + grp])
    assert roles["A2"] == {"role": "pi", "votes": 2, "against": 1,
                           "institution": "Scripps"}


def test_alphabetical_four_authors_discarded():
    # Ascending surnames, 4 authors -> no votes from this work.
    w = _work([_a("Ann Alpha", "first", TARGET, "Scripps"),
               _a("Bob Beta", "middle", "A9", "Scripps"),
               _a("Cid Gamma", "middle", "A8", "Scripps"),
               _a("Pat Zulu", "last", "A2", "Scripps")])
    assert metrics.infer_collaborator_roles(TARGET, [[w]]) == {}


def test_alphabetical_three_authors_kept():
    # Autin/Gardner/Olson case: 3 authors alphabetical by chance —
    # still counts.
    w = _work([_a("Ludovic Autin", "first", "A2", "Scripps"),
               _a("Adam Gardner", "middle", "A9", "Scripps"),
               _a("Arthur Olson", "last", TARGET, "Scripps")])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "group"


def test_dedup_same_doi_across_lists():
    w = _work([_a("Bob J", "first", TARGET, "Scripps"),
               _a("Pat L", "last", "A2", "Scripps")], doi="10.1/dup")
    roles = metrics.infer_collaborator_roles(TARGET, [[w], [dict(w)]])
    assert roles["A2"]["votes"] == 1


def test_dedup_no_doi_uses_title_year():
    w1 = _work([_a("Bob J", "first", TARGET, "Scripps"),
                _a("Pat L", "last", "A2", "Scripps")],
               doi=None, title="Same Paper", year=1999)
    roles = metrics.infer_collaborator_roles(
        TARGET, [[w1], [dict(w1)]])
    assert roles["A2"]["votes"] == 1


def test_target_absent_no_vote():
    w = _work([_a("Ann A", "first", "A9", "Scripps"),
               _a("Pat L", "last", "A2", "Scripps")])
    assert metrics.infer_collaborator_roles(TARGET, [[w]]) == {}


def test_target_id_normalized_from_url():
    w = _work([_a("Bob J", "first", "A1", "Scripps"),
               _a("Pat L", "last", "A2", "Scripps")])
    roles = metrics.infer_collaborator_roles(
        "https://openalex.org/A1", [[w]])
    assert roles["A2"]["role"] == "pi"


def test_no_target_id_empty():
    assert metrics.infer_collaborator_roles(None, [[]]) == {}


def test_alphabetical_helper():
    alpha = [_a("A Aa", "first", "A1", None),
             _a("B Bb", "middle", "A2", None),
             _a("C Cc", "middle", "A3", None),
             _a("D Dd", "last", "A4", None)]
    assert metrics._authorship_list_is_alphabetical(alpha)
    # Equal adjacent surnames still count as ordered (family members).
    fam = list(alpha)
    fam[2] = _a("X Bb", "middle", "A3", None)
    assert metrics._authorship_list_is_alphabetical(fam)
    # Missing name disqualifies (treated as not-alphabetical).
    broken = list(alpha)
    broken[1] = _a(None, "middle", "A2", None)
    assert not metrics._authorship_list_is_alphabetical(broken)
    # Out of order.
    unordered = list(alpha)
    unordered[0], unordered[3] = unordered[3], unordered[0]
    assert not metrics._authorship_list_is_alphabetical(unordered)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- Corresponding-author rule (used when a work carries flags) -----

def _ac(name, position, oid, inst, corr):
    a = _a(name, position, oid, inst)
    a["is_corresponding"] = corr
    return a


def test_corresponding_coauthor_is_pi():
    # Coauthor is corresponding (middle position!) -> PI vote.
    w = _work([_a_corr := _ac("Bob J", "first", TARGET, "Scripps", False),
               _ac("Pat C", "middle", "A2", "Scripps", True),
               _ac("Zed L", "last", "A9", "Elsewhere", False)])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "pi"


def test_corresponding_target_gives_group():
    w = _work([_ac("Pat J", "first", "A2", "Scripps", False),
               _ac("Bob C", "last", TARGET, "Scripps", True)])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "group"


def test_both_corresponding_no_vote():
    w = _work([_ac("Pat C", "first", "A2", "Scripps", True),
               _ac("Bob C", "last", TARGET, "Scripps", True)])
    assert metrics.infer_collaborator_roles(TARGET, [[w]]) == {}


def test_corresponding_rule_ignores_alphabetical():
    # 4 authors, ascending surnames — the position rule would discard
    # this work, but a corresponding flag restores its value.
    w = _work([_ac("Ann Alpha", "first", TARGET, "Scripps", False),
               _ac("Bob Beta", "middle", "A9", "Scripps", False),
               _ac("Cid Gamma", "middle", "A8", "Scripps", False),
               _ac("Pat Zulu", "last", "A2", "Scripps", True)])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "pi"


def test_corresponding_rule_beats_position_within_work():
    # Coauthor is corresponding but NOT last; someone else is last.
    # The corresponding rule governs the whole work: A2 gets the PI
    # vote, the (non-corresponding) last author A9 gets nothing.
    w = _work([_ac("Bob J", "first", TARGET, "Scripps", False),
               _ac("Pat C", "middle", "A2", "Scripps", True),
               _ac("Zed L", "last", "A9", "Scripps", False)])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "pi"
    assert "A9" not in roles


def test_no_flags_falls_back_to_position_rule():
    # Old cache entries (no is_corresponding keys anywhere) keep the
    # last-author rule — existing caches must not go dark.
    w = _work([_a("Bob J", "first", TARGET, "Scripps"),
               _a("Pat L", "last", "A2", "Scripps")])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "pi"


def test_all_flags_false_falls_back_to_position_rule():
    # New-schema work where OpenAlex marked nobody corresponding —
    # position rule applies (flags-all-False is patchy data, not
    # evidence of no seniority).
    w = _work([_ac("Bob J", "first", TARGET, "Scripps", False),
               _ac("Pat L", "last", "A2", "Scripps", False)])
    roles = metrics.infer_collaborator_roles(TARGET, [[w]])
    assert roles["A2"]["role"] == "pi"
