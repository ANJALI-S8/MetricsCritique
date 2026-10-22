"""Unit tests of the shared taxonomy-agreement rule (lib/news_taxonomy.py)."""
import numpy as np
import pandas as pd
import pytest

import news_testutil  # noqa: F401  (puts scripts/ on sys.path)

from lib import news_taxonomy as TX


def L(key, *paths, labelled=True):
    return [{"k": key, "has_label": labelled, "domain_id": p[0], "topic_id": p[1], "category_id": p[2], "subtopic_id": p[3]} for p in paths]


def P(d, t=1, c=1, s=1):
    return (d, f"{d}.T{t:02d}", f"{d}.T{t:02d}.C{c:02d}", f"{d}.T{t:02d}.C{c:02d}.S{s:02d}")


def frame(rows):
    return pd.DataFrame(rows)


def test_unanimous_story_gets_the_full_path():
    r = TX.assign_paths(frame(L("a", *[P("D01")] * 3)), "k").loc["a"]
    assert (r["domain"], r["topic"], r["category"], r["subtopic"]) == ("D01", "D01.T01", "D01.T01.C01", "D01.T01.C01.S01")
    assert r["tax_status"] == "assigned" and r["n_labelled"] == 3 and r["domain_share"] == 1.0


def test_plurality_without_majority_is_mixed():
    # the reported bug: a film story whose articles are scattered; the largest single domain has 31 %, not a majority
    rows = L("film", *[P("D09")] * 4, *[P("D01")] * 3, *[P("D02")] * 3, *[P("D03")] * 3)
    r = TX.assign_paths(frame(rows), "k").loc["film"]
    assert r["tax_status"] == "mixed" and pd.isna(r["domain"]) and pd.isna(r["topic"])


def test_exactly_half_is_not_a_majority():
    r = TX.assign_paths(frame(L("a", *[P("D01")] * 2, *[P("D02")] * 2)), "k").loc["a"]
    assert r["tax_status"] == "mixed"


def test_path_stops_at_the_deepest_level_with_agreement():
    rows = L("a", P("D01", 1), P("D01", 1), P("D01", 2), P("D01", 3), P("D02"))      # D01 4/5, T01 2/5, no topic majority
    r = TX.assign_paths(frame(rows), "k").loc["a"]
    assert r["domain"] == "D01" and pd.isna(r["topic"]) and pd.isna(r["category"]) and r["tax_status"] == "assigned"
    rows = L("b", P("D01", 1, 1, 1), P("D01", 1, 1, 1), P("D01", 1, 1, 2), P("D01", 1, 2, 1))     # topic 4/4, category C01 3/4, subtopic S01 2/4
    r = TX.assign_paths(frame(rows), "k").loc["b"]
    assert (r["topic"], r["category"]) == ("D01.T01", "D01.T01.C01") and pd.isna(r["subtopic"])


def test_path_is_always_a_real_branch():
    # topic D02.T01 is the most common topic overall, but the domain majority is D03 -> it can never be reported
    rows = L("a", *[P("D02", 1)] * 3, *[P("D03", 5)] * 4, *[P("D03", 6)] * 4)
    r = TX.assign_paths(frame(rows), "k").loc["a"]
    assert r["domain"] == "D03" and pd.isna(r["topic"])


def test_out_of_scope_majority_is_outside_and_unlabelled_is_reported():
    rows = L("o", *[P("D00")] * 3, P("D01")) + L("u", P("D01"), labelled=False) + L("m", P("D01"), P("D02"))
    r = TX.assign_paths(frame(rows), "k")
    assert r.loc["o", "tax_status"] == "outside" and r.loc["o", "domain"] == "D00"
    assert r.loc["u", "tax_status"] == "unlabelled" and r.loc["u", "n_labelled"] == 0 and pd.isna(r.loc["u", "domain"])
    assert r.loc["m", "tax_status"] == "mixed"


def test_unlabelled_articles_do_not_vote_and_missing_domain_is_ignored():
    rows = L("a", P("D01"), P("D01"), (None, None, None, None)) + L("a", P("D05"), labelled=False)
    r = TX.assign_paths(frame(rows), "k").loc["a"]
    assert r["domain"] == "D01" and r["n_labelled"] == 2


def test_deterministic_and_order_independent():
    rows = L("a", *[P("D01")] * 3, *[P("D02")] * 2) + L("b", *[P("D03", 2)] * 2, P("D03", 1))
    a = TX.assign_paths(frame(rows), "k")
    b = TX.assign_paths(frame(rows[::-1]), "k").loc[a.index]
    pd.testing.assert_frame_equal(a, b)


def test_empty_input():
    r = TX.assign_paths(pd.DataFrame(columns=["k", "has_label", "domain_id", "topic_id", "category_id", "subtopic_id"]), "k")
    assert len(r) == 0
