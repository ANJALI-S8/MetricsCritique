"""Unit tests for the CAMEO calibration math in scripts/14_cameo_event_coding.py.

These cover the parts that decide what the product publishes -- the Wilson
interval that appears in the spec, and the per-class threshold fit that decides
which labels survive. Run before any full coding pass:

    .venv/bin/python scripts/tests/test_cameo_calibration.py

`wilson()` is checked against statsmodels rather than against hand arithmetic,
because the point of an independent check is that it be independent.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "cameo", ROOT / "scripts" / "14_cameo_event_coding.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

RESULTS: list[bool] = []

def frame(key, scores, judgments, lang="en", weight=None):
    """Build a judged frame whose BLENDED score equals `scores` exactly.

    fit_thresholds() now fits on blended_score(biencoder, nli), so a test that
    supplied only nli_score would silently exercise a different function than
    production. Choosing biencoder = FLOOR + s*(0.70-FLOOR) makes the rescaled
    cosine equal s, and with nli = s the blend collapses to s for any weight --
    so these tests stay valid if NLI_BLEND_W is ever retuned.
    """
    s = np.asarray(scores, dtype=float)
    bi = m.BIENCODER_FLOOR + s * (0.70 - m.BIENCODER_FLOOR)
    assert np.allclose(m.blended_score(bi, s), s), "blend construction is wrong"
    d = {"code_key": [key] * len(s), "biencoder_score": bi, "nli_score": s,
         "judgment": list(judgments), "lang": [lang] * len(s)}
    if weight is not None:
        d["weight"] = weight
    return pd.DataFrame(d)





def chk(name: str, cond, detail: str = "") -> None:
    RESULTS.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")


def test_wilson() -> None:
    from statsmodels.stats.proportion import proportion_confint
    cases = [(0, 10), (1, 10), (3, 20), (13, 18), (18, 25), (50, 100),
             (99, 100), (100, 100), (8, 8)]
    worst = max(max(abs(a - b) for a, b in
                    zip(m.wilson(k, n), proportion_confint(k, n, method="wilson")))
                for k, n in cases)
    chk("wilson matches statsmodels exactly", worst < 1e-12, f"max abs diff {worst:.2e}")
    lo, hi = m.wilson(13, 18)
    chk("wilson(13,18) reproduces the audit's reported 0.49-0.88",
        abs(lo - 0.491) < 0.01 and abs(hi - 0.875) < 0.01, f"[{lo:.3f},{hi:.3f}]")
    chk("wilson stays inside [0,1] at the extremes",
        all(0 <= v <= 1 for k in (0, 1, 19, 20) for v in m.wilson(k, 20)))


def test_threshold_fit() -> None:
    rng = np.random.RandomState(0)
    pos, neg = rng.uniform(0.5, 0.9, 30), rng.uniform(0.02, 0.15, 30)
    sep = frame("a", np.concatenate([pos, neg]), [1] * 30 + [0] * 30)
    th = m.fit_thresholds(sep)["classes"]["a"]
    chk("perfect separation -> F1 = 1", th["f1"] == 1.0)
    chk("threshold sits above every negative", th["threshold"] > neg.max())
    chk("threshold sits at or below every positive",
        th["threshold"] <= round(pos.min(), 3) + 1e-9)
    chk("gray band brackets the threshold on both sides",
        th["gray_lo"] < th["threshold"] <= th["gray_hi"],
        f"[{th['gray_lo']}, {th['threshold']}, {th['gray_hi']}]")
    chk("per-class judged languages are recorded", th["languages"] == ["en"])


def test_tie_break_favours_precision() -> None:
    """At equal F1 the stricter cut must win -- the predecessor's central
    defect was over-tagging, so ties should not be resolved toward recall."""
    tie = frame("t", [.99, .95, .90, .85, .80, .75, .70, .65, .60, .55,
                      .40, .35, .30, .25, .20, .15, .10, .08, .05, .02],
                [1] * 10 + [0] * 10)
    tt = m.fit_thresholds(tie)["classes"]["t"]
    chk("tie-break picks the strictest cut achieving max F1",
        tt["threshold"] == 0.55, f"t={tt['threshold']}")
    chk("...and that cut is P=1.0 R=1.0", tt["precision"] == 1.0 and tt["recall"] == 1.0)

    ov = frame("o", [.90, .85, .80, .75, .70, .65, .60, .55, .50, .45,
                     .50, .45, .30, .25, .20, .15, .10, .08, .05, .02],
               [1] * 10 + [0] * 10)
    oo = m.fit_thresholds(ov)["classes"]["o"]
    chk("overlapping class still maximises F1", oo["f1"] >= 0.9,
        f"F1={oo['f1']} t={oo['threshold']} P={oo['precision']} R={oo['recall']}")
    chk("precision CI is reported and contains the point estimate",
        oo["precision_ci95"][0] <= oo["precision"] <= oo["precision_ci95"][1])


def test_thin_evidence_refuses_to_fit() -> None:
    """A class with too few judged positives must be marked uncalibrated, not
    handed a borrowed number that makes it look calibrated."""
    rng = np.random.RandomState(1)
    thin = frame("b", rng.uniform(0, 1, 20), [1] * 3 + [0] * 17, lang="bn")
    tb = m.fit_thresholds(thin)["classes"]["b"]
    chk("3 positives -> uncalibrated", tb["status"] == "uncalibrated")
    chk("uncalibrated falls back to the global threshold, marked as such",
        tb["threshold"] == m.GLOBAL_THRESHOLD and tb["source"] == "global_fallback")
    chk("uncalibrated carries no fabricated precision figure", "precision" not in tb)
    chk("uncalibrated states its reason in the data", "judged positives" in tb["reason"])

    k = m.MIN_JUDGED_POSITIVES
    edge = frame("c", np.concatenate([rng.uniform(.6, .9, k), rng.uniform(0, .2, 40 - k)]),
                 [1] * k + [0] * (40 - k))
    chk(f"exactly {k} positives fits (boundary is inclusive)",
        m.fit_thresholds(edge)["classes"]["c"]["status"] == "calibrated")


def test_sampling_weights_change_the_fit() -> None:
    """The calibration sample is stratified, so unweighted fitting optimises F1
    on a distribution that does not exist in production. Assert the weights are
    genuinely used rather than accepted and ignored."""
    # The classes must OVERLAP, otherwise one cut is optimal under every
    # weighting and the test passes without exercising anything. Positives and
    # negatives are interleaved between 0.30 and 0.60 on purpose.
    scores = [.90, .80, .70, .60, .50, .40, .35, .30,      # positives
              .65, .55, .45, .38, .20, .12, .06, .02]      # negatives
    y = [1] * 8 + [0] * 8
    flat = m.fit_thresholds(frame("w", scores, y, weight=[1.0] * 16))["classes"]["w"]
    # Up-weight only the three negatives that sit INSIDE the overlap (.65/.55/.45).
    # In the population those are far more numerous than the sample suggests, so
    # a weight-aware fit must raise the cut to exclude them; a fit that ignores
    # the weights cannot move at all.
    wts = [1.0] * 8 + [400.0, 400.0, 400.0] + [1.0] * 5
    skew = m.fit_thresholds(frame("w", scores, y, weight=wts))["classes"]["w"]
    chk("sampling weights move the fitted cut (non-vacuous: classes overlap)",
        skew["threshold"] > flat["threshold"],
        f"unweighted t={flat['threshold']} -> weighted t={skew['threshold']}")
    chk("weighted run is recorded as weighted in the output", skew["weighted"] is True)
    chk("population positive rate is reported", "pop_positive_rate" in skew,
        str(skew.get("pop_positive_rate")))
    chk("lift over base rate is published alongside precision",
        "lift_over_base" in skew and skew["lift_over_base"] > 0,
        f"lift={skew.get('lift_over_base')}x at P={skew.get('precision')}")


def test_blend_beats_nli_alone_on_the_real_sample() -> None:
    """Regression guard on the headline finding: on the actual judged file the
    blended score must rank better than the raw NLI probability. If a future
    change makes NLI decisive again, this fails."""
    path = ROOT / "data" / "calibration_cameo.csv"
    if not path.exists():
        chk("real calibration file present", False, "data/calibration_cameo.csv missing")
        return
    df = pd.read_csv(path)
    df = df[df.judgment.notna()]
    from sklearn.metrics import roc_auc_score
    wins = 0, 0
    n_better = n_total = 0
    for _k, g in df.groupby("code_key"):
        yv = g.judgment.astype(int).values
        if yv.sum() < 2 or yv.sum() == len(yv):
            continue
        n_total += 1
        a_blend = roc_auc_score(yv, m.blended_score(g.biencoder_score.values, g.nli_score.values))
        a_nli = roc_auc_score(yv, g.nli_score.values)
        n_better += a_blend > a_nli
    chk("blended score out-ranks raw NLI on most classes",
        n_better >= n_total - 1, f"{n_better}/{n_total} classes")


def main() -> int:
    for fn in (test_wilson, test_threshold_fit, test_tie_break_favours_precision,
               test_thin_evidence_refuses_to_fit, test_sampling_weights_change_the_fit,
               test_blend_beats_nli_alone_on_the_real_sample):
        print(f"\n{fn.__name__}")
        fn()
    n_ok = sum(RESULTS)
    print(f"\n  {n_ok}/{len(RESULTS)} checks passed")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
