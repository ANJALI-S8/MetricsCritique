#!/usr/bin/env python3
"""
Entity extraction — hybrid NER + gazetteer, full corpus.

Prototyped on a 216-post reservoir sample (lang mix: 102 en, 9 ru, 9 zh-cn, 6 bn,
6 uk, 4 hi, rest other) — see docs/GEO_ONTOLOGY_CONTEXT.md task log. Findings that
shaped this script:
  - Model Babelscape/wikineural-multilingual-ner (HF token-classification pipeline,
    aggregation_strategy='simple', PER/ORG/LOC only, MISC dropped) loads in ~7-14s
    and runs fine on CPU at corpus scale (GPU is memory-held by vLLM, 44/49GB, per
    env notes) — device=-1 throughout.
  - Gazetteer (55 curated domain entries, data/gazetteer -- see GAZETTEER_PATH) is
    matched case-insensitively against each surface form for entity_uid +
    entity_subtype + subtype_source='gazetteer'. Unmatched spans are KEPT (never
    dropped) with subtype_source='unresolved', entity_subtype=NULL, pending a later
    zero-shot subtyping pass.
  - CJK tokenization artifact: mBERT tokenizes CJK text character-by-character, and
    since adjacent CJK chars aren't '##'-continuation pieces, aggregation_strategy=
    'simple' rejoins them with spaces (e.g. "德 国 救 生 协 会"). Fixed with a
    regex that strips whitespace strictly between two CJK characters before
    dedup/gazetteer matching (despace_cjk() below).
  - Precision spot-check (25 random mentions, prototype sample): ~84% correct.
    Known false-positive modes at this scale: bare acronyms tagged ORG (e.g. "AI"),
    single stray non-Latin characters tagged LOC. Not filtered here (kept for the
    zero-shot pass to adjudicate) — see docs/GEO_ONTOLOGY_CONTEXT.md.

Reads FULL corpus from data/analytics.duckdb (read-only, never written).
Writes entity_dim / entity_mentions to data/geo_ontology.duckdb (sole writer here;
does not touch topic_dim_v2 / post_topic_v2 from the prior stage).
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

EDA_ROOT = Path(__file__).resolve().parents[1]
SRC_DB = EDA_ROOT / "data" / "analytics.duckdb"
OUT_DB = EDA_ROOT / "data" / "geo_ontology.duckdb"
# Read the committed copy under data/, not a session scratchpad -- the previous
# path pointed at /tmp/claude-.../scratchpad/gazetteer.json, which makes this
# script unreproducible the moment that session directory is cleaned up.
GAZETTEER_PATH = EDA_ROOT / "data" / "gazetteer.json"

MODEL_NAME = "Babelscape/wikineural-multilingual-ner"
TYPE_MAP = {"PER": "PERSON", "ORG": "ORG", "LOC": "LOCATION"}  # MISC dropped
MIN_TEXT_LEN = 40
MAX_CHARS = 1000  # matches prototype truncation; avoids >512-token pipeline errors
PROGRESS_EVERY = 1000

CJK_RANGES = r"一-鿿㐀-䶿豈-﫿"
_CJK_SPACE_RE = re.compile(rf"(?<=[{CJK_RANGES}])\s+(?=[{CJK_RANGES}])")


def despace_cjk(s: str) -> str:
    """Strip whitespace the tokenizer inserted strictly between two CJK chars."""
    return _CJK_SPACE_RE.sub("", s)


# A span can start/end mid-word, in which case the pipeline hands back the
# WordPiece continuation marker: '##News', '##egram Channel' (a mangled
# "Telegram Channel"), '##rendraModi'. Those leaked into entity_dim as 2,017
# distinct "entities" / 4,466 mentions and even produced alerts. The offsets
# are reliable, so recover the real word from the source text instead of
# string-patching the token.
_WORDCHAR_RE = re.compile(r"[^\W_]", re.UNICODE)
_CJK_CHAR_RE = re.compile(rf"[{CJK_RANGES}]")


def expand_to_word_boundaries(text: str, start: int, end: int) -> str:
    """Widen [start,end) outward while the neighbouring char continues the same
    word. Skipped for CJK, which has no spaces -- expanding there would swallow
    the rest of the sentence."""
    if start is None or end is None or not (0 <= start < end <= len(text)):
        return ""
    if _CJK_CHAR_RE.search(text[start:end]):
        return text[start:end]
    while start > 0 and _WORDCHAR_RE.match(text[start - 1]) and _WORDCHAR_RE.match(text[start]):
        start -= 1
    while end < len(text) and _WORDCHAR_RE.match(text[end]) and _WORDCHAR_RE.match(text[end - 1]):
        end += 1
    return text[start:end].strip()


# Surface forms that are never a real entity, whatever the model scored them.
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def is_junk_surface(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    if "##" in s:
        return True                      # unrecovered subword fragment
    if _PUNCT_ONLY_RE.match(s) or not _WORDCHAR_RE.search(s):
        return True                      # punctuation-only / no letter or digit
    # Length floor applies to alphabetic scripts only. A CJK entity is
    # routinely 2-3 characters -- 美国 (US, 1,023 mentions), 伊朗 (Iran, 657),
    # 中国 (China, 372) -- so a blanket len<=2 rule would delete exactly the
    # high-volume non-Latin entities this fix is supposed to protect.
    if _CJK_CHAR_RE.search(s):
        return False
    if len(s) <= 2:
        return True                      # 1,210 such "entities" / 12,300 mentions previously
    return False


def load_gazetteer(path: Path) -> dict[str, dict]:
    entries = json.loads(path.read_text())
    surface_to_entry: dict[str, dict] = {}
    for e in entries:
        for sf in e["surface_forms"]:
            surface_to_entry[sf.lower()] = e
    return surface_to_entry


def main() -> int:
    if not SRC_DB.exists():
        print(f"ERROR: source db missing: {SRC_DB}")
        return 1
    if not GAZETTEER_PATH.exists():
        print(f"ERROR: gazetteer missing: {GAZETTEER_PATH}")
        return 1

    con = duckdb.connect(str(OUT_DB), read_only=False)
    existing = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables"
    ).fetchall()}
    # Guard stays: overwriting a multi-hour extraction by accident is expensive.
    # --replace is the explicit opt-in for a deliberate re-run (e.g. after a
    # gazetteer or span-cleaning change). Back up geo_ontology.duckdb first --
    # it also holds risk_labels/theme_labels/topic_dim_v3, which this stage does
    # NOT regenerate.
    replace = "--replace" in sys.argv
    for tbl in ("entity_dim", "entity_mentions"):
        if tbl in existing and not replace:
            print(f"ERROR: {tbl} already exists in {OUT_DB}; refusing to clobber blindly. "
                  f"Re-run intentionally with --replace (back up the DB first).")
            return 1
    if replace:
        print("--replace: existing entity_dim/entity_mentions will be overwritten on completion")
    if not {"topic_dim_v2", "post_topic_v2"}.issubset(existing):
        print("WARN: expected prior-stage tables (topic_dim_v2/post_topic_v2) not found "
              "-- continuing anyway, this stage does not depend on them")
    con.close()  # release the writer lock while the (slow) NER pass runs

    src = duckdb.connect(str(SRC_DB), read_only=True)
    df = src.execute(
        f"""
        SELECT post_uid, text, lang, posted_at FROM post_facts
        WHERE text IS NOT NULL AND length(text) > {MIN_TEXT_LEN}
        """
    ).df()
    src.close()
    print(f"eligible posts (text not null, len>{MIN_TEXT_LEN}): {len(df):,}")

    gaz = load_gazetteer(GAZETTEER_PATH)
    print(f"gazetteer surface forms loaded: {len(gaz)}")

    t0 = time.time()
    from transformers import pipeline
    nlp = pipeline("ner", model=MODEL_NAME, aggregation_strategy="simple", device=-1)
    print(f"model loaded in {time.time()-t0:.1f}s: {MODEL_NAME}")

    mention_rows = []
    n_err = 0
    n_junk = 0
    t_start = time.time()
    for i, row in enumerate(df.itertuples(index=False), start=1):
        text = str(row.text)[:MAX_CHARS]
        try:
            ents = nlp(text)
        except Exception:
            n_err += 1
            continue
        for e in ents:
            etype = TYPE_MAP.get(e["entity_group"])
            if etype is None:
                continue
            # Prefer the source-text span (recovers '##egram Channel' ->
            # 'Telegram Channel'); fall back to the token only if offsets are
            # missing or the recovery came back empty.
            recovered = expand_to_word_boundaries(text, e.get("start"), e.get("end"))
            surface = despace_cjk((recovered or e["word"]).strip())
            if is_junk_surface(surface):
                n_junk += 1
                continue
            gaz_hit = gaz.get(surface.lower())
            mention_rows.append(
                {
                    "post_uid": row.post_uid,
                    "lang": row.lang,
                    "posted_at": row.posted_at,
                    "surface_form": surface,
                    "entity_type": etype,
                    "entity_uid": gaz_hit["entity_uid"] if gaz_hit else None,
                    "entity_subtype": gaz_hit["entity_subtype"] if gaz_hit else None,
                    "subtype_source": "gazetteer" if gaz_hit else "unresolved",
                    "subtype_confidence": 1.0 if gaz_hit else None,
                    "ner_score": float(e["score"]),
                }
            )
        if i % PROGRESS_EVERY == 0:
            elapsed = time.time() - t_start
            rate = i / elapsed
            eta = (len(df) - i) / rate if rate > 0 else float("nan")
            print(f"  ... {i:,}/{len(df):,} posts, {len(mention_rows):,} mentions, "
                  f"{elapsed:.0f}s elapsed, ETA {eta/60:.1f} min", flush=True)

    print(f"NER pass done: {time.time()-t_start:.0f}s total, errors={n_err}, "
          f"junk spans dropped={n_junk:,} (subword fragments / <=2 chars / punctuation-only)")

    mentions = pd.DataFrame(mention_rows)
    # provisional entity_uid for unresolved mentions: slug of the lowercased surface
    # form, so entity_dim can still group/aggregate them pending the zero-shot pass
    unresolved_mask = mentions["entity_uid"].isna()
    mentions.loc[unresolved_mask, "entity_uid"] = (
        mentions.loc[unresolved_mask, "surface_form"]
        .str.lower()
        .str.strip()
        .str.replace(r"[^\w]+", "_", regex=True)
        .str.strip("_")
        .radd("unresolved_")
    )
    mentions.insert(0, "mention_id", range(1, len(mentions) + 1))
    mentions["extracted_at"] = datetime.now(timezone.utc).isoformat()
    mentions["model"] = MODEL_NAME

    n_total = len(mentions)
    n_gaz = int((mentions["subtype_source"] == "gazetteer").sum())
    by_type = Counter(mentions["entity_type"])
    print(f"\nmentions total: {n_total:,}  gazetteer-matched: {n_gaz:,} "
          f"({100*n_gaz/max(1,n_total):.1f}%)")
    print(f"by entity_type: {dict(by_type)}")

    # ---- entity_dim: one row per distinct entity_uid ----
    agg = (
        mentions.groupby("entity_uid")
        .agg(
            entity_type=("entity_type", lambda s: s.mode().iat[0]),
            entity_subtype=("entity_subtype", lambda s: s.dropna().mode().iat[0] if s.notna().any() else None),
            subtype_source=("subtype_source", lambda s: s.mode().iat[0]),
            mention_count=("mention_id", "count"),
            distinct_posts=("post_uid", "nunique"),
            first_seen=("posted_at", "min"),
            last_seen=("posted_at", "max"),
            avg_ner_score=("ner_score", "mean"),
        )
        .reset_index()
    )

    def canonical_name(uid: str, sub: pd.DataFrame) -> str:
        # gazetteer entries: prefer the primary surface form from the gazetteer file
        for e in gaz.values():
            if e["entity_uid"] == uid:
                return e["surface_forms"][0]
        # unresolved: most frequent raw surface form for this provisional uid
        return sub["surface_form"].value_counts().idxmax()

    canon = mentions.groupby("entity_uid").apply(
        lambda sub: canonical_name(sub.name, sub), include_groups=False
    )
    agg["canonical_name"] = agg["entity_uid"].map(canon)
    entity_dim = agg[
        [
            "entity_uid", "canonical_name", "entity_type", "entity_subtype",
            "subtype_source", "mention_count", "distinct_posts",
            "first_seen", "last_seen", "avg_ner_score",
        ]
    ].sort_values("mention_count", ascending=False).reset_index(drop=True)

    entity_mentions = mentions[
        [
            "mention_id", "post_uid", "surface_form", "entity_uid", "entity_type",
            "entity_subtype", "subtype_confidence", "subtype_source", "ner_score",
            "lang", "posted_at", "model", "extracted_at",
        ]
    ]

    # ---- Write to geo_ontology.duckdb (sole writer this stage) ----
    out = duckdb.connect(str(OUT_DB), read_only=False)
    out.register("_entity_dim", entity_dim)
    # OR REPLACE so the stage is re-runnable: plain CREATE TABLE made a second
    # run fail outright, which is why the '##' fix could not simply be re-applied.
    out.execute("CREATE OR REPLACE TABLE entity_dim AS SELECT * FROM _entity_dim")
    out.unregister("_entity_dim")
    out.register("_entity_mentions", entity_mentions)
    out.execute("CREATE OR REPLACE TABLE entity_mentions AS SELECT * FROM _entity_mentions")
    out.unregister("_entity_mentions")
    out.close()

    print(f"\nwrote {OUT_DB}")
    print(f"  entity_dim: {len(entity_dim):,} rows")
    print(f"  entity_mentions: {len(entity_mentions):,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
