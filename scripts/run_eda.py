#!/usr/bin/env python3
"""
Detailed EDA for ShaktiDB social-media Postgres database (shaktidb_social).
Connects once, freezes a REPEATABLE READ snapshot so all numbers in the report
are mutually consistent despite the DB being actively ingested in real time.
Writes:
  - /home/hariramanan/NTRO/EDA/reports/EDA_Report.html
  - /home/hariramanan/NTRO/EDA/reports/KEY_FINDINGS.md
  - /home/hariramanan/NTRO/EDA/reports/figures/*.png
"""
import base64
import io
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import PG

OUT_DIR = "/home/hariramanan/NTRO/EDA/reports"
FIG_DIR = f"{OUT_DIR}/figures"

# ---------------------------------------------------------------- palette --
L = dict(
    surface="#fcfcfb", page="#f9f9f7", ink="#0b0b0b", ink2="#52514e",
    muted="#898781", grid="#e1e0d9", baseline="#c3c2b7",
)
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BLUE = "#2a78d6"
ORANGE = "#eb6834"
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#256abf", "#184f95", "#0d366b"]
DIV = matplotlib.colors.LinearSegmentedColormap.from_list(
    "div_blue_red", ["#184f95", "#6da7ec", "#f0efec", "#f0a39f", "#e34948"]
)

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 11,
    "text.color": L["ink"],
    "axes.edgecolor": L["baseline"],
    "axes.labelcolor": L["ink2"],
    "xtick.color": L["muted"],
    "ytick.color": L["muted"],
    "axes.grid": True,
    "grid.color": L["grid"],
    "grid.linewidth": 0.8,
    "figure.facecolor": L["surface"],
    "axes.facecolor": L["surface"],
    "savefig.facecolor": L["surface"],
})

FIGS = {}  # name -> base64 png


DEVANAGARI_FONT = fm.FontProperties(fname="/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf")


def needs_devanagari(s):
    return any("ऀ" <= ch <= "ॿ" for ch in str(s))


def save_fig(fig, name):
    fig.tight_layout()
    path = f"{FIG_DIR}/{name}.png"
    fig.savefig(path, dpi=150)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    FIGS[name] = base64.b64encode(buf.getvalue()).decode()


def style_ax(ax, hide_x_grid=True):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    if hide_x_grid:
        ax.grid(axis="x", visible=False)
    ax.tick_params(length=0)


# ---------------------------------------------------------------- connect --
conn = psycopg2.connect(**PG)
conn.autocommit = False
cur = conn.cursor()
cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
SNAPSHOT_TS = datetime.now(timezone.utc)


def q(sql, params=None):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall()
    return cols, rows


def q1(sql, params=None):
    cur.execute(sql, params)
    return cur.fetchone()


R = {}  # results dict

TABLES = ["facebook_posts", "instagram_posts", "linkedin_posts", "pinterest_posts",
          "reddit_posts", "snapchat_posts", "telegram_posts", "whatsapp_posts",
          "youtube_comments", "youtube_videos"]

print("=== snapshot row counts ===")
counts = {}
for t in TABLES:
    counts[t] = q1(f'SELECT COUNT(*) FROM "{t}";')[0]
    print(t, counts[t])
R["counts"] = counts

sizes = {}
_, rows = q("""SELECT table_name, pg_total_relation_size('"'||table_name||'"')
  FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';""")
for name, sz in rows:
    sizes[name] = sz
R["sizes"] = sizes

dbsize = q1("SELECT pg_database_size('shaktidb_social');")[0]
R["dbsize"] = dbsize

print("=== unified view platform check ===")
_, rows = q("SELECT DISTINCT platform FROM social_posts_unified;")
R["unified_platforms"] = [r[0] for r in rows]
print(R["unified_platforms"])

# ============================================================ DATA QUALITY
print("=== data quality: nulls ===")
null_pct = {}
for t in TABLES:
    if counts[t] == 0:
        continue
    cur.execute("""SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        AND data_type NOT IN ('USER-DEFINED','tsvector') ORDER BY ordinal_position;""", (t,))
    cols = [r[0] for r in cur.fetchall()]
    exprs = ", ".join(f'ROUND(100.0*SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) AS "{c}"' for c in cols)
    row = q1(f'SELECT {exprs} FROM "{t}";')
    null_pct[t] = dict(zip(cols, [float(x) if x is not None else 0.0 for x in row]))
R["null_pct"] = null_pct

print("=== duplicates on natural keys ===")
dup_checks = {
    "facebook_posts": "post_id", "instagram_posts": "id", "linkedin_posts": "post_url",
    "pinterest_posts": "pin_id", "reddit_posts": "id", "snapchat_posts": "id",
    "telegram_posts": "post_url", "youtube_comments": "comment_id", "youtube_videos": "video_id",
}
# NOTE: telegram_posts.message_id is only unique WITHIN a channel (not globally), so a naive
# uniqueness check on message_id alone produces false-positive "duplicates" across channels.
# post_url is the real natural key here (verified (channel_id,message_id) is 100% unique;
# post_url duplication is a genuine re-ingestion issue: the same message lands twice, once
# via a code path that fails to populate channel_id).
dups = {}
for t, key in dup_checks.items():
    if counts[t] == 0:
        continue
    n = q1(f'SELECT COUNT(*) - COUNT(DISTINCT "{key}") FROM "{t}" WHERE "{key}" IS NOT NULL;')[0]
    dups[t] = n
R["dups"] = dups
print(dups)

print("=== timestamp sanity ===")
ts_checks = {}
for t, tcol in [("facebook_posts", "posted_time"), ("instagram_posts", "posted_at"),
                 ("linkedin_posts", "posted_at"), ("pinterest_posts", "posted_at"),
                 ("reddit_posts", "posted_at"), ("snapchat_posts", "posted_at"),
                 ("telegram_posts", "posted_at"), ("youtube_comments", "published_at"),
                 ("youtube_videos", "video_published_at")]:
    if counts[t] == 0:
        continue
    future = q1(f'SELECT COUNT(*) FROM "{t}" WHERE "{tcol}" > NOW();')[0]
    null_ts = q1(f'SELECT COUNT(*) FROM "{t}" WHERE "{tcol}" IS NULL;')[0]
    mn, mx = q1(f'SELECT MIN("{tcol}"), MAX("{tcol}") FROM "{t}";')
    ts_checks[t] = dict(future=future, null_ts=null_ts, min=str(mn), max=str(mx))
R["ts_checks"] = ts_checks
print(json.dumps(ts_checks, indent=2, default=str))

# lag posted_at -> scraped_at (days), only where both present and scraped>=posted
lag_stats = {}
for t, tcol in [("facebook_posts", "posted_time"), ("instagram_posts", "posted_at"),
                 ("linkedin_posts", "posted_at"), ("pinterest_posts", "posted_at"),
                 ("reddit_posts", "posted_at"), ("snapchat_posts", "posted_at"),
                 ("telegram_posts", "posted_at")]:
    if counts[t] == 0 or "scraped_at" not in null_pct.get(t, {}):
        continue
    row = q1(f"""SELECT
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (scraped_at-"{tcol}"))/86400.0),
        AVG(EXTRACT(EPOCH FROM (scraped_at-"{tcol}"))/86400.0),
        COUNT(*) FILTER (WHERE scraped_at < "{tcol}")
        FROM "{t}" WHERE scraped_at IS NOT NULL AND "{tcol}" IS NOT NULL;""")
    lag_stats[t] = dict(median_days=row[0], mean_days=row[1], negative_lag_rows=row[2])
R["lag_stats"] = lag_stats
print(json.dumps(lag_stats, indent=2, default=str))

print("Phase 1 (overview + quality) complete.")

# ============================================================ TEMPORAL ====
print("=== temporal: posts per week by platform ===")
PLATFORM_TABLES = [  # (label, table, posted_col)
    ("facebook", "facebook_posts", "posted_time"),
    ("instagram", "instagram_posts", "posted_at"),
    ("linkedin", "linkedin_posts", "posted_at"),
    ("pinterest", "pinterest_posts", "posted_at"),
    ("reddit", "reddit_posts", "posted_at"),
    ("snapchat", "snapchat_posts", "posted_at"),
    ("telegram", "telegram_posts", "posted_at"),
    ("youtube_video", "youtube_videos", "video_published_at"),
]
weekly = {}
for label, t, tcol in PLATFORM_TABLES:
    if counts[t] == 0:
        continue
    _, rows = q(f"""SELECT date_trunc('week', "{tcol}")::date AS wk, COUNT(*)
        FROM "{t}" WHERE "{tcol}" IS NOT NULL GROUP BY 1 ORDER BY 1;""")
    weekly[label] = [(str(w), c) for w, c in rows]
R["weekly"] = weekly

print("=== scraped_at recency ===")
recency = {}
for t, scol in [("facebook_posts", "scraped_at"), ("instagram_posts", "scraped_at"),
                 ("linkedin_posts", "scraped_at"), ("pinterest_posts", "scraped_at"),
                 ("reddit_posts", "scraped_at"), ("snapchat_posts", "scraped_at"),
                 ("telegram_posts", "scraped_at"), ("youtube_comments", "scraped_at"),
                 ("youtube_videos", "scraped_at")]:
    if counts[t] == 0:
        continue
    mn, mx = q1(f'SELECT MIN("{scol}"), MAX("{scol}") FROM "{t}" WHERE "{scol}" IS NOT NULL;')
    recency[t] = dict(min=str(mn), max=str(mx))
R["recency"] = recency

# ============================================================ KEYWORDS ====
print("=== keywords ===")
kw_tables = [("instagram", "instagram_posts", "keyword"), ("linkedin", "linkedin_posts", "keyword"),
             ("pinterest", "pinterest_posts", "keyword"), ("reddit", "reddit_posts", "keyword"),
             ("snapchat", "snapchat_posts", "keyword"), ("youtube_video", "youtube_videos", "keyword"),
             ("facebook", "facebook_posts", "keywords")]
top_keywords = {}
for label, t, kcol in kw_tables:
    if counts[t] == 0:
        continue
    _, rows = q(f"""SELECT "{kcol}", COUNT(*) c FROM "{t}"
        WHERE "{kcol}" IS NOT NULL AND "{kcol}" <> '' GROUP BY 1 ORDER BY 2 DESC LIMIT 10;""")
    top_keywords[label] = rows
R["top_keywords"] = top_keywords

_, rows = q("""SELECT keyword, COUNT(*) c FROM social_posts_unified
    WHERE keyword IS NOT NULL AND keyword <> '' GROUP BY 1 ORDER BY 2 DESC LIMIT 15;""")
R["top_keywords_unified"] = rows

_, rows = q("""SELECT keyword, COUNT(DISTINCT platform) FROM social_posts_unified
    WHERE keyword IS NOT NULL AND keyword <> '' GROUP BY 1 HAVING COUNT(DISTINCT platform) > 1
    ORDER BY 2 DESC LIMIT 15;""")
R["keyword_platform_overlap"] = rows

# ============================================================ ENGAGEMENT ==
print("=== engagement distributions ===")
ENGAGE_COLS = {
    "facebook": ("facebook_posts", ["reactions_count", "comments_count", "shares_count"]),
    "instagram": ("instagram_posts", ["likes_count", "comment_count", "shares_count"]),
    "linkedin": ("linkedin_posts", ["reaction_count", "comment_count"]),
    "pinterest": ("pinterest_posts", ["comment_count"]),
    "snapchat": ("snapchat_posts", ["view_count", "likes", "comments", "shares"]),
    "telegram": ("telegram_posts", ["views", "forwards"]),
    "youtube_video": ("youtube_videos", ["video_view_count", "video_like_count", "video_comment_count"]),
}
engage_stats = {}
engage_raw = {}
for label, (t, cols) in ENGAGE_COLS.items():
    engage_stats[label] = {}
    for c in cols:
        row = q1(f"""SELECT MIN("{c}"), PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{c}"),
            AVG("{c}"), MAX("{c}"), COUNT(*) FILTER (WHERE "{c}" IS NOT NULL)
            FROM "{t}";""")
        engage_stats[label][c] = dict(min=row[0], median=row[1], mean=row[2], max=row[3], n=row[4])
        _, vals = q(f'SELECT "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL AND "{c}" >= 0;')
        engage_raw[(label, c)] = [v[0] for v in vals]
R["engage_stats"] = engage_stats

top_posts = {}
top_post_queries = {
    "facebook": ('SELECT author_name, LEFT(COALESCE(post_text,\'\'),80), reactions_count, url '
                 'FROM facebook_posts ORDER BY reactions_count DESC NULLS LAST LIMIT 5'),
    "instagram": ('SELECT user_name, LEFT(COALESCE(caption,\'\'),80), likes_count, post_url '
                  'FROM instagram_posts ORDER BY likes_count DESC NULLS LAST LIMIT 5'),
    "linkedin": ('SELECT author_page, LEFT(COALESCE(main_text,\'\'),80), reaction_count, post_url '
                 'FROM linkedin_posts ORDER BY reaction_count DESC NULLS LAST LIMIT 5'),
    "telegram": ('SELECT channel_name, LEFT(COALESCE(text,\'\'),80), views, post_url '
                 'FROM telegram_posts ORDER BY views DESC NULLS LAST LIMIT 5'),
    "youtube_video": ('SELECT channel_name, LEFT(video_title,80), video_view_count, video_url '
                       'FROM youtube_videos ORDER BY video_view_count DESC NULLS LAST LIMIT 5'),
    "snapchat": ('SELECT creator, LEFT(COALESCE(caption,\'\'),80), view_count, spotlight_url '
                 'FROM snapchat_posts ORDER BY view_count DESC NULLS LAST LIMIT 5'),
}
for label, sql in top_post_queries.items():
    _, rows = q(sql)
    top_posts[label] = rows
R["top_posts"] = top_posts

# verified vs non-verified
verified_cmp = {}
for label, t, ecol in [("facebook", "facebook_posts", "reactions_count"),
                        ("instagram", "instagram_posts", "likes_count"),
                        ("youtube_video", "youtube_videos", "video_view_count")]:
    _, rows = q(f"""SELECT is_verified, COUNT(*), AVG("{ecol}"), PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "{ecol}")
        FROM "{t}" WHERE is_verified IS NOT NULL GROUP BY 1;""")
    verified_cmp[label] = rows
R["verified_cmp"] = verified_cmp

# ============================================================ LANGUAGE ====
print("=== language ===")
lang = {}
for label, t, lcol in [("telegram", "telegram_posts", "language"), ("snapchat", "snapchat_posts", "language"),
                        ("youtube_video", "youtube_videos", "transcript_language")]:
    _, rows = q(f"""SELECT COALESCE(NULLIF("{lcol}",''),'(unknown)'), COUNT(*) FROM "{t}"
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10;""")
    lang[label] = rows
R["lang"] = lang

# ============================================================ AUTHORS =====
print("=== authors / channels ===")
top_authors_vol = {}
top_authors_eng = {}
author_queries = {
    "telegram": ("telegram_posts", "channel_name", "views"),
    "youtube_video": ("youtube_videos", "channel_name", "video_view_count"),
    "facebook": ("facebook_posts", "author_name", "reactions_count"),
    "reddit": ("reddit_posts", "author", None),
    "instagram": ("instagram_posts", "user_name", "likes_count"),
}
for label, (t, acol, ecol) in author_queries.items():
    _, rows = q(f"""SELECT "{acol}", COUNT(*) FROM "{t}" WHERE "{acol}" IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10;""")
    top_authors_vol[label] = rows
    if ecol:
        _, rows = q(f"""SELECT "{acol}", SUM("{ecol}") s FROM "{t}" WHERE "{acol}" IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 10;""")
        top_authors_eng[label] = rows
R["top_authors_vol"] = top_authors_vol
R["top_authors_eng"] = top_authors_eng

# ============================================================ YOUTUBE =====
print("=== youtube deep dive ===")
_, dur = q("SELECT video_duration_seconds FROM youtube_videos WHERE video_duration_seconds IS NOT NULL AND video_duration_seconds > 0;")
R["yt_duration"] = [d[0] for d in dur]

_, yv = q("""SELECT video_view_count, video_like_count, video_comment_count FROM youtube_videos
    WHERE video_view_count IS NOT NULL AND video_like_count IS NOT NULL AND video_comment_count IS NOT NULL;""")
R["yt_vlc"] = yv

transcript_avail = q1("SELECT COUNT(*) FILTER (WHERE transcript_available), COUNT(*) FROM youtube_videos;")
R["transcript_avail"] = transcript_avail

_, twc = q("SELECT transcript_word_count FROM youtube_videos WHERE transcript_word_count IS NOT NULL AND transcript_word_count > 0;")
R["transcript_word_count"] = [d[0] for d in twc]

_, ib = q("SELECT COALESCE(importance_bucket,'(none)'), COUNT(*) FROM youtube_videos GROUP BY 1 ORDER BY 2 DESC;")
R["importance_bucket"] = ib

_, cpv = q("SELECT video_id, COUNT(*) c FROM youtube_comments GROUP BY 1 ORDER BY 2 DESC LIMIT 5000;")
R["comments_per_video_top"] = cpv[:10]
R["comments_per_video_dist"] = [c for _, c in cpv]

reply_ratio = q1("SELECT COUNT(*) FILTER (WHERE is_reply), COUNT(*) FILTER (WHERE NOT is_reply), COUNT(*) FILTER (WHERE is_reply IS NULL) FROM youtube_comments;")
R["reply_ratio"] = reply_ratio

_, top_vids = q("""SELECT channel_name, LEFT(video_title,80), video_view_count, video_comment_count, video_url
    FROM youtube_videos ORDER BY video_comment_count DESC NULLS LAST LIMIT 5;""")
R["top_commented_videos"] = top_vids

_, uview = q("SELECT DISTINCT platform FROM youtube_comments_unified LIMIT 1;") if False else (None, None)

conn.rollback()
cur.close()
conn.close()
print("All queries complete. Connection closed (rollback, read-only).")

with open(f"{OUT_DIR}/_raw_results.json", "w") as f:
    json.dump({k: v for k, v in R.items() if k not in ("engage_raw",)}, f, default=str, indent=2)

print("Saved raw results JSON. Now generating figures + HTML...")

PLATFORM_ORDER = ["telegram", "youtube_video", "reddit", "linkedin", "instagram",
                   "facebook", "snapchat", "pinterest"]
PLATFORM_COLOR = dict(zip(PLATFORM_ORDER, CAT))


def hbar(name, labels, values, color=BLUE, xlabel="", title="", fmt="{:,.0f}"):
    fig, ax = plt.subplots(figsize=(7.2, max(2.2, 0.42 * len(labels) + 0.6)))
    y = np.arange(len(labels))
    ax.barh(y, values, color=color, height=0.6)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    for lbl, artist in zip(labels, ax.get_yticklabels()):
        if needs_devanagari(lbl):
            artist.set_fontproperties(DEVANAGARI_FONT)
    ax.invert_yaxis()
    style_ax(ax)
    ax.set_xlabel(xlabel)
    for yi, v in zip(y, values):
        ax.text(v, yi, "  " + fmt.format(v), va="center", ha="left", fontsize=9, color=L["ink2"])
    if title:
        ax.set_title(title, loc="left", fontsize=12, color=L["ink"], pad=10)
    save_fig(fig, name)


def loghist(name, values, color=BLUE, xlabel="", title=""):
    vals = np.array([v for v in values if v is not None and v >= 0], dtype=float)
    fig, ax = plt.subplots(figsize=(5.6, 3.4))
    if len(vals) == 0:
        ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes, color=L["muted"])
    else:
        logvals = np.log10(vals + 1)
        ax.hist(logvals, bins=28, color=color, edgecolor=L["surface"], linewidth=0.6)
        xticks = ax.get_xticks()
        ax.set_xticklabels([f"{10**t-1:,.0f}" for t in xticks])
    style_ax(ax)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("count")
    if title:
        ax.set_title(title, loc="left", fontsize=11, color=L["ink"], pad=8)
    save_fig(fig, name)


def line_multi(name, series_dict, xlabel="", ylabel="posts / week", title="", logy=False):
    import matplotlib.dates as mdates
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    any_plotted = False
    for i, label in enumerate(PLATFORM_ORDER):
        if label not in series_dict:
            continue
        pts = series_dict[label]
        if not pts:
            continue
        xs = [datetime.strptime(p[0], "%Y-%m-%d") for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, color=PLATFORM_COLOR[label], linewidth=1.8, label=label)
        any_plotted = True
    style_ax(ax)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if logy:
        ax.set_yscale("symlog")
    if any_plotted:
        locator = mdates.AutoDateLocator(minticks=5, maxticks=10)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    leg = ax.legend(loc="upper left", frameon=False, ncol=4, fontsize=8.5)
    if title:
        ax.set_title(title, loc="left", fontsize=12, color=L["ink"], pad=10)
    save_fig(fig, name)


def heatmap(name, matrix, row_labels, col_labels, title="", fmt="{:.2f}", cmap=None, vmin=None, vmax=None):
    cmap = cmap or matplotlib.colors.LinearSegmentedColormap.from_list("seqblue", SEQ_BLUE)
    title_w = max(len(title) * 0.085, 0) if title else 0
    fig, ax = plt.subplots(figsize=(max(1.1 * len(col_labels) + 2.4, title_w), 0.55 * len(row_labels) + 1.6))
    im = ax.imshow(matrix, cmap=cmap, aspect="auto", vmin=vmin, vmax=vmax)
    ax.set_xticks(range(len(col_labels)))
    ax.set_xticklabels(col_labels, rotation=30, ha="right")
    ax.set_yticks(range(len(row_labels)))
    ax.set_yticklabels(row_labels)
    for i in range(len(row_labels)):
        for j in range(len(col_labels)):
            v = matrix[i][j]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            lum = im.cmap(im.norm(v))[:3]
            textcol = L["ink"] if (0.299*lum[0]+0.587*lum[1]+0.114*lum[2]) > 0.6 else "#ffffff"
            ax.text(j, i, fmt.format(v), ha="center", va="center", fontsize=9, color=textcol)
    ax.spines[:].set_visible(False)
    ax.tick_params(length=0)
    if title:
        ax.set_title(title, loc="left", fontsize=12, color=L["ink"], pad=10)
    save_fig(fig, name)


def grouped_bar(name, categories, series_labels, series_values, colors, ylabel="", title=""):
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    x = np.arange(len(categories))
    w = 0.8 / len(series_labels)
    for i, (slab, svals) in enumerate(zip(series_labels, series_values)):
        ax.bar(x + i * w - 0.4 + w/2, svals, width=w, color=colors[i], label=slab)
    ax.set_xticks(x)
    ax.set_xticklabels(categories)
    style_ax(ax)
    ax.set_ylabel(ylabel)
    ax.legend(frameon=False, fontsize=9)
    if title:
        ax.set_title(title, loc="left", fontsize=12, color=L["ink"], pad=10)
    save_fig(fig, name)


# ---- 1. row counts & sizes -------------------------------------------------
labels = [t.replace("_", " ") for t in TABLES]
hbar("row_counts", labels, [counts[t] for t in TABLES], xlabel="rows (snapshot)")
hbar("table_sizes_mb", labels, [sizes.get(t, 0)/1e6 for t in TABLES], xlabel="size (MB)", color=BLUE)

# ---- 2. null% heatmap (top varying columns) --------------------------------
null_rows = []
null_row_labels = []
for t in TABLES:
    if t not in null_pct:
        continue
    interesting = {k: v for k, v in null_pct[t].items() if v > 0}
    top = sorted(interesting.items(), key=lambda kv: -kv[1])[:6]
    for c, v in top:
        null_rows.append((t.replace("_posts", "").replace("_", " "), c, v))
if null_rows:
    tabs = sorted(set(r[0] for r in null_rows))
    max_cols = max(sum(1 for r in null_rows if r[0] == tb) for tb in tabs)
    mat = np.full((len(tabs), max_cols), np.nan)
    collabels = [f"col {i+1}" for i in range(max_cols)]
    cellnames = [["" for _ in range(max_cols)] for _ in range(len(tabs))]
    for tb_i, tb in enumerate(tabs):
        items = [r for r in null_rows if r[0] == tb]
        for j, (_, c, v) in enumerate(items):
            mat[tb_i][j] = v
            cellnames[tb_i][j] = c
    fig, ax = plt.subplots(figsize=(1.15*max_cols+2.4, 0.5*len(tabs)+1.5))
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("seqblue", SEQ_BLUE)
    im = ax.imshow(mat, cmap=cmap, aspect="auto", vmin=0, vmax=100)
    ax.set_yticks(range(len(tabs))); ax.set_yticklabels(tabs)
    ax.set_xticks([])
    for i in range(len(tabs)):
        for j in range(max_cols):
            if np.isnan(mat[i][j]):
                continue
            lum_val = mat[i][j]/100
            textcol = "#ffffff" if lum_val > 0.55 else L["ink"]
            ax.text(j, i, f"{cellnames[i][j]}\n{mat[i][j]:.0f}%", ha="center", va="center",
                    fontsize=7.6, color=textcol)
    ax.spines[:].set_visible(False)
    ax.set_title("Null % — top affected columns per table", loc="left", fontsize=12, color=L["ink"], pad=10)
    cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("null %", fontsize=8)
    save_fig(fig, "null_heatmap")

# ---- 3. weekly timeline -----------------------------------------------------
line_multi("weekly_timeline", weekly, title="Posts per week by platform (all history in snapshot, log scale)", logy=True)

# recent 16 weeks zoom
weekly_recent = {}
for label, pts in weekly.items():
    weekly_recent[label] = pts[-16:]
line_multi("weekly_timeline_recent", weekly_recent, title="Posts per week — last 16 weeks")

# ---- 4. keywords -------------------------------------------------------------
kw = R["top_keywords_unified"]
if kw:
    hbar("top_keywords", [k for k, c in kw][::-1], [c for k, c in kw][::-1], xlabel="posts")

# ---- 5. engagement small multiples ------------------------------------------
metric_label = {
    "reactions_count": "reactions", "comments_count": "comments", "shares_count": "shares",
    "likes_count": "likes", "comment_count": "comments", "reaction_count": "reactions",
    "view_count": "views", "likes": "likes", "comments": "comments", "shares": "shares",
    "views": "views", "forwards": "forwards", "video_view_count": "views",
    "video_like_count": "likes", "video_comment_count": "comments",
}
panels = [(label, c) for label, (t, cols) in ENGAGE_COLS.items() for c in cols]
ncols = 4
nrows = math.ceil(len(panels) / ncols)
fig, axes = plt.subplots(nrows, ncols, figsize=(3.1*ncols, 2.6*nrows))
axes = np.array(axes).reshape(-1)
for i, (label, c) in enumerate(panels):
    ax = axes[i]
    vals = np.array([v for v in engage_raw[(label, c)] if v is not None and v >= 0], dtype=float)
    if len(vals) > 0:
        logvals = np.log10(vals + 1)
        ax.hist(logvals, bins=20, color=PLATFORM_COLOR.get(label, BLUE), edgecolor=L["surface"], linewidth=0.4)
        xticks = ax.get_xticks()
        ax.set_xticklabels([f"{10**t-1:,.0f}" for t in xticks], fontsize=7)
    ax.set_title(f"{label} · {metric_label.get(c,c)}", fontsize=9, loc="left", color=L["ink2"])
    style_ax(ax)
    ax.tick_params(axis="y", labelsize=7)
for j in range(len(panels), len(axes)):
    axes[j].axis("off")
fig.suptitle("Engagement distributions (log10 scale)", x=0.01, ha="left", fontsize=13, color=L["ink"])
save_fig(fig, "engagement_small_multiples")

# ---- 6. verified vs not ------------------------------------------------------
vc = R["verified_cmp"]
cats, s_false, s_true = [], [], []
for label, rows in vc.items():
    d = {bool(v): (n, avg) for v, n, avg, med in rows}
    if False in d and True in d:
        cats.append(label)
        s_false.append(float(d[False][1]))
        s_true.append(float(d[True][1]))
if cats:
    grouped_bar("verified_vs_not", cats, ["not verified", "verified"], [s_false, s_true],
                [BLUE, ORANGE], ylabel="mean engagement", title="Verified vs non-verified — mean primary engagement")

# ---- 7. language --------------------------------------------------------------
for label, rows in lang.items():
    if rows:
        hbar(f"lang_{label}", [r[0] for r in rows][::-1], [r[1] for r in rows][::-1], xlabel="posts",
             title=f"{label}: top languages")

# ---- 8. authors/channels ------------------------------------------------------
for label, rows in top_authors_vol.items():
    if rows:
        hbar(f"authors_vol_{label}", [str(r[0])[:28] for r in rows][::-1], [r[1] for r in rows][::-1],
             xlabel="posts", title=f"{label}: top accounts by volume")
for label, rows in top_authors_eng.items():
    if rows:
        rows2 = [(a, float(s) if s is not None else 0) for a, s in rows]
        hbar(f"authors_eng_{label}", [str(r[0])[:28] for r in rows2][::-1], [r[1] for r in rows2][::-1],
             xlabel="total engagement", color=ORANGE, title=f"{label}: top accounts by total engagement")

# ---- 9. youtube deep dive -------------------------------------------------
durations_min = [d/60.0 for d in R["yt_duration"]]
fig, ax = plt.subplots(figsize=(6, 3.4))
ax.hist([d for d in durations_min if d <= 60], bins=30, color=BLUE, edgecolor=L["surface"], linewidth=0.5)
style_ax(ax); ax.set_xlabel("duration (minutes, capped at 60)"); ax.set_ylabel("videos")
ax.set_title("YouTube video duration distribution", loc="left", fontsize=12, color=L["ink"], pad=8)
save_fig(fig, "yt_duration")

yv = R["yt_vlc"]
if yv:
    arr = np.array(yv, dtype=float)
    logarr = np.log10(arr + 1)
    corr = np.corrcoef(logarr.T)
    heatmap("yt_corr", corr.tolist(), ["views", "likes", "comments"], ["views", "likes", "comments"],
            title="View/like/comment correlation (log, r)", cmap=DIV, vmin=-1, vmax=1)

loghist("yt_transcript_wordcount", R["transcript_word_count"], xlabel="transcript word count",
        title="Transcript word-count distribution (videos with transcripts)")

ib = R["importance_bucket"]
if ib:
    hbar("importance_bucket", [r[0] for r in ib][::-1], [r[1] for r in ib][::-1], xlabel="videos")

loghist("comments_per_video", R["comments_per_video_dist"], xlabel="comments per video",
        title="Comments-per-video distribution")

rr = R["reply_ratio"]
grouped_bar("reply_ratio", ["comments"], ["replies", "top-level"], [[rr[0]], [rr[1]]], [ORANGE, BLUE],
            ylabel="count", title="YouTube comments: replies vs top-level")

print("Figures generated:", len(FIGS))
with open(f"{OUT_DIR}/_R_full.json", "w") as f:
    json.dump(R, f, default=str, indent=2)
print("Now writing HTML + KEY_FINDINGS...")

def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def fig_img(name, caption=""):
    b64 = FIGS.get(name)
    if not b64:
        return f"<p class='muted'>[chart '{name}' unavailable]</p>"
    cap = f"<figcaption>{esc(caption)}</figcaption>" if caption else ""
    return f"""<figure class="chart-card"><img src="data:image/png;base64,{b64}" alt="{esc(caption or name)}">{cap}</figure>"""


def table_html(headers, rows, fmts=None):
    fmts = fmts or [None] * len(headers)
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = []
    for row in rows:
        tds = []
        for v, f in zip(row, fmts):
            if v is None:
                tds.append("<td class='muted'>—</td>")
            elif f:
                tds.append(f"<td>{f.format(v)}</td>")
            else:
                tds.append(f"<td>{esc(v)}</td>")
        trs.append("<tr>" + "".join(tds) + "</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"


total_rows = sum(counts.values())
snapshot_str = SNAPSHOT_TS.strftime("%Y-%m-%d %H:%M UTC")

# --- overview table
ov_rows = [(t.replace("_", " "), counts[t], f"{sizes.get(t,0)/1e6:.1f} MB") for t in TABLES]
overview_table = table_html(["table", "rows (snapshot)", "size"], ov_rows)

# --- growth callout numbers (observed live during inspection, hardcoded from session)
growth_note = """
<div class="callout warn">
<strong>Live, actively-ingesting database.</strong> During initial reconnaissance of this database (before
this snapshot), background scrapers were observed writing new rows within minutes — telegram_posts grew from
14,281 → 23,325 rows (+63%) in a few minutes of inspection, and reddit/instagram/linkedin/snapchat also grew
noticeably. All counts and charts below are frozen from a single REPEATABLE READ transaction taken at
<strong>{snap}</strong>, so they are mutually consistent with each other — but re-running this script later
will show materially higher counts, especially for telegram_posts and youtube_comments.
</div>
""".format(snap=snapshot_str)

# --- data quality
dup_rows = [(t.replace("_posts",""), dups.get(t,0), counts[t], f"{100*dups.get(t,0)/counts[t]:.1f}%" if counts[t] else "—")
            for t in dup_checks if counts.get(t,0) > 0]
dup_table = table_html(["table", "duplicate key rows", "total rows", "dup rate"], dup_rows)

ts_rows = [(t.replace("_posts",""), v["null_ts"], f"{100*v['null_ts']/counts[t]:.1f}%" if counts[t] else "—",
            v["min"][:10] if v["min"] != "None" else "—", v["max"][:10] if v["max"] != "None" else "—")
           for t, v in ts_checks.items()]
ts_table = table_html(["table", "null posted_at", "null rate", "earliest", "latest"], ts_rows)

lag_rows = [(t.replace("_posts",""), f"{v['median_days']:.2f}" if v["median_days"] is not None else "—",
             f"{float(v['mean_days']):.2f}" if v["mean_days"] is not None else "—", v["negative_lag_rows"])
            for t, v in lag_stats.items()]
lag_table = table_html(["table", "median lag (days)", "mean lag (days)", "scraped-before-posted rows"], lag_rows)

# --- keyword tables
kw_sections = []
for label, rows in top_keywords.items():
    if rows:
        kw_sections.append(f"<h4>{esc(label)}</h4>" + table_html(["keyword", "count"], rows))
kw_overlap_table = table_html(["keyword", "platforms"], R["keyword_platform_overlap"]) if R["keyword_platform_overlap"] else "<p class='muted'>none</p>"

# --- top posts tables
top_posts_sections = []
for label, rows in top_posts.items():
    fmt_rows = [(a, (t + "…") if t and len(t) >= 80 else t, v, u) for a, t, v, u in rows]
    top_posts_sections.append(f"<h4>{esc(label)}</h4>" + table_html(["author/channel", "text (truncated)", "engagement", "url"], fmt_rows))

# --- engage stats table
engage_stat_rows = []
for label, cols in engage_stats.items():
    for c, s in cols.items():
        engage_stat_rows.append((label, metric_label.get(c,c), s["min"], f"{float(s['median']):.0f}" if s['median'] is not None else "—",
                                  f"{float(s['mean']):.1f}" if s['mean'] is not None else "—", s["max"], s["n"]))
engage_table = table_html(["platform", "metric", "min", "median", "mean", "max", "n"], engage_stat_rows)

# --- authors / lang html blocks
def imgs_for(prefix, keys):
    return "".join(fig_img(f"{prefix}_{k}") for k in keys if f"{prefix}_{k}" in FIGS)

lang_imgs = imgs_for("lang", lang.keys())
authors_vol_imgs = imgs_for("authors_vol", top_authors_vol.keys())
authors_eng_imgs = imgs_for("authors_eng", top_authors_eng.keys())

top_commented_table = table_html(["channel", "title", "views", "comments", "url"], R["top_commented_videos"])

transcript_pct = 100*transcript_avail[0]/transcript_avail[1] if transcript_avail[1] else 0
reply_pct = 100*rr[0]/(rr[0]+rr[1]) if (rr[0]+rr[1]) else 0

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin:0; font-family: system-ui,-apple-system,"Segoe UI",sans-serif;
  background: #f9f9f7; color:#0b0b0b; line-height:1.5;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) body { background:#0d0d0d; color:#ffffff; }
}
:root[data-theme="dark"] body { background:#0d0d0d; color:#ffffff; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 80px; }
header.hero { padding: 28px 0 20px; border-bottom: 1px solid rgba(11,11,11,0.10); margin-bottom: 28px; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) header.hero { border-color: rgba(255,255,255,0.10);} }
:root[data-theme="dark"] header.hero { border-color: rgba(255,255,255,0.10); }
h1 { font-size: 1.7rem; margin: 0 0 6px; }
h2 { font-size: 1.25rem; margin: 40px 0 14px; padding-top: 8px; border-top: 1px solid #e1e0d9; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) h2 { border-color:#2c2c2a; } }
:root[data-theme="dark"] h2 { border-color:#2c2c2a; }
h3 { font-size: 1.05rem; margin: 24px 0 10px; color:#52514e; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) h3 { color:#c3c2b7; } }
:root[data-theme="dark"] h3 { color:#c3c2b7; }
h4 { font-size: 0.92rem; margin: 18px 0 6px; color:#898781; text-transform: uppercase; letter-spacing:.03em;}
p { color:#52514e; max-width: 78ch; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) p { color:#c3c2b7; } }
:root[data-theme="dark"] p { color:#c3c2b7; }
.muted { color:#898781; }
.meta { color:#898781; font-size:0.92rem; }
.stat-row { display:flex; flex-wrap:wrap; gap:14px; margin: 18px 0; }
.stat-tile { background:#fcfcfb; border:1px solid rgba(11,11,11,0.08); border-radius:10px; padding:14px 18px; min-width:140px; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .stat-tile { background:#1a1a19; border-color: rgba(255,255,255,0.08);} }
:root[data-theme="dark"] .stat-tile { background:#1a1a19; border-color: rgba(255,255,255,0.08); }
.stat-tile .v { font-size: 1.5rem; font-weight:600; }
.stat-tile .k { font-size: 0.78rem; color:#898781; text-transform:uppercase; letter-spacing:.04em; }
.callout { border-radius: 10px; padding: 14px 18px; margin: 16px 0; font-size:0.92rem; }
.callout.warn { background: #fdf3e4; border:1px solid #eda100; color:#52514e; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .callout.warn { background:#2a2210; border-color:#c98500; color:#c3c2b7;} }
:root[data-theme="dark"] .callout.warn { background:#2a2210; border-color:#c98500; color:#c3c2b7; }
.callout.bad { background: #fbe9e8; border:1px solid #e34948; color:#52514e; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .callout.bad { background:#2a1414; border-color:#e66767; color:#c3c2b7;} }
:root[data-theme="dark"] .callout.bad { background:#2a1414; border-color:#e66767; color:#c3c2b7; }
table { border-collapse: collapse; width:100%; margin: 10px 0 22px; font-size:0.88rem; }
th, td { text-align:left; padding:6px 10px; border-bottom: 1px solid #e1e0d9; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) th, :root:where(:not([data-theme="light"])) td { border-color:#2c2c2a; } }
:root[data-theme="dark"] th, :root[data-theme="dark"] td { border-color:#2c2c2a; }
th { color:#898781; font-weight:600; font-size:0.78rem; text-transform:uppercase; letter-spacing:.03em; }
td { color:#0b0b0b; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) td { color:#ffffff; } }
:root[data-theme="dark"] td { color:#ffffff; }
figure.chart-card { background:#fcfcfb; border:1px solid rgba(11,11,11,0.08); border-radius:12px; padding:14px; margin:14px 0; display:inline-block; max-width:100%; }
figure.chart-card img { max-width:100%; height:auto; display:block; }
figcaption { color:#898781; font-size:0.8rem; margin-top:6px; }
.grid2 { display:grid; grid-template-columns: 1fr 1fr; gap:16px; }
@media (max-width: 800px) { .grid2 { grid-template-columns: 1fr; } }
.toc { columns: 2; font-size: 0.92rem; }
@media (max-width: 700px) { .toc { columns: 1; } }
.toc a { color:#2a78d6; text-decoration:none; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) .toc a { color:#3987e5; } }
:root[data-theme="dark"] .toc a { color:#3987e5; }
code { background: rgba(11,11,11,0.06); padding:1px 5px; border-radius:4px; font-size:0.85em; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) code { background: rgba(255,255,255,0.1);} }
:root[data-theme="dark"] code { background: rgba(255,255,255,0.1); }
"""

html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ShaktiDB Social-Media EDA Report</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <h1>ShaktiDB Social-Media — Exploratory Data Analysis</h1>
  <p class="meta">Database <code>shaktidb_social</code> · host <code>{PG["host"]}:{PG["port"]}</code> (PostgreSQL 18.4 / pgvector) ·
  snapshot taken <strong>{snapshot_str}</strong> (single REPEATABLE READ transaction — all figures below are mutually consistent)</p>
</header>

<div class="stat-row">
  <div class="stat-tile"><div class="v">{len(TABLES)}</div><div class="k">base tables</div></div>
  <div class="stat-tile"><div class="v">{total_rows:,}</div><div class="k">total rows (snapshot)</div></div>
  <div class="stat-tile"><div class="v">{dbsize/1e6:.0f} MB</div><div class="k">database size</div></div>
  <div class="stat-tile"><div class="v">8</div><div class="k">platforms covered</div></div>
</div>

{growth_note}

<nav class="toc">
<a href="#overview">1. Overview</a><br>
<a href="#quality">2. Data quality</a><br>
<a href="#temporal">3. Temporal coverage</a><br>
<a href="#keywords">4. Keyword / topic analysis</a><br>
<a href="#engagement">5. Engagement metrics</a><br>
<a href="#language">6. Language</a><br>
<a href="#authors">7. Authors / channels</a><br>
<a href="#youtube">8. YouTube deep dive</a><br>
<a href="#findings">9. Key findings</a><br>
</nav>

<h2 id="overview">1. Overview</h2>
<p>The database backs a keyword/OSINT-style social monitoring pipeline: every platform table carries a
<code>keyword</code> column (or equivalent), a <code>scraped_at</code>/<code>ingested_at</code> pair, and
pgvector <code>embedding</code> + <code>tsvector</code> <code>text_search</code> columns for semantic and
full-text retrieval. Two convenience views, <code>social_posts_unified</code> (8 platforms, excludes
YouTube's native shape and the empty WhatsApp table) and <code>youtube_comments_unified</code>, expose a
common schema across platforms — both are plain <code>UNION ALL</code>s of the base tables, so they carry
no extra dedup logic of their own.</p>
{overview_table}
<div class="grid2">
{fig_img("row_counts", "Row count per table (snapshot)")}
{fig_img("table_sizes_mb", "On-disk size per table (MB)")}
</div>

<h2 id="quality">2. Data quality</h2>
<h3>Nulls</h3>
<p>Columns below 0% null in every table are omitted; only columns with at least one null are shown, top 6 per table.</p>
{fig_img("null_heatmap", "Null percentage — worst columns per table")}

<h3>Duplicate natural keys</h3>
<p>Row count minus distinct-key count, on each table's expected unique identifier.</p>
{dup_table}

<h3>Timestamp sanity</h3>
<p><code>posted_at</code> equivalent column: null rate and observed date range. No future-dated rows were found anywhere.</p>
{ts_table}

<h3>Scrape lag</h3>
<p>Days between a post's <code>posted_at</code> and when it was <code>scraped_at</code>. Pinterest is omitted — 100% of its <code>posted_at</code> values are null.</p>
{lag_table}

<h2 id="temporal">3. Temporal coverage</h2>
<p>Weekly post volume by platform, across the full history captured in this snapshot, and zoomed to the last 16 weeks.</p>
{fig_img("weekly_timeline", "Posts per week by platform — full history")}
{fig_img("weekly_timeline_recent", "Posts per week by platform — last 16 weeks")}

<h2 id="keywords">4. Keyword / topic analysis</h2>
<p>Top tracked keywords across the unified view (8 platforms), and per-table breakdowns. Telegram and YouTube
comments track by channel rather than keyword, so they're absent from the unified keyword view (YouTube videos
do carry a <code>keyword</code> column and are shown separately below).</p>
{fig_img("top_keywords", "Top 15 keywords, unified view (8 platforms)")}
<h3>Keywords tracked on more than one platform</h3>
{kw_overlap_table}
<h3>Per-platform top keywords</h3>
{''.join(kw_sections)}

<h2 id="engagement">5. Engagement metrics</h2>
<p>Distributions are log10-scaled (x-axis labels show the un-logged value) because engagement counts are
heavy-tailed: a handful of posts dominate the total. Reddit and Telegram/WhatsApp have no likes/reactions
column in this schema — Telegram exposes <code>views</code>/<code>forwards</code> only, Reddit exposes neither.</p>
{fig_img("engagement_small_multiples", "Per-platform engagement metric distributions (log10)")}
<h3>Summary statistics</h3>
{engage_table}
<h3>Verified vs non-verified accounts</h3>
<p>Facebook and YouTube videos in this snapshot have no verified accounts at all (<code>is_verified</code> is
always false), so only Instagram has a real comparison.</p>
{fig_img("verified_vs_not", "Mean primary engagement metric, verified vs not")}
<h3>Top posts by platform</h3>
{''.join(top_posts_sections)}

<h2 id="language">6. Language</h2>
<p>Language is recorded natively for Telegram and Snapchat, and inferred on YouTube via transcript language.</p>
{lang_imgs}

<h2 id="authors">7. Authors / channels</h2>
<p>Top accounts by post volume, and (where an engagement column exists) by total engagement.</p>
{authors_vol_imgs}
{authors_eng_imgs}

<h2 id="youtube">8. YouTube deep dive</h2>
<p>YouTube is the richest table pair in the schema: {counts['youtube_videos']:,} videos and
{counts['youtube_comments']:,} comments, with transcript ingestion and an importance-scoring layer not present
on any other platform.</p>
<div class="stat-row">
  <div class="stat-tile"><div class="v">{transcript_pct:.0f}%</div><div class="k">videos with transcript ({transcript_avail[0]:,} / {transcript_avail[1]:,})</div></div>
  <div class="stat-tile"><div class="v">{reply_pct:.0f}%</div><div class="k">comments that are replies ({rr[0]:,} / {rr[0]+rr[1]:,})</div></div>
</div>
{fig_img("yt_duration", "Video duration distribution (minutes, capped at 60)")}
{fig_img("yt_corr", "Correlation between views / likes / comments (log-scale)")}
{fig_img("yt_transcript_wordcount", "Transcript word-count distribution")}
{fig_img("importance_bucket", "Videos by importance bucket")}
{fig_img("comments_per_video", "Comments-per-video distribution")}
{fig_img("reply_ratio", "Replies vs top-level comments")}
<h3>Top commented videos</h3>
{top_commented_table}

<h2 id="findings">9. Key findings</h2>
<p>See <code>KEY_FINDINGS.md</code> alongside this report for the condensed summary.</p>

</div>
</body>
</html>
"""

with open(f"{OUT_DIR}/EDA_Report.html", "w") as f:
    f.write(html)
print(f"Wrote {OUT_DIR}/EDA_Report.html ({len(html)/1e6:.2f} MB)")
