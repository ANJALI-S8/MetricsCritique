# Key Findings — ShaktiDB Social EDA

Generated from descriptive EDA snapshot results (`_raw_results.json`).

## Corpus overview

- **Total rows (snapshot):** 106,003
- **DB size:** ~756 MB
- **Unified platforms:** reddit, facebook, telegram, instagram, linkedin, youtube_video, pinterest, snapchat

| Table | Rows |
|---|---:|
| `facebook_posts` | 230 |
| `instagram_posts` | 337 |
| `linkedin_posts` | 924 |
| `pinterest_posts` | 78 |
| `reddit_posts` | 2,107 |
| `snapchat_posts` | 177 |
| `telegram_posts` | 23,325 |
| `whatsapp_posts` | 0 |
| `youtube_comments` | 76,520 |
| `youtube_videos` | 2,305 |

## Blocking findings for metrics

1. **No per-post engagement history** — each post observed once; true decay needs local snapshot accrual.
2. **Heavy duplicates** — telegram ~37% URL dupes; reddit/instagram/snapchat/pinterest also inflated.

| Table | Dup key rows |
|---|---:|
| `facebook_posts` | 0 |
| `instagram_posts` | 0 |
| `linkedin_posts` | 0 |
| `pinterest_posts` | 0 |
| `reddit_posts` | 0 |
| `snapchat_posts` | 0 |
| `telegram_posts` | 9,044 |
| `youtube_comments` | 0 |
| `youtube_videos` | 0 |

3. **Telegram has no keywords** — ontology/clustering is a hard dependency for topic metrics.
4. **Scrape lag confounds engagement** — lag ranges from sub-day (reddit/fb/ig) to months (youtube/snapchat).

| Table | Median lag (days) | Mean lag (days) |
|---|---:|---:|
| `facebook_posts` | 0.68 | 0.63 |
| `instagram_posts` | 0.59 | 0.57 |
| `linkedin_posts` | 4.19 | 5.08 |
| `pinterest_posts` | — | — |
| `reddit_posts` | 0.05 | 0.60 |
| `snapchat_posts` | 157.28 | 149.85 |
| `telegram_posts` | 3.79 | 10.56 |

5. **Collector ramp-up confounds growth** — volume growth may track scrape effort, not narrative.
6. **Multilingual corpus** — English-only NLP invalid; need multilingual models.

- **telegram:** en=14537, unknown=2626, ru=1558, zh-cn=1555, bn=1129
- **snapchat:** (unknown)=167, en=5, hi=5
- **youtube_video:** (unknown)=2046, en=192, hi=46, my=6, bn=4

7. **Engagement coverage is platform-specific** — Reddit has zero engagement fields; Snapchat/Pinterest largely unusable for time series.
8. **Short text** — YouTube comments median ~59 chars; readability length-gated.
9. **YouTube video embeddings missing** — compute bge-m3 locally for clustering (never write back to prod).
10. **Effective history ~6 weeks** — cap long windows (EMA/baseline) at ~21 days.

## Top keywords (unified)

| Keyword | Count |
|---|---:|
| iran israel war | 175 |
| युद्ध भारत | 87 |
| neet | 82 |
| terror attack | 78 |
| stock market | 68 |
| viral news | 65 |
| public opinion | 65 |
| startup | 65 |
| Zaobao SG | 64 |
| breaking event | 63 |
| education | 62 |
| political protest | 62 |
| military movement india | 62 |
| indian army | 61 |
| nifty | 60 |

## YouTube deep dive (snapshot)
- Transcripts available: **258/2305** videos
- Importance buckets: (none)=1979, MEDIUM=220, HIGH=76, LOW=30
- Comment replies vs top-level: 25,621 replies / 50,899 top-level

## Next steps

Proceed with the 5-layer pipeline (extract → diagnostics → ontology → NLP → metrics → validation).
See plan / `README.md` for run order. Production Postgres remains **read-only**.
