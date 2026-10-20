Assessment: Is this correct, right, and adoptable for social media?

Short answer:
Yes for a serious OSINT / social-intelligence analytics layer — with clear boundaries.
No if you treat it as a finished, brand-safe “social listening product” or a hashtag firehose like Brandwatch/Sprinklr out of the box.

What you have is statistically much more correct than typical scraped-social dashboards, and adoptable as an internal decision-support system — not as uncritical automated truth.

───

1. What is actually solid (worth adopting)

Engineering posture — correct for live social data
┌─────────────────────────────────────────────┬──────────────────────────────────────────────────────────────┐
│ Choice                                      │ Why it’s right for social media                              │
├─────────────────────────────────────────────┼──────────────────────────────────────────────────────────────┤
│ Production DB read-only                     │ Scrapers keep writing; analytics must not corrupt collection │
├─────────────────────────────────────────────┼──────────────────────────────────────────────────────────────┤
│ Snapshot / local DuckDB                     │ Live corpus drifts; metrics need a frozen extract            │
├─────────────────────────────────────────────┼──────────────────────────────────────────────────────────────┤
│ Dedup (Telegram 23k → 14.6k)                │ Social scrapes double-count; raw volume is a lie             │
├─────────────────────────────────────────────┼──────────────────────────────────────────────────────────────┤
│ Collection-effort normalisation             │ Volume often tracks how hard you scrape, not narrative heat  │
├─────────────────────────────────────────────┼──────────────────────────────────────────────────────────────┤
│ Windowing (live window vs YouTube backfill) │ Old published_at is not “today’s social signal”              │
├─────────────────────────────────────────────┼──────────────────────────────────────────────────────────────┤
│ Support gates (support_ok)                  │ Better to show NULL than a fake spike                        │
└─────────────────────────────────────────────┴──────────────────────────────────────────────────────────────┘

That pattern is how mature social-intel systems should work. Many commercial tools hide these problems instead of modelling them.

Statistics — v1 was not trustworthy; v2 largely is
Claude’s review found real defects. Empirically:

┌────────────────────────────┬────────────────────────────┬────────────────────────────────────────────────────────────┐
│ Issue                      │ v1                         │ v2 (now)                                                   │
├────────────────────────────┼────────────────────────────┼────────────────────────────────────────────────────────────┤
│ Sparse-cell “intensity”    │ |z| up to 143 on 0–2 posts │ surprise max ~3.1, 0 scores on V≤2                         │
├────────────────────────────┼────────────────────────────┼────────────────────────────────────────────────────────────┤
│ Panel sparsity             │ 91% cells V≤2              │ Gated: 345 publishable cells of 1,522                      │
├────────────────────────────┼────────────────────────────┼────────────────────────────────────────────────────────────┤
│ Historical pollution       │ ~82% cells pre-window      │ Trends only 2026-06-14 → 07-28                             │
├────────────────────────────┼────────────────────────────┼────────────────────────────────────────────────────────────┤
│ Null-control / sensitivity │ Logic bugs                 │ Self-test corr(default,default)=1.0; structure tests fixed │
├────────────────────────────┼────────────────────────────┼────────────────────────────────────────────────────────────┤
│ Count model                │ Gaussian z on counts       │ NegBinom (24/25 series prefer NB over Poisson)             │
└────────────────────────────┴────────────────────────────┴────────────────────────────────────────────────────────────┘

So: adopting v1 metrics as-is would have been wrong.
Adopting v2 metrics for “unusual volume / emergence” is directionally right.

Social-media realities that the design respects
• Cross-platform engagement is not comparable → virality is within-platform / age-aware, not one global score
• No follower graph → reach is labelled a proxy (honest)
• Multilingual war/terror corpus → multilingual models; hate gated (hate_calibrated=false)
• Telegram has no keywords → topic clusters as primary entity (correct for this corpus)
• Hashtags cover ~18% of posts → alerts should not be hashtag-primary (documented)

Those are the right calls for OSINT-style social media, not consumer marketing listening.

───

2. What is not fully “correct” yet (do not over-claim)

A. This is your collection, not “the internet”
Metrics measure what you scraped (channels, keywords, platforms, lag).

• Telegram dominates (~14k posts) → “global narrative” will look Telegram-heavy
• Snapchat/Pinterest/WhatsApp are weak or empty
• Scrape lag (hours → months by platform) still confounds engagement age

Adoption rule: Frame outputs as “signals in the NTRO social corpus”, not “public opinion.”

B. Ontology is usable but coarse
Topics like Middle-East / Russia-Ukraine / Cyber are useful first-pass taxonomy, but:
• Labels came from clustering + heuristics, not human curation
• ~4k posts still unassigned
• YouTube embeddings incomplete → novelty/topic quality weaker there
• Two clusters can share a domain label (e.g. two RU-UA clusters)

Adoption rule: Good for triage and routing; not yet a gold ontology for legal/strategic citation without analyst review.

C. Hate / toxicity is not ready to publish
Off-the-shelf toxicity on war reporting over-fires on quoted violence. The system correctly keeps hate_calibrated=false.

Adoption rule: Use only after hand-label calibration; never in automated public-facing flags yet.

D. Alerts are calibrated statistically, not operationally
FDR q-values control false discoveries among many tests — good science.

They do not yet encode:
• analyst priority (border incident vs meme)
• source reliability
• coordination vs organic virality with high confidence

Also: many of the 34 candidates are tag_platform surprise — fine as secondary, but primary ops should still prefer topic_platform (coverage).

E. Half-life / acceleration are better, still short-history
~6 weeks of dense history, median AR(1) half-life ~1.2 days on fitted series.
That can be real (fast social churn) or underpowered. Treat persistence as exploratory.

F. Engagement history is still thin
Per-post decay needs multi-day engagement_snapshots. You’re only starting that accrual.

───

3. “Can this be adopted for social media?” — by use case

┌────────────────────────────────────────────────────────────────┬─────────┬─────────────────────────────────────────────────────────────┐
│ Use case                                                       │ Adopt?  │ How                                                         │
├────────────────────────────────────────────────────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ Internal OSINT / narrative monitoring (Telegram-heavy, defence │ Yes     │ Primary product path                                        │
│ /geo topics)                                                   │         │                                                             │
├────────────────────────────────────────────────────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ “What is spiking today?” triage for analysts                   │ Yes     │ alert_candidates + dashboard, with human in the loop        │
├────────────────────────────────────────────────────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ Cross-platform brand / marketing listening                     │ Partial │ Wrong corpus design; hashtag coverage too low; engagement   │
│                                                                │         │ fields uneven                                               │
├────────────────────────────────────────────────────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ Automated blocking / legal enforcement on hate                 │ No      │ Uncalibrated toxicity                                       │
├────────────────────────────────────────────────────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ Causal claims (“this post caused that”)                        │ No      │ Observational, single-snapshot engagement                   │
├────────────────────────────────────────────────────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ True audience reach / influence ranking                        │ No      │ No follower graph; proxy only                               │
├────────────────────────────────────────────────────────────────┼─────────┼─────────────────────────────────────────────────────────────┤
│ Replace commercial social suites wholesale                     │ No      │ You built a metrics + risk layer, not a full social CRM     │
└────────────────────────────────────────────────────────────────┴─────────┴─────────────────────────────────────────────────────────────┘

───

4. Verdict matrix

┌───────────────────────────────────────────────┬───────────────────────────┬──────────────────────────────────────────────────┐
│ Dimension                                     │ Grade                     │ Comment                                          │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ Data hygiene (dedup, lag, effort)             │ A−                        │ Strong for scraped social                        │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ Statistical honesty (v2)                      │ A−                        │ Major defects found and fixed                    │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ Platform coverage balance                     │ C+                        │ Telegram-weighted by design of collection        │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ Topic model quality                           │ B−                        │ Useful, needs human curation                     │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ Engagement science                            │ B                         │ Age-aware where possible; history still weak     │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ Safety (hate/toxicity)                        │ B (process) / D (publish) │ Gated correctly; not calibrated                  │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ Product readiness for ops                     │ B                         │ Dashboard + metrics DB exist; notifier not built │
├───────────────────────────────────────────────┼───────────────────────────┼──────────────────────────────────────────────────┤
│ General “social media industry” best practice │ B+ as OSINT analytics     │ C as generic social listening                    │
└───────────────────────────────────────────────┴───────────────────────────┴──────────────────────────────────────────────────┘

───

5. What “adoption” should mean in practice

Adopt now (safe):
1. Use data/metrics.duckdb + UI/index.html for daily analyst review
2. Key ops on topic × platform × day, not hashtags
3. Treat surprise / emergence / burst as “look here,” not “this is true”
4. Keep production read-only; re-run extract → v2 on a schedule
5. Keep hate off automated actions

Do before calling it production-grade intel:
1. Hand-label ~200 posts for tone/hate; set thresholds
2. Analyst-edit the 8 topic cards into a stable taxonomy
3. Daily extract cron (engagement snapshots)
4. Free GPU → finish YouTube embeddings / novelty
5. Build notifier only on alert_candidates with human confirm

Do not adopt:
• v1 intensity_z / old validation sensitivity numbers
• Hashtag-only alerting
• Uncalibrated hate scores in briefings

───

6. Bottom line

┌───────────────────────────────────────────────┬─────────────────────────────────────────────────────────────┐
│ Question                                      │ Answer                                                      │
├───────────────────────────────────────────────┼─────────────────────────────────────────────────────────────┤
│ Is the approach right for social media OSINT? │ Yes — effort-aware, deduped, gated, multi-platform-aware    │
├───────────────────────────────────────────────┼─────────────────────────────────────────────────────────────┤
│ Is v1 correct enough to adopt?                │ No                                                          │
├───────────────────────────────────────────────┼─────────────────────────────────────────────────────────────┤
│ Is v2 correct enough to adopt for monitoring? │ Yes, with analyst oversight                                 │
├───────────────────────────────────────────────┼─────────────────────────────────────────────────────────────┤
│ Is it “the truth about social media”?         │ No — it’s the truth about your corpus under your collectors │
├───────────────────────────────────────────────┼─────────────────────────────────────────────────────────────┤
│ Ready for automated high-stakes decisions?    │ Not yet (hate, ontology, short history)                     │
└───────────────────────────────────────────────┴─────────────────────────────────────────────────────────────┘

One sentence:
You have built a credible, scientifically defensible social-signal analytics stack for OSINT-style monitoring; it should be adopted as a decision-support system, not as an autonomous oracle — and only the v2 metrics layer, not the first-pass robust-z panel.

If you want next, I can turn this into a short go/no-go adoption checklist for your ops team (1 page: what to trust, what to ignore, who signs off).