"""Story / event taxonomy path from the stored article labels. READ-ONLY: it only aggregates labels, never changes them.

Rule (one rule, used by the Trending page and the advanced dashboard so they always agree):

  A story (or event) carries a taxonomy value at a level ONLY if a STRICT MAJORITY (> 50 %) of its labelled articles carry
  that same value at that level, and the levels are resolved top-down, so the displayed path is always a real branch that
  most of the articles actually agree with.

      domain     the value held by > 50 % of the labelled articles
      topic      the value held by > 50 % of the labelled articles AND lying inside the assigned domain
      category   ... inside the assigned topic
      subtopic   ... inside the assigned category

  A plurality is NOT enough: 31 % of the articles in one domain and the rest scattered over seven others is not a story
  "about" that domain, it is a story the classifier could not place. Such a story is reported honestly instead of being
  shown under whichever scattered label happened to be the largest.

Status of the result (`tax_status`):
  assigned    a domain with majority support (and any deeper levels that also have it)
  outside     the majority label is the out-of-scope domain (D00 "Non-relevant / Other"); no taxonomy path is shown
  mixed       the labelled articles do not agree on a domain
  unlabelled  no article of the story has a label
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TIER_COL = {"domain": "domain_id", "topic": "topic_id", "category": "category_id", "subtopic": "subtopic_id"}
OUT_OF_SCOPE_DOMAIN = "D00"
PATH_COLUMNS = ["domain", "topic", "category", "subtopic", "tax_status", "n_labelled", "domain_share"]


def assign_paths(lab: pd.DataFrame, key: str, *, min_agreement: float = 0.5, min_labelled: int = 1,
                 out_of_scope: str = OUT_OF_SCOPE_DOMAIN) -> pd.DataFrame:
    """Per-`key` taxonomy path. `lab` has one row per article with `key`, `has_label` and the four *_id columns.

    Returns a frame indexed by `key` with columns domain, topic, category, subtopic (NaN where unsupported), tax_status,
    n_labelled and domain_share (the share of labelled articles behind the domain, NaN when none). Deterministic.
    A value needs STRICTLY MORE than `min_agreement` of the labelled articles (0.5 = strict majority; raise it to demand more)."""
    cols = PATH_COLUMNS
    keys = pd.Index(pd.unique(lab[key]), name=key)
    out = pd.DataFrame(index=keys, columns=cols, dtype=object)
    out["tax_status"] = "unlabelled"
    out["n_labelled"] = 0
    if not len(lab):
        return out
    ok = lab["has_label"].fillna(False).astype(bool) & lab["domain_id"].notna()
    sub = lab[ok]
    if not len(sub):
        return out
    n_lab = sub.groupby(key).size()
    out["n_labelled"] = n_lab.reindex(keys).fillna(0).astype(int)
    parent: tuple[str, str] | None = None
    for tier, col in TIER_COL.items():
        if parent is not None:
            p = out[parent[0]].rename("_p")
            sub = sub.join(p, on=key)
            sub = sub[sub[parent[1]] == sub["_p"]].drop(columns="_p")
        g = sub.dropna(subset=[col]).groupby([key, col]).size().rename("n").reset_index()
        if not len(g):
            break
        g["tot"] = g[key].map(n_lab)
        # strictly more than min_agreement (0.5 -> a strict majority; at most one value per key can exceed it, so no tie to break)
        win = g[(g["n"] > min_agreement * g["tot"]) & (g["tot"] >= min_labelled)].set_index(key)
        out.loc[win.index, tier] = win[col]
        if tier == "domain":
            out.loc[win.index, "domain_share"] = win["n"] / win["tot"]
        parent = (tier, col)
    dom = out["domain"]
    out.loc[dom.notna() & (dom != out_of_scope), "tax_status"] = "assigned"
    out.loc[dom == out_of_scope, "tax_status"] = "outside"
    out.loc[dom.isna() & (out["n_labelled"] > 0), "tax_status"] = "mixed"
    out["n_labelled"] = out["n_labelled"].astype(int)
    out["domain_share"] = pd.to_numeric(out["domain_share"], errors="coerce")
    return out
