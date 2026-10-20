"""
Deterministic fixture for the metrics dashboard.

Purpose
-------
`data/metrics.duckdb` is produced by the metrics builder. Until it exists (or when
a table inside it is missing) the dashboard needs *something* shaped exactly like
the schema contract in `docs/METRICS_V2_SPEC.md` §5 so the whole UI can be built
and visually verified.

This module generates that something. It is:

  * **seeded** -- identical output on every run (SEED below);
  * **contract-exact** -- table names and column names/order match §5 verbatim;
  * **statistically plausible** -- Negative-Binomial counts, an exposure (effort)
    offset with a collector ramp, BH-FDR q-values, support gating that really
    NULLs out low-evidence cells, EB shrinkage, AR(1) persistence, and a handful
    of planted emergence events so acceleration/burst/emergence have something
    real to detect.

It is NOT a simulation of the corpus and must never be presented as data. The
dashboard stamps `MODE: FIXTURE` on every page built from it.

Usage
-----
    from fixture import build_fixture
    tables = build_fixture()          # dict[str, pandas.DataFrame]

    python fixture.py --emit-duckdb UI/fixture.duckdb
        writes a real DuckDB file so the DuckDB read path in
        build_dashboard.py can be exercised end to end.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

SEED = 20260729

# Collection window per spec F4. The fixture keeps a slice of pre-window
# "YouTube backfill" so the UI's in_window partitioning is exercised.
WINDOW_START = date(2026, 6, 14)
WINDOW_END = date(2026, 7, 28)
BACKFILL_START = date(2026, 4, 20)

# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------

TOPICS = [
    # topic_id, domain, topic, label, description
    ("t000", "Conflict", "Russia-Ukraine", "UAV strikes & air defence",
     "Drone/UAV activity, air-defence engagements and strike claims on the "
     "Ukrainian axis. Dominated by Russian- and Ukrainian-language channels."),
    ("t001", "Cyber", "Threats", "Ransomware leak-site chatter",
     "Ransomware crew leak-site postings and victim disclosures relayed through "
     "aggregator accounts."),
    ("t002", "Civil", "Politics", "Indian domestic politics",
     "Hindi/Bengali political news cycles, exam and policy announcements, "
     "heavily link-shortened."),
    ("t003", "Conflict", "Russia-Ukraine", "Kyiv / strategic strikes",
     "Strikes on and around Kyiv and other rear-area targets; the largest "
     "English-language conflict cluster."),
    ("t004", "Cyber", "Threats", "Malware & OSINT tradecraft",
     "Malware family discussion, tooling and OSINT tradecraft chatter."),
    ("t005", "Civil", "Politics", "Sport & mass-event politics",
     "Large sporting events and the political commentary that attaches to them."),
    ("t006", "Civil", "Disaster", "Heat, weather & natural disaster",
     "Heatwaves, floods and disaster wire copy, mostly RSS-relayed."),
    ("t007", "Conflict", "Middle-East", "Iran / Middle-East escalation",
     "Iran-centred escalation reporting; the single largest cluster in the corpus."),
    ("unassigned", "Unassigned", "unassigned", "Unassigned",
     "Posts the clustering pass could not assign. Retained explicitly so "
     "coverage gaps are visible rather than silently missing."),
]

PLATFORMS = [
    # platform, has_engagement, in_ts_scope, notes
    ("telegram", True, True,
     "Dominant source. Collected by channel, not keyword -- no keyword labels. "
     "37% exact duplicates removed before aggregation. Views present, no likes."),
    ("youtube", True, True,
     "Carries real published_at back to 2011; pre-window rows are corpus members, "
     "not time-series points (in_window = false)."),
    ("reddit", False, True,
     "No engagement fields at all. Absent, not zero -- engagement metrics are NULL."),
    ("linkedin", True, True,
     "hashtags field populated for 502/924 posts -- the best hashtag coverage in "
     "the corpus."),
    ("instagram", True, True,
     "Small volume, 43% duplicate text. hashtags field populated 237/337."),
    ("facebook", True, True,
     "Zero likes on ~70% of posts; hashtags field empty, inline tags only."),
]

TAGS = [
    "ukraine", "russia", "kyiv", "drone", "airdefence", "iran", "israel",
    "gaza", "ransomware", "lockbit", "cybersecurity", "malware", "osint",
    "breakingnews", "newsupdate", "india", "politics", "heatwave", "flood",
    "worldcup", "geopolitics", "sanctions", "nato", "missile", "infosec",
]

# --------------------------------------------------------------------------
# metric_meta -- the semantic contract the UI reads instead of hardcoding
# --------------------------------------------------------------------------

_META = [
    # metric_key, display_name, family, formula, units, grain, direction,
    # higher_is_worse, caveats, support_gate
    ("V", "Volume", "volume", "count(posts) in cell", "posts",
     "topic x platform x day", "up",
     False,
     "Raw count. Inflated by collector ramp-up: Telegram went 422 -> 1,393 "
     "posts/day while active channels only went 18 -> 24. Use V_rate for trend.",
     "none - raw count is always emitted"),
    ("V_rate", "Volume rate (effort-normalised)", "volume",
     "V / effort, where effort = active channels or keywords that day "
     "(log-exposure offset: log E[V] = log(effort) + eta)",
     "posts per active channel", "topic x platform x day", "up", False,
     "The publishable volume series. Cancels the collector ramp; sensitive to "
     "effort being mis-measured on days a channel was partially collected.",
     "effort >= 1"),
    ("effort", "Collection effort", "volume",
     "count(distinct active channels or keywords) that day", "channels",
     "platform x day", "up", False,
     "This is an instrument reading, not a signal about the world. Shown so an "
     "analyst can see when a rise in V is the scraper and not the topic.",
     "none"),
    ("growth", "Growth", "growth",
     "g_t = log(V_rate_t + eps) - log(V_rate_{t-1} + eps)",
     "log-rate change per day", "topic x platform x day", "up", False,
     "First difference of the log rate. Noisy at low volume; read alongside "
     "growth_7d rather than alone.",
     "support_ok and V_obs >= 3"),
    ("growth_share", "Growth share", "growth",
     "cell growth / sum of positive growth across cells that day",
     "share (0-1)", "topic x platform x day", "up", False,
     "Which cells account for the day's total movement. Undefined on days with "
     "no positive growth anywhere.",
     "support_ok"),
    ("growth_7d", "Growth (7-day)", "growth",
     "mean(g) over trailing 7 days", "log-rate change per day",
     "topic x platform x day", "up", False,
     "Smoothed growth. Lags a real turn by roughly half the window.",
     "support_ok and >= 7 trailing days"),
    ("accel", "Acceleration (raw)", "acceleration",
     "a_t = g_t - g_{t-1}  (2nd difference of the effort-normalised log rate)",
     "log-rate change per day^2", "topic x platform x day", "up", False,
     "Second differences double the noise. Diagnostic only -- accel_smooth is "
     "the publishable form.",
     "support_ok and V_obs >= 3"),
    ("accel_smooth", "Acceleration (smoothed)", "acceleration",
     "Savitzky-Golay 2nd derivative, window 5, polynomial order 2, on the "
     "effort-normalised log rate",
     "log-rate change per day^2", "topic x platform x day", "up", False,
     "The publishable acceleration. Savitzky-Golay needs 5 consecutive days, so "
     "the first and last two days of any series are NULL.",
     "support_ok and >= 5 consecutive days"),
    ("momentum", "Momentum", "growth",
     "EMA_7(log V_rate) - EMA_21(log V_rate)", "log-rate difference",
     "topic x platform x day", "up", False,
     "Trend-vs-baseline separation. Warms up over ~21 days, so early-window "
     "values are biased toward zero.",
     "support_ok and >= 21 trailing days"),
    ("surprise", "Surprise", "intensity",
     "-log10 P(X >= V_obs | X ~ NegBinom(mu_cp, theta_cp)) fitted on a trailing "
     "window per topic x platform; Poisson fallback when theta is unidentifiable",
     "-log10 p (bits of evidence)", "topic x platform x day", "up", True,
     "Replaces v1 robust-z, which reached |z| = 143 on cells of 2 posts. Bounded "
     "and correctly unimpressed by V = 1 against a small baseline. Not a "
     "severity: a huge surprise on a tiny baseline is still a small event.",
     "baseline_days >= 14 and baseline_mean >= 0.5 and V_obs >= 3"),
    ("surprise_p", "Surprise p-value", "intensity",
     "P(X >= V_obs | baseline)", "probability", "topic x platform x day",
     "down", False,
     "Uncorrected. Hundreds of cells are tested daily, so this p-value on its "
     "own would produce false alarms by construction -- use surprise_q.",
     "same as surprise"),
    ("surprise_q", "Surprise q-value (BH-FDR)", "intensity",
     "Benjamini-Hochberg step-up on all supported cells within the same day",
     "q-value", "topic x platform x day", "down", False,
     "The alerting quantity. q < 0.05 means an expected false-discovery rate of "
     "5% among flagged cells that day -- not a 5% chance this cell is a "
     "false alarm.",
     "same as surprise"),
    ("alert_flag", "Alert flag", "alerting",
     "surprise_q < 0.05 and support_ok", "boolean",
     "topic x platform x day", "up", True,
     "FDR-gated, not a z cutoff. Per spec §6.5 no alert threshold may be a raw "
     "z value.",
     "support_ok"),
    ("burst_state", "Burst state", "acceleration",
     "Viterbi state of a 2-state (baseline / burst) HMM over the count series "
     "(Kleinberg); Poisson CUSUM fallback for short series",
     "0 = baseline, 1 = burst", "topic x platform x day", "up", True,
     "A state, not a slope. Short series can produce a state sequence that "
     "flickers; episodes shorter than 2 days are suppressed.",
     "in_window and >= 14 days of series"),
    ("burst_intensity", "Burst intensity", "acceleration",
     "log-likelihood ratio of the burst state against baseline at day t",
     "log-likelihood ratio", "topic x platform x day", "up", True,
     "Only meaningful while burst_state = 1.",
     "burst_state = 1"),
    ("novelty", "Semantic novelty", "acceleration",
     "1 - cosine(centroid(bge-m3 embeddings of the day's posts), "
     "trailing 14-day topic centroid)",
     "cosine distance (0-1)", "topic x platform x day", "up", True,
     "Catches a NEW SUB-NARRATIVE inside an existing topic -- invisible to "
     "volume alone. Unstable below ~5 posts/day because a day's centroid is "
     "then one or two documents.",
     "V_obs >= 5"),
    ("days_to_threshold", "Days to threshold", "acceleration",
     "days from the cell's first appearance to its first crossing of k = 5 "
     "posts/day", "days", "topic x platform", "down", False,
     "Speed of emergence from zero. NULL for cells that never cross k, which is "
     "most of them -- absence here is not slowness.",
     "cell must cross k at least once"),
    ("emergence_score", "Emergence score", "acceleration",
     "mean of the within-day percentile ranks of surprise, accel_smooth, "
     "novelty and inverse days_to_threshold; then FDR-gated",
     "score (0-1)", "topic x platform x day", "up", True,
     "The single number the notification service should key on for 'a situation "
     "is appearing'. A rank combination, so it is only comparable within a day. "
     "Always inspect the four components before acting.",
     "support_ok and at least 2 of 4 components present"),
    ("tone_mean", "Tone (raw mean)", "tone",
     "mean(sentiment polarity) over the cell's posts, multilingual model",
     "polarity -1..+1", "topic x platform x day", "neutral", False,
     "A cell mean over 1-2 posts is noise. Published value is tone_eb.",
     "tone_n >= 1"),
    ("tone_eb", "Tone (EB-shrunk)", "tone",
     "theta_hat = (n*xbar + k*mu_parent) / (n + k), parent = topic then platform "
     "then global; k from between/within variance",
     "polarity -1..+1", "topic x platform x day", "neutral", False,
     "The publishable tone. Shrinkage means low-n cells sit near their parent "
     "mean by construction -- read tone_n before reading tone_eb.",
     "tone_n >= 1 and parent mean available"),
    ("tone_n", "Tone support", "tone", "count of posts scored for tone",
     "posts", "topic x platform x day", "up", False,
     "Every z-like or p-like metric carries its support count (§6.4).", "none"),
    ("hate_mean", "Hate/toxicity (raw)", "hate",
     "mean(toxicity probability) over the cell's posts", "probability 0-1",
     "topic x platform x day", "up", True,
     "UNCALIBRATED AND UNPUBLISHABLE. A war/terror corpus over-fires every "
     "off-the-shelf toxicity model: factual reporting of violence scores as "
     "toxic. Blocked until reports/calibration_sample.csv is hand-labelled.",
     "BLOCKED - hate_calibrated = false"),
    ("hate_eb", "Hate/toxicity (EB-shrunk)", "hate",
     "empirical-Bayes shrinkage of hate_mean toward the parent mean",
     "probability 0-1", "topic x platform x day", "up", True,
     "UNCALIBRATED. Shrinking an uncalibrated score does not calibrate it.",
     "BLOCKED - hate_calibrated = false"),
    ("hate_prevalence", "Hate/toxicity prevalence", "hate",
     "share of the cell's posts above the model's default threshold",
     "share 0-1", "topic x platform x day", "up", True,
     "UNCALIBRATED. The threshold itself is the untested assumption.",
     "BLOCKED - hate_calibrated = false"),
    ("hate_calibrated", "Hate calibration status", "hate",
     "hand-labelled calibration sample exists and agreement was acceptable",
     "boolean", "run", "up", False,
     "false everywhere until calibration is done. The dashboard must gate the "
     "whole hate family on this flag.",
     "none"),
    ("virality_pct", "Virality percentile", "virality",
     "empirical percentile of engagement within (platform x post-age band)",
     "percentile 0-1", "topic x platform x day", "up", False,
     "Distribution-free and age-honest, because a parametric age normalisation "
     "did not fit. NULL on Reddit, which has no engagement fields at all -- "
     "absent, not zero.",
     "platform has_engagement and >= 20 posts in the age band"),
    ("reach_proxy", "Reach (PROXY)", "virality",
     "views where available, else a weighted engagement composite",
     "proxy units", "topic x platform x day", "up", False,
     "A PROXY. No follower or subscriber counts exist anywhere in the corpus, so "
     "true reach is not measurable. Never present this as audience size.",
     "platform has_engagement"),
    ("reach_audience_norm", "Reach / channel median (PROXY)", "virality",
     "post_views / median(views) for the same channel", "ratio",
     "topic x platform x day", "up", False,
     "Telegram only -- a channel's median views is the only defensible "
     "audience-size estimate available. Still a proxy.",
     "platform = telegram and channel has >= 10 posts"),
    ("author_entropy", "Author entropy", "diversity",
     "Shannon entropy of the author/channel distribution, natural log",
     "nats", "topic x platform x day", "up", False,
     "Bounded above by log(n_authors), so it rises with volume. Read with "
     "n_authors, never alone.",
     "n_authors >= 2"),
    ("author_1_hhi", "Author dispersion (1 - HHI)", "diversity",
     "1 - sum(share_author^2)", "index 0-1", "topic x platform x day", "up",
     False,
     "0 = one author wrote everything. Volume-independent, unlike entropy, "
     "which is why both are shown.",
     "n_authors >= 2"),
    ("n_authors", "Distinct authors", "diversity",
     "count(distinct author or channel)", "authors",
     "topic x platform x day", "up", False,
     "Telegram authorship is the channel, not the person -- 24 channels is the "
     "practical ceiling there.",
     "none"),
    ("coordination_flag", "Coordination flag", "diversity",
     "author_1_hhi < 0.2 and V >= 10 and near-duplicate text share > 0.5",
     "boolean", "topic x platform x day", "up", True,
     "A heuristic screen, not a finding. A single high-volume wire-relay channel "
     "trips it legitimately, so every hit needs a human look.",
     "V >= 10"),
    ("readability_mean", "Readability (Flesch)", "readability",
     "mean Flesch reading-ease over posts long enough to score (>= 40 chars, "
     "Latin script)", "Flesch score", "topic x platform x day", "neutral",
     False,
     "Only defined for scorable posts. YouTube comments have a median length of "
     "59 characters with 36% under 40, so most of the corpus is unscorable and "
     "this mean describes a minority of it.",
     "readability_coverage >= 0.05"),
    ("readability_coverage", "Readability coverage", "readability",
     "scorable posts / total posts in cell", "share 0-1",
     "topic x platform x day", "up", False,
     "The number that decides whether readability_mean means anything. "
     "Corpus-wide it is low.",
     "none"),
    ("support_n", "Support count", "quality",
     "count of posts backing the cell's z-like and p-like measures", "posts",
     "topic x platform x day", "up", False,
     "Carried by every gated metric per §6.4.", "none"),
    ("support_ok", "Support gate", "quality",
     "baseline_days >= 14 and baseline_mean >= 0.5 and V_obs >= 3", "boolean",
     "topic x platform x day", "up", False,
     "When false, gated metrics are NULL rather than a guess (§6.2). 91% of the "
     "v1 panel was V <= 2, which is exactly what this gate exists to stop.",
     "none - this IS the gate"),
    ("ar1_phi", "AR(1) coefficient", "persistence",
     "phi from x_t = c + phi*x_{t-1} + eps, where x is the effort-normalised "
     "log rate with its 14-day local mean removed",
     "coefficient", "topic x platform", "up", False,
     "Replaces post-peak curve fitting, which accepted only 6 of 39 cells. "
     "Fitted on deviations from a local baseline, NOT on the raw first "
     "difference: differencing an already-noisy log rate over-differences it and "
     "drives phi negative, leaving half-life undefined everywhere. "
     "phi >= 1 means non-stationary and half-life is undefined.",
     "n_obs >= 20"),
    ("phi_se", "AR(1) standard error", "persistence",
     "OLS standard error of phi", "coefficient", "topic x platform", "down",
     False, "Read phi with its interval; a phi of 0.6 +/- 0.3 is not a finding.",
     "n_obs >= 20"),
    ("half_life_days", "Half-life", "persistence",
     "ln(0.5) / ln(phi)", "days", "topic x platform", "neutral", False,
     "Defined only for 0 < phi < 1. This is aggregate-level persistence of the "
     "series -- NOT per-post engagement decay, which a single snapshot cannot "
     "measure at all.",
     "0 < ar1_phi < 1"),
    ("volatility", "Volatility", "persistence",
     "sd(log-returns of V_rate)", "log-rate sd", "topic x platform", "down",
     True, "Inflated by zero-inflation: alternating 0 and 1 post looks violent.",
     "n_obs >= 20"),
    ("ljung_box_q", "Ljung-Box Q", "persistence",
     "Ljung-Box portmanteau statistic, lags 1..7", "statistic",
     "topic x platform", "up", False,
     "Tests whether autocorrelation exists at all. If p is large, the AR(1) "
     "coefficient and its half-life describe noise.",
     "n_obs >= 20"),
    ("ljung_box_p", "Ljung-Box p", "persistence",
     "p-value of ljung_box_q", "probability", "topic x platform", "down",
     False,
     "p < 0.05 is the evidence that the series has temporal structure worth "
     "modelling. This replaces the v1 null control, whose stated pass "
     "condition was inverted (a smooth series has LOWER mean|delta| than its "
     "shuffle, not higher).",
     "n_obs >= 20"),
    ("onset_slope", "Onset slope", "acceleration",
     "maximum slope of a logistic/Gompertz fit to the burst's rising limb",
     "log-rate per day", "burst episode", "up", True,
     "How explosively the episode arrived. Fitted on the rising limb only; "
     "check r2 before quoting it.",
     "episode length >= 4 days"),
    ("half_life_days_episode", "Episode half-life", "persistence",
     "ln(2) / decay_lambda from an exponential fit to the episode's falling limb",
     "days", "burst episode", "neutral", False,
     "Scoped to detected episodes rather than whole series, which is why it fits "
     "where the v1 whole-series version did not.",
     "episode has >= 4 post-peak days and r2 >= 0.3"),
    ("severity", "Alert severity", "alerting",
     "ordinal band from surprise_q and emergence_score: critical q < 0.001, "
     "high q < 0.01, medium q < 0.05",
     "critical | high | medium", "alert", "up", True,
     "Severity is evidence strength, not impact. A critical q on a 4-post cell "
     "is strong evidence of a small thing.",
     "support_ok and alert_flag"),
]

META_COLUMNS = ["metric_key", "display_name", "family", "formula", "units",
                "grain", "direction", "higher_is_worse", "caveats",
                "support_gate"]

METRICS_DAILY_COLUMNS = [
    "day", "topic_id", "platform", "in_window",
    "V", "V_rate", "effort",
    "growth", "growth_share", "growth_7d",
    "accel", "accel_smooth", "momentum",
    "surprise", "surprise_p", "surprise_q", "alert_flag",
    "burst_state", "burst_intensity", "burst_episode_id",
    "novelty", "days_to_threshold", "emergence_score",
    "tone_mean", "tone_eb", "tone_n",
    "hate_mean", "hate_eb", "hate_prevalence", "hate_calibrated",
    "virality_pct", "reach_proxy", "reach_audience_norm",
    "author_entropy", "author_1_hhi", "n_authors", "coordination_flag",
    "readability_mean", "readability_coverage",
    "support_n", "support_ok",
]


# --------------------------------------------------------------------------
# small statistical helpers (no scipy dependency)
# --------------------------------------------------------------------------

def nb_sf(k: int, mu: float, theta: float) -> float:
    """P(X >= k) for X ~ NegBinom(mean=mu, dispersion theta). Poisson as theta->inf."""
    if k <= 0:
        return 1.0
    mu = max(mu, 1e-9)
    if theta is None or theta > 1e6 or not np.isfinite(theta):
        # Poisson survival
        total, term = 0.0, math.exp(-mu)
        for i in range(0, k):
            total += term
            term *= mu / (i + 1)
        return max(min(1.0 - total, 1.0), 1e-300)
    p = theta / (theta + mu)
    # pmf(0)
    log_pmf = theta * math.log(p)
    cdf = math.exp(log_pmf)
    for i in range(1, k):
        log_pmf += math.log((theta + i - 1) / i) + math.log(1 - p)
        cdf += math.exp(log_pmf)
        if cdf >= 1.0:
            break
    return max(min(1.0 - cdf, 1.0), 1e-300)


def bh_qvalues(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg step-up. NaN in -> NaN out."""
    q = np.full(p.shape, np.nan)
    idx = np.where(np.isfinite(p))[0]
    if idx.size == 0:
        return q
    vals = p[idx]
    order = np.argsort(vals)
    m = vals.size
    ranked = vals[order]
    adj = ranked * m / (np.arange(1, m + 1))
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(adj, 0, 1)
    q[idx] = out
    return q


def savgol_second_derivative(y: np.ndarray) -> np.ndarray:
    """Savitzky-Golay window-5 order-2 second derivative. Ends are NaN."""
    n = y.size
    out = np.full(n, np.nan)
    # For window 5, order 2 the quadratic coefficient kernel is
    # (2, -1, -2, -1, 2)/7; the 2nd derivative is 2*a2.
    k = np.array([2.0, -1.0, -2.0, -1.0, 2.0]) / 7.0
    for i in range(2, n - 2):
        seg = y[i - 2:i + 3]
        if np.all(np.isfinite(seg)):
            out[i] = 2.0 * float(np.dot(k, seg))
    return out


def ema(x: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1.0)
    out = np.empty_like(x, dtype=float)
    acc = x[0] if np.isfinite(x[0]) else 0.0
    for i, v in enumerate(x):
        if np.isfinite(v):
            acc = a * v + (1 - a) * acc
        out[i] = acc
    return out


def _nanmean_rows(stack: np.ndarray) -> np.ndarray:
    """Column-wise nanmean without the all-NaN RuntimeWarning."""
    mask = np.isfinite(stack)
    cnt = mask.sum(axis=0)
    tot = np.where(mask, stack, 0.0).sum(axis=0)
    return np.where(cnt > 0, tot / np.maximum(cnt, 1), np.nan)


def ranks01(x: np.ndarray) -> np.ndarray:
    """Within-array percentile ranks in [0,1]; NaN preserved."""
    out = np.full(x.shape, np.nan)
    idx = np.where(np.isfinite(x))[0]
    if idx.size == 0:
        return out
    v = x[idx]
    order = np.argsort(v)
    r = np.empty(v.size)
    r[order] = np.arange(v.size)
    out[idx] = r / max(v.size - 1, 1)
    return out


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------

def build_fixture() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(SEED)

    topic_ids = [t[0] for t in TOPICS]
    platforms = [p[0] for p in PLATFORMS]
    has_eng = {p[0]: p[1] for p in PLATFORMS}

    window_days = pd.date_range(WINDOW_START, WINDOW_END, freq="D").date.tolist()
    backfill_days = pd.date_range(BACKFILL_START,
                                  WINDOW_START - timedelta(days=1),
                                  freq="D").date.tolist()

    # ---- effort: platform x day, with the Telegram collector ramp ----------
    effort_base = {"telegram": 18, "youtube": 9, "reddit": 12, "linkedin": 5,
                   "instagram": 4, "facebook": 6}
    effort_end = {"telegram": 24, "youtube": 11, "reddit": 13, "linkedin": 6,
                  "instagram": 4, "facebook": 6}
    effort = {}
    all_days = backfill_days + window_days
    for p in platforms:
        for i, d in enumerate(all_days):
            if d < WINDOW_START:
                # pre-window: only youtube backfill is meaningful
                effort[(p, d)] = effort_base[p] if p == "youtube" else 0
                continue
            frac = (d - WINDOW_START).days / max((WINDOW_END - WINDOW_START).days, 1)
            base = effort_base[p] + (effort_end[p] - effort_base[p]) * frac
            effort[(p, d)] = max(1, int(round(base + rng.normal(0, 0.5))))

    # ---- per-cell base rate ------------------------------------------------
    platform_weight = {"telegram": 1.0, "youtube": 0.22, "reddit": 0.30,
                       "linkedin": 0.06, "instagram": 0.04, "facebook": 0.05}
    topic_weight = {"t000": 0.09, "t001": 0.08, "t002": 0.11, "t003": 0.22,
                    "t004": 0.10, "t005": 0.05, "t006": 0.05, "t007": 0.42,
                    "unassigned": 0.20}

    # planted emergence events: (topic, platform, onset day offset, amplitude, width)
    events = [
        ("t007", "telegram", 22, 3.1, 5.0),
        ("t001", "reddit", 30, 2.6, 3.5),
        ("t003", "telegram", 12, 1.9, 6.0),
        ("t006", "youtube", 34, 2.2, 3.0),
        ("t004", "telegram", 37, 2.9, 2.5),
        ("t002", "linkedin", 26, 1.7, 4.0),
    ]
    event_map: dict[tuple[str, str], list] = {}
    for t, p, off, amp, wid in events:
        event_map.setdefault((t, p), []).append((off, amp, wid))

    rows = []
    theta_by_cell: dict[tuple[str, str], float] = {}

    for t in topic_ids:
        for p in platforms:
            lam0 = 52.0 * topic_weight[t] * platform_weight[p]
            lam0 *= float(rng.uniform(0.55, 1.6))
            theta = float(rng.uniform(1.6, 6.0))
            theta_by_cell[(t, p)] = theta

            # --- counts -------------------------------------------------
            days = all_days if p == "youtube" else window_days
            n = len(days)
            mu = np.empty(n)
            for i, d in enumerate(days):
                weekly = 1.0 + 0.16 * math.sin(2 * math.pi * (d.toordinal() % 7) / 7.0)
                eff = max(effort[(p, d)], 1)
                m = lam0 * weekly * (eff / max(effort_base[p], 1))
                if d < WINDOW_START:
                    m *= 0.30          # thin backfill
                for off, amp, wid in event_map.get((t, p), []):
                    if d >= WINDOW_START:
                        x = ((d - WINDOW_START).days - off) / wid
                        m *= 1.0 + amp * math.exp(-0.5 * x * x)
                mu[i] = m
            # NegBinom draw
            shape = theta
            gam = rng.gamma(shape, mu / shape)
            V = rng.poisson(gam).astype(float)

            eff_arr = np.array([max(effort[(p, d)], 1) for d in days], dtype=float)
            in_win = np.array([d >= WINDOW_START for d in days])
            V_rate = V / eff_arr
            logr = np.log(V_rate + 1e-3)

            # --- growth family (in-window only) -------------------------
            growth = np.full(n, np.nan)
            growth[1:] = np.diff(logr)
            growth[~in_win] = np.nan
            g7 = pd.Series(growth).rolling(7, min_periods=7).mean().to_numpy()
            accel = np.full(n, np.nan)
            accel[1:] = np.diff(growth)
            logr_win = np.where(in_win, logr, np.nan)
            accel_s = savgol_second_derivative(logr_win)
            mom = ema(logr_win, 7) - ema(logr_win, 21)
            mom[~in_win] = np.nan
            mom[:21] = np.where(in_win[:21], mom[:21] * 0.4, np.nan)

            # --- baseline / surprise ------------------------------------
            surprise = np.full(n, np.nan)
            surprise_p = np.full(n, np.nan)
            support_ok = np.zeros(n, dtype=bool)
            for i in range(n):
                lo = max(0, i - 21)
                hist = V[lo:i]
                hist_in = in_win[lo:i]
                hist = hist[hist_in] if in_win[i] else hist
                bdays = hist.size
                bmean = float(hist.mean()) if bdays else 0.0
                ok = bool(in_win[i] and bdays >= 14 and bmean >= 0.5 and V[i] >= 3)
                support_ok[i] = ok
                if ok:
                    pv = nb_sf(int(V[i]), max(bmean, 1e-6), theta)
                    surprise_p[i] = pv
                    surprise[i] = -math.log10(max(pv, 1e-300))

            # --- novelty ------------------------------------------------
            nov = np.full(n, np.nan)
            for i in range(n):
                if V[i] >= 5 and in_win[i]:
                    base = 0.13 + 0.04 * math.sin(i / 5.0)
                    bump = 0.0
                    for off, amp, wid in event_map.get((t, p), []):
                        x = ((days[i] - WINDOW_START).days - off) / (wid * 1.4)
                        bump += 0.22 * (amp / 3.0) * math.exp(-0.5 * x * x)
                    nov[i] = float(np.clip(base + bump + rng.normal(0, 0.022), 0, 1))

            # --- days_to_threshold --------------------------------------
            dtt = np.full(n, np.nan)
            crossed = np.where((V >= 5) & in_win)[0]
            first = np.where(in_win & (V > 0))[0]
            if crossed.size and first.size:
                val = float((days[crossed[0]] - days[first[0]]).days)
                dtt[crossed[0]:] = np.where(in_win[crossed[0]:], val, np.nan)

            # --- burst state --------------------------------------------
            sm = pd.Series(np.where(in_win, V, np.nan)).rolling(3, min_periods=1).mean().to_numpy()
            bl = pd.Series(np.where(in_win, V, np.nan)).rolling(21, min_periods=7).median().to_numpy()
            ratio = sm / np.maximum(bl, 0.5)
            bstate = ((ratio > 1.9) & in_win & (V >= 4)).astype(float)
            bstate[~in_win] = np.nan
            bint = np.where(bstate == 1, np.log(np.maximum(ratio, 1.01)) * V, np.nan)

            # --- tone / hate --------------------------------------------
            topic_tone = {"t000": -0.42, "t001": -0.18, "t002": -0.05,
                          "t003": -0.38, "t004": -0.12, "t005": 0.31,
                          "t006": -0.26, "t007": -0.47, "unassigned": -0.08}[t]
            tone_n = np.where(V > 0, np.maximum(1, (V * 0.92).astype(int)), 0).astype(float)
            tone_raw = np.where(tone_n > 0,
                                topic_tone + rng.normal(0, 0.55 / np.sqrt(np.maximum(tone_n, 1))),
                                np.nan)
            tone_raw = np.clip(tone_raw, -1, 1)
            hate_raw = np.where(tone_n > 0,
                                np.clip(0.24 - 0.30 * topic_tone
                                        + rng.normal(0, 0.10 / np.sqrt(np.maximum(tone_n, 1))),
                                        0, 1),
                                np.nan)
            hate_prev = np.where(np.isfinite(hate_raw),
                                 np.clip(hate_raw * 1.5 + rng.normal(0, 0.04, n), 0, 1),
                                 np.nan)

            # --- engagement ---------------------------------------------
            if has_eng[p]:
                vir = np.where(V >= 3, np.clip(rng.beta(2.1, 2.6, n)
                                               + 0.10 * (bstate == 1), 0, 1), np.nan)
                reach = np.where(V > 0,
                                 V * rng.lognormal(9.2 if p == "telegram" else 6.4,
                                                   0.85, n), np.nan)
                if p == "telegram":
                    ranorm = np.where(V > 0, np.clip(rng.lognormal(0.0, 0.6, n), 0, 12), np.nan)
                else:
                    ranorm = np.full(n, np.nan)
            else:
                vir = np.full(n, np.nan)
                reach = np.full(n, np.nan)
                ranorm = np.full(n, np.nan)

            # --- authorship ---------------------------------------------
            ceiling = {"telegram": 24, "youtube": 40, "reddit": 120,
                       "linkedin": 30, "instagram": 25, "facebook": 22}[p]
            n_auth = np.minimum(np.maximum(1, (V * rng.uniform(0.35, 0.8, n)).astype(int)),
                                ceiling).astype(float)
            n_auth = np.where(V > 0, np.maximum(n_auth, 1), 0)
            safe_auth = np.maximum(n_auth, 2.0)
            hhi = np.where(n_auth >= 2,
                           np.clip(1 - (1.0 / safe_auth) - rng.uniform(0, 0.22, n), 0, 1),
                           np.nan)
            ent = np.where(n_auth >= 2,
                           np.log(np.maximum(n_auth, 2)) * rng.uniform(0.62, 0.97, n),
                           np.nan)
            coord = np.where(V >= 10,
                             (np.nan_to_num(hhi, nan=1.0) < 0.20) & (rng.random(n) < 0.35),
                             False)

            # --- readability --------------------------------------------
            cov_base = {"telegram": 0.16, "youtube": 0.11, "reddit": 0.34,
                        "linkedin": 0.51, "instagram": 0.23, "facebook": 0.29}[p]
            cov = np.where(V > 0, np.clip(rng.normal(cov_base, 0.05, n), 0.0, 1.0), np.nan)
            read = np.where((np.nan_to_num(cov) >= 0.05) & (V > 0),
                            np.clip(rng.normal(46, 12, n), 0, 100), np.nan)

            for i, d in enumerate(days):
                rows.append({
                    "day": d, "topic_id": t, "platform": p,
                    "in_window": bool(in_win[i]),
                    "V": float(V[i]), "V_rate": float(V_rate[i]),
                    "effort": float(eff_arr[i]),
                    "growth": growth[i], "growth_share": np.nan,
                    "growth_7d": g7[i],
                    "accel": accel[i], "accel_smooth": accel_s[i],
                    "momentum": mom[i],
                    "surprise": surprise[i], "surprise_p": surprise_p[i],
                    "surprise_q": np.nan, "alert_flag": False,
                    "burst_state": bstate[i], "burst_intensity": bint[i],
                    "burst_episode_id": None,
                    "novelty": nov[i], "days_to_threshold": dtt[i],
                    "emergence_score": np.nan,
                    "tone_mean": tone_raw[i], "tone_eb": np.nan,
                    "tone_n": tone_n[i],
                    "hate_mean": hate_raw[i], "hate_eb": np.nan,
                    "hate_prevalence": hate_prev[i], "hate_calibrated": False,
                    "virality_pct": vir[i], "reach_proxy": reach[i],
                    "reach_audience_norm": ranorm[i],
                    "author_entropy": ent[i], "author_1_hhi": hhi[i],
                    "n_authors": float(n_auth[i]),
                    "coordination_flag": bool(coord[i]),
                    "readability_mean": read[i],
                    "readability_coverage": cov[i],
                    "support_n": float(V[i]), "support_ok": bool(support_ok[i]),
                })

    md = pd.DataFrame(rows)

    # ---- gate: support_ok = false => NULL, not a guess (§6.2) -------------
    gated = ["growth", "growth_7d", "accel", "accel_smooth", "momentum",
             "surprise", "surprise_p"]
    for c in gated:
        md.loc[~md["support_ok"], c] = np.nan

    # ---- BH-FDR per day --------------------------------------------------
    md["surprise_q"] = np.nan
    for d, grp in md.groupby("day", sort=False):
        md.loc[grp.index, "surprise_q"] = bh_qvalues(grp["surprise_p"].to_numpy(float))
    md["alert_flag"] = (md["surprise_q"] < 0.05) & md["support_ok"]

    # ---- growth_share per day -------------------------------------------
    for d, grp in md.groupby("day", sort=False):
        pos = grp["growth"].clip(lower=0)
        tot = float(pos.sum())
        md.loc[grp.index, "growth_share"] = (pos / tot) if tot > 0 else np.nan

    # ---- emergence score: rank-combination within day -------------------
    md["emergence_score"] = np.nan
    for d, grp in md.groupby("day", sort=False):
        comp = np.vstack([
            ranks01(grp["surprise"].to_numpy(float)),
            ranks01(grp["accel_smooth"].to_numpy(float)),
            ranks01(grp["novelty"].to_numpy(float)),
            ranks01(-grp["days_to_threshold"].to_numpy(float)),
        ])
        present = np.isfinite(comp).sum(axis=0)
        score = _nanmean_rows(comp)
        score = np.where(present >= 2, score, np.nan)
        score = np.where(grp["support_ok"].to_numpy(), score, np.nan)
        md.loc[grp.index, "emergence_score"] = score

    # ---- EB shrinkage for tone & hate (F5) -------------------------------
    def eb_shrink(col: str, out: str, k: float) -> None:
        g = md.groupby("topic_id")[col].transform("mean")
        gp = md.groupby("platform")[col].transform("mean")
        parent = g.fillna(gp).fillna(md[col].mean())
        n = md["tone_n"].fillna(0)
        md[out] = np.where(n > 0, (n * md[col].fillna(parent) + k * parent) / (n + k), np.nan)

    eb_shrink("tone_mean", "tone_eb", 6.0)
    eb_shrink("hate_mean", "hate_eb", 6.0)

    md = md[METRICS_DAILY_COLUMNS]

    # ---- burst episodes --------------------------------------------------
    episodes = []
    ep_assign: dict[tuple, str] = {}
    for (t, p), grp in md[md["in_window"]].groupby(["topic_id", "platform"], sort=False):
        grp = grp.sort_values("day").reset_index(drop=True)
        st = grp["burst_state"].fillna(0).to_numpy()
        i = 0
        while i < len(st):
            if st[i] == 1:
                j = i
                while j + 1 < len(st) and st[j + 1] == 1:
                    j += 1
                if j - i + 1 >= 2:
                    seg = grp.iloc[i:j + 1]
                    ep_id = f"e{len(episodes):03d}"
                    peak_i = int(seg["V"].to_numpy().argmax())
                    peak_day = seg["day"].iloc[peak_i]
                    rise = seg["V"].to_numpy()[:peak_i + 1]
                    onset = float(np.diff(np.log(rise + 1)).max()) if rise.size > 1 else np.nan
                    fall = seg["V"].to_numpy()[peak_i:]
                    if fall.size >= 3 and fall[-1] > 0:
                        lam = float(max((math.log(fall[0] + 1) - math.log(fall[-1] + 1))
                                        / max(fall.size - 1, 1), 1e-3))
                        r2 = float(rng.uniform(0.34, 0.88))
                        hl = math.log(2) / lam
                    else:
                        lam, r2, hl = np.nan, np.nan, np.nan
                    episodes.append({
                        "episode_id": ep_id, "topic_id": t, "platform": p,
                        "start_day": seg["day"].iloc[0], "peak_day": peak_day,
                        "end_day": seg["day"].iloc[-1],
                        "peak_V": float(seg["V"].max()),
                        "total_V": float(seg["V"].sum()),
                        "onset_slope": onset,
                        "inflection_day": seg["day"].iloc[max(peak_i - 1, 0)],
                        "decay_lambda": lam, "half_life_days": hl, "r2": r2,
                        "level": int(2 if seg["V"].max() > grp["V"].median() * 3 else 1),
                    })
                    for d in seg["day"]:
                        ep_assign[(d, t, p)] = ep_id
                i = j + 1
            else:
                i += 1
    be = pd.DataFrame(episodes, columns=[
        "episode_id", "topic_id", "platform", "start_day", "peak_day", "end_day",
        "peak_V", "total_V", "onset_slope", "inflection_day", "decay_lambda",
        "half_life_days", "r2", "level"])

    md["burst_episode_id"] = [
        ep_assign.get((d, t, p)) for d, t, p in
        zip(md["day"], md["topic_id"], md["platform"])
    ]

    # ---- metrics_topic_day: platform rolled up ---------------------------
    w = md[md["in_window"] | (md["platform"] == "youtube")].copy()
    agg = {
        "in_window": "max", "V": "sum", "effort": "sum",
        "tone_n": "sum", "support_n": "sum", "n_authors": "sum",
    }
    mean_cols = ["growth", "growth_share", "growth_7d", "accel", "accel_smooth",
                 "momentum", "surprise", "surprise_p", "surprise_q", "novelty",
                 "days_to_threshold", "emergence_score", "tone_mean", "tone_eb",
                 "hate_mean", "hate_eb", "hate_prevalence", "virality_pct",
                 "reach_audience_norm", "author_entropy", "author_1_hhi",
                 "readability_mean", "readability_coverage", "burst_intensity"]
    for c in mean_cols:
        agg[c] = "mean"
    agg["reach_proxy"] = "sum"
    agg["burst_state"] = "max"
    agg["alert_flag"] = "max"
    agg["coordination_flag"] = "max"
    agg["support_ok"] = "max"
    mtd = w.groupby(["day", "topic_id"], as_index=False).agg(agg)
    mtd["V_rate"] = mtd["V"] / mtd["effort"].replace(0, np.nan)
    mtd["hate_calibrated"] = False
    mtd["burst_episode_id"] = None
    mtd = mtd[[c for c in METRICS_DAILY_COLUMNS if c != "platform"]]

    # ---- persistence -----------------------------------------------------
    pers = []
    for (t, p), grp in md[md["in_window"]].groupby(["topic_id", "platform"], sort=False):
        # F7 uses the WHOLE in-window series, not the support-gated growth column:
        # AR(1) with a standard error is what replaces per-cell curve fitting.
        vr = grp.sort_values("day")["V_rate"].to_numpy(float)
        lr = np.log(vr + 1e-3)
        # Deviations from a 14-day local mean. Differencing the log rate directly
        # over-differences a noisy series and drives phi negative, which would
        # leave half_life undefined everywhere -- see ar1_phi's caveat.
        loc = pd.Series(lr).rolling(14, min_periods=4).mean().to_numpy()
        x = lr - loc
        x = x[np.isfinite(x)]
        if x.size < 20:
            continue
        x0, x1 = x[:-1], x[1:]
        denom = float(((x0 - x0.mean()) ** 2).sum())
        phi = float(((x0 - x0.mean()) * (x1 - x1.mean())).sum() / denom) if denom > 0 else np.nan
        resid = x1 - (x1.mean() + phi * (x0 - x0.mean()))
        se = float(math.sqrt(max((resid ** 2).sum() / max(x1.size - 2, 1), 0) / max(denom, 1e-9)))
        hl = math.log(0.5) / math.log(phi) if (np.isfinite(phi) and 0 < phi < 1) else np.nan
        acf = []
        for lag in range(1, 8):
            if x.size > lag + 2:
                a, b = x[:-lag], x[lag:]
                da, db = a - a.mean(), b - b.mean()
                dd = math.sqrt(float((da ** 2).sum()) * float((db ** 2).sum()))
                acf.append(float((da * db).sum() / dd) if dd > 0 else 0.0)
        nn = x.size
        Q = nn * (nn + 2) * sum(r * r / max(nn - i - 1, 1) for i, r in enumerate(acf))
        # chi2 survival with df=len(acf), Wilson-Hilferty approximation
        df = max(len(acf), 1)
        z = ((Q / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
        pval = 0.5 * math.erfc(z / math.sqrt(2))
        pers.append({
            "topic_id": t, "platform": p, "ar1_phi": phi, "phi_se": se,
            "half_life_days": hl,
            "volatility": float(np.nanstd(x)),
            "ljung_box_q": float(Q), "ljung_box_p": float(min(max(pval, 0), 1)),
            "n_obs": int(nn),
        })
    pf = pd.DataFrame(pers, columns=["topic_id", "platform", "ar1_phi", "phi_se",
                                     "half_life_days", "volatility",
                                     "ljung_box_q", "ljung_box_p", "n_obs"])

    # ---- tag metrics -----------------------------------------------------
    tag_rows = []
    tag_platforms = ["telegram", "linkedin", "instagram", "reddit", "youtube", "facebook"]
    tag_pw = {"telegram": 0.55, "linkedin": 0.18, "instagram": 0.10,
              "reddit": 0.06, "youtube": 0.08, "facebook": 0.03}
    for ti, tag in enumerate(TAGS):
        tag_id = f"g{ti:03d}"
        strength = float(rng.uniform(0.4, 4.2))
        for p in tag_platforms:
            lam = 9.0 * strength * tag_pw[p]
            if lam < 0.35:
                continue
            n = len(window_days)
            mu_t = lam * (1 + 0.3 * np.sin(np.arange(n) / 6.0 + ti))
            if ti % 7 == 0:
                mu_t = mu_t * (1 + 2.4 * np.exp(-0.5 * ((np.arange(n) - 33) / 4.0) ** 2))
            Vt = rng.poisson(np.maximum(mu_t, 0.02)).astype(float)
            eff_t = np.array([max(effort[(p, d)], 1) for d in window_days], float)
            vr = Vt / eff_t
            lg = np.log(vr + 1e-3)
            gr = np.concatenate([[np.nan], np.diff(lg)])
            acs = savgol_second_derivative(lg)
            sup_ok = Vt >= 3
            sp = np.full(n, np.nan)
            for i in range(n):
                if sup_ok[i] and i >= 14:
                    bm = float(Vt[max(0, i - 21):i].mean())
                    if bm >= 0.5:
                        sp[i] = nb_sf(int(Vt[i]), bm, 2.5)
                    else:
                        sup_ok[i] = False
                else:
                    sup_ok[i] = False
            for i, d in enumerate(window_days):
                tag_rows.append({
                    "day": d, "tag_id": tag_id, "platform": p, "V": float(Vt[i]),
                    "V_rate": float(vr[i]),
                    "growth": gr[i] if sup_ok[i] else np.nan,
                    "accel_smooth": acs[i] if sup_ok[i] else np.nan,
                    "surprise": (-math.log10(max(sp[i], 1e-300))
                                 if np.isfinite(sp[i]) else np.nan),
                    "surprise_q": np.nan, "alert_flag": False,
                    "tone_eb": float(np.clip(rng.normal(-0.2, 0.28), -1, 1)),
                    "hate_eb": float(np.clip(rng.normal(0.3, 0.12), 0, 1)),
                    "emergence_score": np.nan,
                    "support_n": float(Vt[i]), "support_ok": bool(sup_ok[i]),
                })
    tm = pd.DataFrame(tag_rows)
    tm["surprise_p"] = np.where(np.isfinite(tm["surprise"]),
                                10 ** (-tm["surprise"].fillna(0)), np.nan)
    for d, grp in tm.groupby("day", sort=False):
        q = bh_qvalues(grp["surprise_p"].to_numpy(float))
        tm.loc[grp.index, "surprise_q"] = q
        tagscore = _nanmean_rows(np.vstack([
            ranks01(grp["surprise"].to_numpy(float)),
            ranks01(grp["accel_smooth"].to_numpy(float))]))
        tm.loc[grp.index, "emergence_score"] = np.where(
            grp["support_ok"].to_numpy(), tagscore, np.nan)
    tm["alert_flag"] = (tm["surprise_q"] < 0.05) & tm["support_ok"]
    tm = tm[["day", "tag_id", "platform", "V", "V_rate", "growth", "accel_smooth",
             "surprise", "surprise_q", "alert_flag", "tone_eb", "hate_eb",
             "emergence_score", "support_n", "support_ok"]]

    # ---- tag_dim ---------------------------------------------------------
    tsum = tm.groupby("tag_id").agg(n_posts=("V", "sum"),
                                    first_seen=("day", "min"),
                                    last_seen=("day", "max")).reset_index()
    tplat = tm[tm["V"] > 0].groupby("tag_id")["platform"].apply(
        lambda s: ",".join(sorted(set(s)))).reset_index(name="platforms")
    tag_dim = tsum.merge(tplat, on="tag_id", how="left")
    tag_dim["tag"] = [TAGS[int(i[1:])] for i in tag_dim["tag_id"]]
    tag_dim = tag_dim[["tag_id", "tag", "n_posts", "platforms", "first_seen", "last_seen"]]

    # ---- alert candidates -------------------------------------------------
    def severity(q: float) -> str:
        if q < 0.001:
            return "critical"
        if q < 0.01:
            return "high"
        return "medium"

    ac = []
    flagged = md[md["alert_flag"].fillna(False)].copy()
    for _, r in flagged.iterrows():
        ac.append({
            "day": r["day"], "topic_id": r["topic_id"], "platform": r["platform"],
            "tag_id": None, "metric_key": "surprise", "value": float(r["surprise"]),
            "surprise_q": float(r["surprise_q"]), "severity": severity(r["surprise_q"]),
            "rationale": (
                f"V={int(r['V'])} on {r['platform']} vs trailing NegBinom baseline; "
                f"surprise {r['surprise']:.2f} (-log10 p), BH q={r['surprise_q']:.2e} "
                f"across {int(flagged[flagged['day'] == r['day']].shape[0])} flagged "
                f"cells that day; support n={int(r['support_n'])}."),
        })
    emg = md[(md["emergence_score"] > 0.80) & md["alert_flag"].fillna(False)]
    for _, r in emg.iterrows():
        parts = []
        if np.isfinite(r["accel_smooth"]):
            parts.append(f"accel_smooth {r['accel_smooth']:+.3f}")
        if np.isfinite(r["novelty"]):
            parts.append(f"novelty {r['novelty']:.2f}")
        if np.isfinite(r["days_to_threshold"]):
            parts.append(f"reached 5/day in {int(r['days_to_threshold'])}d")
        ac.append({
            "day": r["day"], "topic_id": r["topic_id"], "platform": r["platform"],
            "tag_id": None, "metric_key": "emergence_score",
            "value": float(r["emergence_score"]),
            "surprise_q": float(r["surprise_q"]),
            "severity": severity(r["surprise_q"]),
            "rationale": ("Rank-composite emergence "
                          f"{r['emergence_score']:.2f}: " + "; ".join(parts)
                          + f". FDR-gated at q={r['surprise_q']:.2e}."),
        })
    tflag = tm[tm["alert_flag"].fillna(False) & (tm["surprise_q"] < 0.01)]
    tagname = dict(zip(tag_dim["tag_id"], tag_dim["tag"]))
    for _, r in tflag.iterrows():
        ac.append({
            "day": r["day"], "topic_id": None, "platform": r["platform"],
            "tag_id": r["tag_id"], "metric_key": "surprise",
            "value": float(r["surprise"]), "surprise_q": float(r["surprise_q"]),
            "severity": severity(r["surprise_q"]),
            "rationale": (f"#{tagname.get(r['tag_id'], r['tag_id'])} on "
                          f"{r['platform']}: V={int(r['V'])}, surprise "
                          f"{r['surprise']:.2f}, q={r['surprise_q']:.2e}. "
                          "HASHTAG GRAIN -- covers ~15% of corpus (§4); "
                          "secondary facet only."),
        })
    acf_df = pd.DataFrame(ac, columns=["day", "topic_id", "platform", "tag_id",
                                       "metric_key", "value", "surprise_q",
                                       "severity", "rationale"])

    # ---- topic_dim -------------------------------------------------------
    tvol = md.groupby("topic_id")["V"].sum()
    topic_dim = pd.DataFrame([
        {"topic_id": t, "domain": dom, "topic": top, "label": lab,
         "n_posts": int(tvol.get(t, 0)), "description": desc}
        for t, dom, top, lab, desc in TOPICS
    ])

    platform_dim = pd.DataFrame(PLATFORMS,
                                columns=["platform", "has_engagement",
                                         "in_ts_scope", "notes"])
    metric_meta = pd.DataFrame(_META, columns=META_COLUMNS)

    run_meta = pd.DataFrame([{
        "run_id": f"fixture-{SEED}",
        "created_at": datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc),
        "source_extract_id": "FIXTURE - no extract; synthetic data",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "params_json": json.dumps({
            "seed": SEED,
            "source": "UI/fixture.py",
            "baseline_window_days": 21,
            "support_gate": {"baseline_days": 14, "baseline_mean": 0.5, "V_obs": 3},
            "fdr": {"method": "benjamini-hochberg", "alpha": 0.05,
                    "scope": "per day across supported cells"},
            "savgol": {"window": 5, "polyorder": 2},
            "ema_spans": [7, 21],
            "eb_k": 6.0,
            "threshold_k_posts_per_day": 5,
            "hate_calibrated": False,
        }, indent=None),
    }])

    return {
        "topic_dim": topic_dim,
        "tag_dim": tag_dim,
        "platform_dim": platform_dim,
        "metric_meta": metric_meta,
        "run_meta": run_meta,
        "metrics_daily": md,
        "metrics_topic_day": mtd,
        "tag_metrics_daily": tm,
        "burst_episodes": be,
        "persistence": pf,
        "alert_candidates": acf_df,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit-duckdb", metavar="PATH",
                    help="write the fixture to a DuckDB file (for testing the "
                         "DuckDB read path)")
    args = ap.parse_args()
    tables = build_fixture()
    for name, df in tables.items():
        print(f"{name:22s} {len(df):7,d} rows  x {len(df.columns):2d} cols")
    if args.emit_duckdb:
        import duckdb
        import os
        if os.path.exists(args.emit_duckdb):
            os.remove(args.emit_duckdb)
        con = duckdb.connect(args.emit_duckdb)
        for name, df in tables.items():
            con.register("_t", df)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM _t")
            con.unregister("_t")
        con.close()
        print(f"\nwrote {args.emit_duckdb}")


if __name__ == "__main__":
    main()
