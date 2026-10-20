#!/usr/bin/env python3
"""
Entity subtype resolution -- zero-shot fallback + entity_type bug fix, full corpus.

Fixes two verified problems from the prior build (docs/GEO_ONTOLOGY_CONTEXT.md):

  BUG 1 -- 98.5% unresolved: scripts/10_entity_extraction.py only ever implemented
  the gazetteer match path for entity_subtype; the zero-shot fallback recommended
  at design time for the 98.5% of entity_mentions (109,334/110,983) that gazetteer
  couldn't match was never written. THIS SCRIPT writes it (Stages A-D below).

  BUG 2 -- entity_type inconsistent with matched subtype: 10_entity_extraction.py
  kept whatever entity_type the base NER pipeline assigned even for gazetteer
  hits, instead of forcing the gazetteer's own known-correct type. Confirmed
  again here: 42 gazetteer-matched mentions have entity_type in {LOCATION, ORG}
  paired with entity_subtype=political_leader (25 LOCATION + 17 ORG) even though
  every gazetteer PERSON entry (Modi, Xi, Putin, Zelensky, ...) declares
  entity_type=PERSON -- the base NER model mistagged the span and that bad type
  was kept. Fixed by forcing entity_type from the gazetteer entry whenever
  subtype_source='gazetteer' (Stage 0 below).

--------------------------------------------------------------------------------
BUG 1 FIX -- design

Stage A (cheap, no model calls): two kinds of unresolved mention are left
unresolved ON PURPOSE rather than sent to the classifier, per the task's
explicit instruction that a lower resolution rate with real confidence beats
force-fitting:
  - NER/tokenizer junk: wordpiece remnants ("##News", from mBERT's aggregation
    quirk -- see 10_entity_extraction.py's own docstring), single-character or
    punctuation-only spans. Manual inspection of the 40 highest-volume
    unresolved surface forms per entity_type (this session) found these are
    common artifacts, e.g. a bare "美"/"中"/"京" (Chinese country/city
    shorthand chars split off alone) or "স" (a lone Bengali fragment).
  - LOCATION_BLOCKLIST: bare country/nationality/continent/ocean/sea/capital-
    city names (India, China, the US, Moscow, the Indian Ocean, Middle East,
    ...) plus a handful of directional NER-split fragments ("Middle", "East").
    None of the 5 LOCATION subtypes (port/border_town/economic_zone/pipeline/
    railway) describes "a country" or "a sea" -- every one of these is a
    guaranteed non-match. This corpus turned out to be a broad multi-topic
    OSINT feed (Trump/Netanyahu/NATO/Iran alongside the China/BRI keywords),
    not narrowly China/BRI, so bare country names dominate unresolved LOCATION
    volume (e.g. "India" 3,180 mentions, "China" 3,176, "Iran" 2,862).
    Specific port/border-town names are deliberately NOT blocklisted even when
    they coincide with an active-conflict or contested area (Kharkiv,
    Zaporizhzhia, Sumy, Odesa/Odessa, Chabahar, Bandar Abbas, Karachi,
    Kolkata, Mongla Port, the Bengali-script "চট্টগ্রাম" = Chittagong not
    covered by the ASCII-only gazetteer) -- those are left to the classifier
    in Stage B/C, and several are used in the precision spot-check below.
  ORG has a small parallel junk list (ORG_JUNK_TERMS) for the same pattern:
  Telegram-channel-name fragments and generic single words ("News", "Breaking",
  "Channel", "Security", "Military", ...) that showed up as spurious ORG spans
  and don't correspond to any of the 5 ORG subtypes.

Stage B -- bge-m3 recall floor: embed each surviving mention's context window
(±200 chars around the surface form's first occurrence in its post -- "using
surrounding post context, not just the bare surface form" per the task) against
rich per-subtype descriptions (candidate set scoped to that mention's own
entity_type only -- 4 for PERSON, 5 for ORG, 5 for LOCATION). Keep the top-2
candidates by cosine score, and only advance a mention to Stage C if its best
score clears BIENCODER_FLOOR. This is a cheap recall net, not the precision
decision -- same division of labor as scripts/12_risk_theme_classification.py.

Stage C -- NLI decisive pass: MoritzLaurer/mDeBERTa-v3-base-mnli-xnli (the same
model scripts/12_risk_theme_classification.py validated on this corpus's
multilingual mix, and which that script's own calibration found necessary --
bi-encoder cosine ALONE capped out at ~30-48% precision on this exact corpus/
embedding model, the same compressed-cosine problem named in the RISK/THEME bug
this build is also fixing). For each of a mention's up-to-2 floor-surviving
candidates, score P(entailment) for a short hypothesis naming the entity itself
(e.g. "Chabahar is a seaport used for shipping."), grounded against the context
premise. Accept the higher-scoring candidate as entity_subtype only if its
score clears NLI_THRESHOLD; otherwise the mention stays unresolved. Thresholds
below were picked from a manual read of ~120 stratified candidates (this
session, printed via --calibrate) spanning each entity_type's score bands --
see CALIBRATION NOTES below the constants.

subtype_source for newly-resolved mentions becomes 'zero_shot', subtype_confidence
is the NLI P(entailment). entity_uid is NOT changed for zero-shot resolutions
(no cross-lingual/coref entity linking is attempted here -- e.g. "習近平"/"Xi
Jinping"/"Путин" stay as distinct unresolved_* uids even after their subtype is
filled in; that is a separate, harder problem than subtyping and out of this
script's scope).

Reads FULL corpus from data/analytics.duckdb (read-only, never written) for
post text context, and data/geo_ontology.duckdb entity_dim/entity_mentions
(read_only=True for the read; the actual write is a short-lived read_only=False
connection at the very end -- DuckDB is single-writer and
scripts/12_risk_theme_classification.py may be concurrently mid-run against the
same file, so this script never holds the writer lock during the (slow) model
passes, only for the final CREATE OR REPLACE, with a retry-on-lock loop).

Reproducibility note: 10_entity_extraction.py's gazetteer lives at an ephemeral
/tmp scratchpad path (GAZETTEER_PATH there). This script copies that same file
to data/gazetteer.json (tracked alongside the rest of the pipeline) and reads
it from there, so a re-run of *this* script doesn't depend on a session-scoped
scratchpad surviving.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# ---- lab-env torch/transformers shim (see docs/GEO_ONTOLOGY_CONTEXT.md) ----
try:
    import torch  # noqa: F401
except ImportError:
    _LAB_ENV = ("/home/hariramanan/NTRO/Entity_extraction/.venv/lib/python3.12/"
                "site-packages")
    if _LAB_ENV not in sys.path:
        sys.path.insert(0, _LAB_ENV)

EDA_ROOT = Path(__file__).resolve().parents[1]
SRC_DB = EDA_ROOT / "data" / "analytics.duckdb"
OUT_DB = EDA_ROOT / "data" / "geo_ontology.duckdb"
GAZETTEER_PATH = EDA_ROOT / "data" / "gazetteer.json"
CHECKPOINT_DIR = EDA_ROOT / "data" / "_entity_subtype_checkpoint"

BIENCODER_MODEL = "BAAI/bge-m3"
NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"
METHOD = "bge_m3_recall_floor+mdeberta_v3_nli_xnli_v1"

# ---- CALIBRATION NOTES (this session, --calibrate n=180, stratified across
# entity_type and score decile; manual read of context + candidate + score) ----
# bi-encoder cosine alone: same compressed range as the risk/theme bug (~0.15-
# 0.55 here); a generous low floor is used only to bound NLI volume, never as
# the accept decision. NLI P(entailment): scores <0.45 were dominated by
# generic/wrong fits (e.g. "Erbil" -> port at 0.31, wrong -- Erbil is landlocked);
# scores >=0.55 were consistently correct in the sample (e.g. "Chabahar" -> port
# 0.91, "Syrskyi" -> military_leader 0.88, "European Union" -> international_org
# 0.95); 0.45-0.55 was a mixed gray band. NLI_THRESHOLD=0.55 chosen as the
# conservative side of that band (favors precision over recall, per the task's
# explicit "lower resolution rate with real confidence" instruction).
BIENCODER_FLOOR = 0.25
NLI_THRESHOLD = 0.55
TOP_K_CANDIDATES = 2       # only NLI-check the top-2 bi-encoder candidates/mention
CONTEXT_WINDOW = 220       # chars each side of the surface form's first hit
NLI_MAX_LEN = 256
NLI_BATCH = 32
THREADS = 4                # modest on purpose: scripts/12_risk_theme_classification.py
                            # is concurrently running 6x8=48 threads on this same
                            # 48-core box (confirmed via `ps`/`uptime` this session,
                            # load average ~70) -- this script deliberately uses a
                            # small footprint rather than contend for the whole box.
PROGRESS_EVERY = 5000

# ============================================================================
# Stage A: cheap filters (no model calls)
# ============================================================================

_JUNK_PREFIX_RE = re.compile(r"^##")

def is_junk(sf: str) -> bool:
    """Wordpiece remnants, single-char/punctuation-only spans -- never resolvable."""
    s = (sf or "").strip()
    if not s:
        return True
    if _JUNK_PREFIX_RE.match(s):
        return True
    if len(s) <= 1:
        return True
    if not re.search(r"\w", s, re.UNICODE):
        return True
    return False


# Bare country/nationality/continent/ocean/sea/capital-city names: none of the
# 5 LOCATION subtypes (port/border_town/economic_zone/pipeline/railway) fits
# any of these, confirmed by manual inspection of the unresolved LOCATION
# volume this session. NOT blocklisted: specific cities/towns even in
# contested/active-conflict areas (Kharkiv, Zaporizhzhia, Sumy, Odesa) --
# those are left to the Stage B/C classifier.
LOCATION_BLOCKLIST = {x.lower() for x in [
    "india", "china", "iran", "us", "usa", "u.s.", "u. s.", "u. s", "united states",
    "america", "russia", "ukraine", "pakistan", "israel", "myanmar", "bangladesh",
    "uk", "britain", "england", "scotland", "wales", "japan", "europe", "hong kong",
    "kiev", "kyiv", "lebanon", "kuwait", "france", "jordan", "australia", "tehran",
    "saudi arabia", "saudi", "washington", "germany", "bahrain", "middle east",
    "middle", "east", "west", "north", "south", "indo", "indo-pacific",
    "indo - pacific", "taiwan", "spain", "new delhi", "delhi", "iraq", "canada",
    "indian ocean", "indonesia", "yemen", "asia", "philippines", "white house",
    "arunachal pradesh", "poland", "south asia", "manipur", "gaza", "venezuela",
    "qatar", "turkey", "uae", "malaysia", "pacific", "argentina", "south china sea",
    "gulf", "nepal", "syria", "thailand", "london", "singapore", "south korea",
    "africa", "hormuz", "strait of hormuz", "oman", "afghanistan", "seychelles",
    "southeast asia", "ankara", "congress", "islamabad", "moscow", "beijing",
    "north korea", "vietnam", "cambodia", "laos", "sri lanka", "bhutan", "maldives",
    "egypt", "libya", "sudan", "somalia", "ethiopia", "nigeria", "brazil", "mexico",
    "italy", "netherlands", "belgium", "switzerland", "sweden", "norway", "finland",
    "denmark", "austria", "greece", "portugal", "ireland", "new zealand",
    "south africa", "kenya", "tanzania", "uganda", "ghana", "morocco", "algeria",
    "tunisia", "angola", "zimbabwe", "zambia", "mozambique", "cuba", "colombia",
    "peru", "chile", "ecuador", "bolivia", "paraguay", "uruguay", "panama",
    "costa rica", "haiti", "jamaica", "dominican republic", "central asia",
    "caucasus", "balkans", "scandinavia", "gulf of oman", "persian gulf",
    "arabian sea", "bay of bengal", "black sea", "red sea", "mediterranean",
    "atlantic", "pacific ocean", "atlantic ocean", "kashmir", "kremlin", "pentagon",
    "capitol hill", "brussels", "cairo", "riyadh", "dubai", "abu dhabi", "doha",
    "baghdad", "damascus", "kabul", "dhaka", "naypyidaw", "yangon", "male",
    "kathmandu", "thimphu", "hanoi", "bangkok", "jakarta", "manila", "kuala lumpur",
    "seoul", "tokyo", "pyongyang", "canberra", "ottawa", "berlin", "paris", "rome",
    "madrid", "lisbon", "vienna", "athens", "warsaw", "budapest", "bucharest",
    "sofia", "zagreb", "belgrade", "united kingdom", "kazakhstan", "tibet",
    "ladakh", "assam", "balochistan", "texas", "california", "new york",
    "west bengal", "brahmaputra", "crimea",
    # multilingual variants observed in this corpus's mix (zh-cn / ru / hi / bn)
    "中国", "美国", "美", "中", "俄罗斯", "俄", "法国", "台湾", "菲律宾", "新加坡",
    "乌克兰", "古巴", "以色列", "北京", "京", "日本", "印度", "巴基斯坦", "伊朗",
    "朝鲜", "韩国", "德国", "英国", "加拿大", "华盛顿", "伦敦", "委内瑞拉", "巴西",
    "西班牙", "伊拉克", "沙特", "科威特", "欧洲", "泰国", "纽约", "土耳其",
    "сша", "украины", "украине", "россии", "россия", "иран", "ирана", "китай",
    "индия", "пакистан", "россию", "москве", "крыму", "крыма", "рф",
    "चीन", "भारत", "पाकिस्तान", "ईरान", "अमेरिका", "रूस", "यूक्रेन", "बांग्लादेश",
    "ভারত", "ইরান", "ইরা", "ইরানের", "বাংলাদেশ", "যুক্তরাষ্ট্র", "চীন",
    "পাকিস্তান", "রাশিয়া", "ভারতের", "ঢাকা", "ইসরায়েল", "ইসরায়েলে",
]}

# Telegram-channel-name fragments / generic single words: not organizations.
ORG_JUNK_TERMS = {x.lower() for x in [
    "news", "breaking", "channel", "link", "security", "defence", "defense",
    "ir", "ce", "geopolitics", "nationalsecurity", "intels", "intelslav",
    "intelslava", "telegram", "telegram channel", "military", "tel",
    "美国", "中国", "美",
]}


def is_blocked(entity_type: str, surface_form: str) -> bool:
    if is_junk(surface_form):
        return True
    sf = surface_form.strip().lower()
    if entity_type == "LOCATION" and sf in LOCATION_BLOCKLIST:
        return True
    if entity_type == "ORG" and sf in ORG_JUNK_TERMS:
        return True
    return False


# ============================================================================
# Candidate subtype descriptions (bi-encoder recall net) + NLI hypotheses
# (short, single-clause, grounded with the entity's own surface form -- the
# same style scripts/12_risk_theme_classification.py's own calibration found
# works far better than long disjunctive descriptions reused as NLI hypotheses)
# ============================================================================

CANDIDATE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "PERSON": {
        "political_leader": "A head of state, head of government, government minister, party leader, or elected/appointed political office-holder such as a president, prime minister, minister, governor, or party chief.",
        "military_leader": "A military officer or commander such as a general, admiral, army chief, commander-in-chief, or defence-forces chief, acting in a military command capacity rather than as a civilian minister.",
        "diplomat": "A diplomat, foreign-ministry official, ambassador, envoy, or government spokesperson conducting diplomacy or speaking for a government's foreign policy.",
        "business_executive": "A corporate executive, CEO, chairman, founder, or business leader of a company, acting in a commercial or corporate capacity.",
    },
    "ORG": {
        "ministry": "A government ministry, department, or state regulatory agency of a national government, such as a ministry of defence, foreign affairs, or finance, or a national regulator.",
        "state_owned_enterprise": "A state-owned or state-controlled commercial company, such as a national oil company, state shipping line, state construction firm, or state bank, operating commercially but owned by a government.",
        "armed_group": "A military force, armed force, intelligence agency, security service, militant group, or insurgent organization -- whether a state's regular armed forces/intelligence service or a non-state militant or rebel group.",
        "international_org": "A multilateral international or regional organization such as the United Nations, NATO, the European Union, ASEAN, or a regional economic or security bloc with multiple member states.",
        "business_org": "A private commercial company, corporation, or business enterprise not owned by a government, such as a technology firm, media company, or conglomerate.",
    },
    "LOCATION": {
        "port": "A specific named seaport, harbour, or port terminal used for shipping and maritime trade.",
        "border_town": "A specific named town, pass, valley, or locality situated on or near a contested or sensitive international land border or line of control.",
        "economic_zone": "A specific named special economic zone, free-trade zone, industrial corridor, or economic corridor.",
        "pipeline": "A specific named oil, gas, or energy pipeline project.",
        "railway": "A specific named railway line or rail corridor project.",
    },
}

NLI_HYP_TEMPLATES: dict[str, dict[str, str]] = {
    "PERSON": {
        "political_leader": "{name} is a head of state, government minister, or political office-holder.",
        "military_leader": "{name} is a military officer or armed-forces commander.",
        "diplomat": "{name} is a diplomat, foreign-ministry official, or government spokesperson.",
        "business_executive": "{name} is a corporate executive or business leader.",
    },
    "ORG": {
        "ministry": "{name} is a government ministry or state regulatory agency.",
        "state_owned_enterprise": "{name} is a state-owned commercial company.",
        "armed_group": "{name} is a military force, intelligence agency, or armed group.",
        "international_org": "{name} is a multilateral international organization with multiple member states.",
        "business_org": "{name} is a private company not owned by a government.",
    },
    "LOCATION": {
        "port": "{name} is a seaport used for shipping.",
        "border_town": "{name} is a town or locality on an international border.",
        "economic_zone": "{name} is a special economic zone or economic corridor.",
        "pipeline": "{name} is an oil or gas pipeline.",
        "railway": "{name} is a railway line.",
    },
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_gazetteer(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def build_context(text: str, surface_form: str, window: int = CONTEXT_WINDOW) -> str:
    if not isinstance(text, str) or not text:
        return surface_form
    idx = text.lower().find(surface_form.lower())
    if idx == -1:
        return text[: window * 2]
    start = max(0, idx - window)
    end = min(len(text), idx + len(surface_form) + window)
    return text[start:end]


# ============================================================================
# Stage B: bge-m3 recall floor
# ============================================================================

def compute_biencoder_candidates(survivors: pd.DataFrame) -> pd.DataFrame:
    """For each survivor mention, embed its context and score against its
    entity_type's candidate subtypes. Returns rows (mention_id, subtype, score)
    for the top TOP_K_CANDIDATES per mention with score >= BIENCODER_FLOOR."""
    import torch
    torch.set_num_threads(THREADS)
    from sentence_transformers import SentenceTransformer

    t0 = time.time()
    model = SentenceTransformer(BIENCODER_MODEL, device="cpu")
    log(f"bge-m3 loaded in {time.time()-t0:.1f}s")

    # candidate description embeddings, per entity_type
    cand_embed: dict[str, tuple[list[str], np.ndarray]] = {}
    for etype, descs in CANDIDATE_DESCRIPTIONS.items():
        keys = list(descs.keys())
        vecs = model.encode(list(descs.values()), normalize_embeddings=True,
                             convert_to_numpy=True).astype(np.float32)
        cand_embed[etype] = (keys, vecs)

    out_rows = []
    n = len(survivors)
    t_start = time.time()
    BATCH = 256
    for start in range(0, n, BATCH):
        chunk = survivors.iloc[start:start + BATCH]
        ctx_texts = chunk["context"].tolist()
        X = model.encode(ctx_texts, normalize_embeddings=True,
                          convert_to_numpy=True).astype(np.float32)
        for i, row in enumerate(chunk.itertuples(index=False)):
            keys, vecs = cand_embed[row.entity_type]
            sims = vecs @ X[i]
            order = np.argsort(-sims)[:TOP_K_CANDIDATES]
            top1 = sims[order[0]]
            if top1 < BIENCODER_FLOOR:
                continue
            for j in order:
                out_rows.append((row.mention_id, keys[j], float(sims[j])))
        done = min(start + BATCH, n)
        if done % PROGRESS_EVERY < BATCH or done == n:
            elapsed = time.time() - t_start
            rate = done / elapsed if elapsed > 0 else float("nan")
            eta = (n - done) / rate if rate > 0 else float("nan")
            log(f"  ... bi-encoder {done:,}/{n:,}, {elapsed:.0f}s elapsed, "
                f"ETA {eta/60:.1f} min")

    cand = pd.DataFrame(out_rows, columns=["mention_id", "subtype", "biencoder_score"])
    log(f"bi-encoder candidates: {len(cand):,} rows from {cand['mention_id'].nunique():,} "
        f"mentions cleared floor={BIENCODER_FLOOR} (of {n:,} survivors)")
    return cand


# ============================================================================
# Stage C: NLI decisive pass (single process, modest thread count -- see
# THREADS comment above re: concurrent risk/theme job on this same box)
# ============================================================================

def run_nli(cand: pd.DataFrame, survivors: pd.DataFrame) -> pd.DataFrame:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(THREADS)
    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(NLI_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL)
    model.eval()
    log(f"NLI model loaded in {time.time()-t0:.1f}s: {NLI_MODEL}")

    ctx_by_id = survivors.set_index("mention_id")["context"]
    sf_by_id = survivors.set_index("mention_id")["surface_form"]
    etype_by_id = survivors.set_index("mention_id")["entity_type"]

    pairs = []
    meta = []  # (mention_id, subtype, biencoder_score)
    for row in cand.itertuples(index=False):
        etype = etype_by_id.loc[row.mention_id]
        name = sf_by_id.loc[row.mention_id]
        hyp = NLI_HYP_TEMPLATES[etype][row.subtype].format(name=name)
        premise = ctx_by_id.loc[row.mention_id]
        pairs.append((premise, hyp))
        meta.append((row.mention_id, row.subtype, row.biencoder_score))

    n = len(pairs)
    log(f"NLI pass: {n:,} (premise, hypothesis) pairs, {THREADS} threads, "
        f"batch={NLI_BATCH}")

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    ckpt_tag = f"n{n}"
    ckpt_path = CHECKPOINT_DIR / f"{ckpt_tag}.npy"
    if ckpt_path.exists():
        try:
            scores = np.load(ckpt_path)
            if len(scores) == n:
                log(f"resuming from checkpoint {ckpt_path}")
            else:
                scores = None
        except Exception:
            scores = None
    else:
        scores = None

    if scores is None:
        scores = np.empty(n, dtype=np.float32)
        t_start = time.time()
        for start in range(0, n, NLI_BATCH):
            batch = pairs[start:start + NLI_BATCH]
            premises = [p[:1200] for p, _ in batch]
            hyps = [h for _, h in batch]
            enc = tok(premises, hyps, return_tensors="pt", truncation="only_first",
                      max_length=NLI_MAX_LEN, padding=True)
            with torch.no_grad():
                logits = model(**enc).logits
            probs = torch.softmax(logits, dim=-1).numpy()
            scores[start:start + len(batch)] = probs[:, 0]  # id2label[0]=='entailment'
            done = start + len(batch)
            if done % (NLI_BATCH * 40) < NLI_BATCH or done >= n:
                elapsed = time.time() - t_start
                rate = done / elapsed if elapsed > 0 else float("nan")
                eta = (n - done) / rate if rate > 0 else float("nan")
                log(f"  ... NLI {done:,}/{n:,}, {elapsed:.0f}s elapsed, "
                    f"ETA {eta/60:.1f} min")
                np.save(ckpt_path, scores[:done])  # partial checkpoint
        np.save(ckpt_path, scores)

    result = pd.DataFrame(meta, columns=["mention_id", "subtype", "biencoder_score"])
    result["nli_score"] = scores
    return result


def resolve_subtypes(result: pd.DataFrame) -> pd.DataFrame:
    """Pick, per mention_id, the candidate with the highest nli_score; accept
    only if it clears NLI_THRESHOLD."""
    result = result.sort_values("nli_score", ascending=False)
    best = result.groupby("mention_id", as_index=False).first()
    accepted = best[best["nli_score"] >= NLI_THRESHOLD].copy()
    return accepted


# ============================================================================
# Main
# ============================================================================

def open_writer_with_retry(db_path: Path, attempts: int = 6, delay_s: float = 20.0):
    """DuckDB single-writer -- scripts/12_risk_theme_classification.py may still
    be mid-run against the same file. Retry briefly instead of clobbering."""
    last_err = None
    for i in range(attempts):
        try:
            return duckdb.connect(str(db_path), read_only=False)
        except duckdb.IOException as e:
            last_err = e
            log(f"writer lock busy (attempt {i+1}/{attempts}): {e}; "
                f"retrying in {delay_s:.0f}s")
            time.sleep(delay_s)
    raise RuntimeError(f"could not acquire writer lock on {db_path}") from last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", type=int, default=0,
                     help="if >0, run Stage A-C on a stratified sample of this "
                          "many survivors and print diagnostics; do not write the DB")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    if not SRC_DB.exists() or not OUT_DB.exists():
        print(f"ERROR: missing {SRC_DB} or {OUT_DB}")
        return 1
    if not GAZETTEER_PATH.exists():
        print(f"ERROR: gazetteer missing: {GAZETTEER_PATH}")
        return 1

    gaz = load_gazetteer(GAZETTEER_PATH)
    gaz_type_by_uid = {e["entity_uid"]: e["entity_type"] for e in gaz}

    # ---- read (read_only=True; safe alongside a concurrent writer that isn't
    # actively writing right now -- retried below if it briefly is) ----
    con = duckdb.connect(str(OUT_DB), read_only=True)
    mentions = con.execute("SELECT * FROM entity_mentions").df()
    con.close()
    log(f"read entity_mentions: {len(mentions):,} rows")

    n_total = len(mentions)
    n_gaz_before = int((mentions["subtype_source"] == "gazetteer").sum())
    n_unresolved_before = int((mentions["subtype_source"] == "unresolved").sum())

    # ==== BUG 2 FIX: force entity_type from gazetteer for gazetteer matches ====
    gaz_mask = mentions["subtype_source"] == "gazetteer"
    mapped_type = mentions.loc[gaz_mask, "entity_uid"].map(gaz_type_by_uid)
    n_type_fixed = int((mentions.loc[gaz_mask, "entity_type"] != mapped_type).sum())
    mentions.loc[gaz_mask, "entity_type"] = mapped_type
    log(f"BUG 2 fix: forced entity_type from gazetteer for {int(gaz_mask.sum()):,} "
        f"gazetteer-matched mentions ({n_type_fixed:,} rows had a mismatching "
        f"entity_type before this fix)")

    # ==== BUG 1 FIX: zero-shot fallback for unresolved mentions ====
    unresolved = mentions[mentions["subtype_source"] == "unresolved"].copy()
    unresolved["blocked"] = [
        is_blocked(t, s) for t, s in zip(unresolved["entity_type"], unresolved["surface_form"])
    ]
    survivors = unresolved[~unresolved["blocked"]].copy()
    log(f"unresolved mentions: {len(unresolved):,}; junk/blocklist-filtered out: "
        f"{int(unresolved['blocked'].sum()):,}; survivors sent to classifier: "
        f"{len(survivors):,}")

    if args.calibrate:
        rng = np.random.RandomState(args.seed)
        parts = []
        for etype, grp in survivors.groupby("entity_type"):
            k = min(len(grp), max(1, args.calibrate // 3))
            parts.append(grp.sample(n=k, random_state=rng))
        survivors = pd.concat(parts).reset_index(drop=True)
        log(f"--calibrate: sampled {len(survivors):,} survivors "
            f"({dict(survivors['entity_type'].value_counts())})")

    # ---- context text from analytics.duckdb (read-only, always) ----
    src = duckdb.connect(str(SRC_DB), read_only=True)
    posts = src.execute(
        "SELECT post_uid, text FROM post_facts WHERE text IS NOT NULL"
    ).df()
    src.close()
    text_by_post = posts.set_index("post_uid")["text"]
    survivors["post_text"] = survivors["post_uid"].map(text_by_post)
    survivors["context"] = [
        build_context(t, s) for t, s in zip(survivors["post_text"], survivors["surface_form"])
    ]
    survivors = survivors.drop(columns=["post_text"])

    t_start = time.time()
    cand = compute_biencoder_candidates(survivors)
    nli_result = run_nli(cand, survivors)
    accepted = resolve_subtypes(nli_result)
    log(f"Stage B+C done in {(time.time()-t_start)/60:.1f} min: "
        f"{len(accepted):,}/{len(survivors):,} survivors accepted "
        f"(NLI_THRESHOLD={NLI_THRESHOLD})")

    if args.calibrate:
        log("\n--- CALIBRATION SAMPLE (mention_id, entity_type, surface_form, "
            "context[:160], subtype, biencoder_score, nli_score, ACCEPTED?) ---")
        merged = nli_result.sort_values("nli_score", ascending=False)
        merged = merged.merge(
            survivors[["mention_id", "entity_type", "surface_form", "context"]],
            on="mention_id", how="left",
        )
        pd.set_option("display.max_colwidth", 160)
        for row in merged.itertuples(index=False):
            acc = "ACCEPT" if row.nli_score >= NLI_THRESHOLD else "reject"
            ctx_disp = str(row.context).replace("\n", " ")[:160]
            print(f"[{acc}] {row.entity_type:8s} '{row.surface_form}' -> "
                  f"{row.subtype:22s} bi={row.biencoder_score:.3f} "
                  f"nli={row.nli_score:.3f}  ctx: {ctx_disp}")
        return 0

    # ==== merge accepted zero-shot resolutions back into entity_mentions ====
    accepted_map = accepted.set_index("mention_id")
    zs_mask = mentions["mention_id"].isin(accepted_map.index)
    mentions.loc[zs_mask, "entity_subtype"] = mentions.loc[zs_mask, "mention_id"].map(
        accepted_map["subtype"])
    mentions.loc[zs_mask, "subtype_confidence"] = mentions.loc[zs_mask, "mention_id"].map(
        accepted_map["nli_score"])
    mentions.loc[zs_mask, "subtype_source"] = "zero_shot"

    n_zero_shot = int(zs_mask.sum())
    n_unresolved_after = int((mentions["subtype_source"] == "unresolved").sum())
    log(f"\nnewly resolved via zero-shot: {n_zero_shot:,} mentions")
    log(f"unresolved before: {n_unresolved_before:,} ({100*n_unresolved_before/n_total:.1f}%)")
    log(f"unresolved after:  {n_unresolved_after:,} ({100*n_unresolved_after/n_total:.1f}%)")

    # ==== rebuild entity_dim from corrected entity_mentions (same aggregation
    # logic as 10_entity_extraction.py's original build, applied to the fixed
    # input) ====
    def canonical_name(uid: str, sub: pd.DataFrame) -> str:
        for e in gaz:
            if e["entity_uid"] == uid:
                return e["surface_forms"][0]
        return sub["surface_form"].value_counts().idxmax()

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
            avg_subtype_confidence=("subtype_confidence", "mean"),
        )
        .reset_index()
    )
    canon = mentions.groupby("entity_uid").apply(
        lambda sub: canonical_name(sub.name, sub), include_groups=False
    )
    agg["canonical_name"] = agg["entity_uid"].map(canon)
    entity_dim = agg[
        [
            "entity_uid", "canonical_name", "entity_type", "entity_subtype",
            "subtype_source", "mention_count", "distinct_posts",
            "first_seen", "last_seen", "avg_ner_score", "avg_subtype_confidence",
        ]
    ].sort_values("mention_count", ascending=False).reset_index(drop=True)

    mentions["subtype_resolved_at"] = datetime.now(timezone.utc).isoformat()
    mentions["subtype_method"] = mentions["subtype_source"].map({
        "gazetteer": "gazetteer", "zero_shot": METHOD, "unresolved": None,
    })

    # ==== write (short-lived writer, retried if the risk/theme job is mid-write) ====
    out = open_writer_with_retry(OUT_DB)
    try:
        out.register("_entity_mentions", mentions)
        out.execute("CREATE OR REPLACE TABLE entity_mentions AS SELECT * FROM _entity_mentions")
        out.unregister("_entity_mentions")
        out.register("_entity_dim", entity_dim)
        out.execute("CREATE OR REPLACE TABLE entity_dim AS SELECT * FROM _entity_dim")
        out.unregister("_entity_dim")
    finally:
        out.close()

    log(f"\nwrote {OUT_DB}")
    log(f"  entity_mentions: {len(mentions):,} rows")
    log(f"  entity_dim: {len(entity_dim):,} rows")

    import shutil
    if CHECKPOINT_DIR.exists():
        shutil.rmtree(CHECKPOINT_DIR, ignore_errors=True)
        log(f"cleared checkpoint dir {CHECKPOINT_DIR}")

    log(f"\ntotal wall time: {(time.time()-t_start)/60:.1f} min")

    print("\n--- SUMMARY ---")
    print(f"gazetteer entity_type fixes (BUG 2):     {n_type_fixed:,}/{int(gaz_mask.sum()):,} "
          f"gazetteer mentions had entity_type corrected")
    print(f"subtype coverage before: "
          f"{100*(n_total-n_unresolved_before)/n_total:.1f}% "
          f"({n_total-n_unresolved_before:,}/{n_total:,})")
    print(f"subtype coverage after:  "
          f"{100*(n_total-n_unresolved_after)/n_total:.1f}% "
          f"({n_total-n_unresolved_after:,}/{n_total:,})")
    print(f"  of which gazetteer: {n_gaz_before:,}, zero_shot (new): {n_zero_shot:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
