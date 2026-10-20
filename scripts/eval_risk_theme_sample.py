"""
Utility (not a numbered pipeline stage): draws a fresh stratified sample (2 posts/category,
21 categories = up to 42 rows) from the CURRENT risk_labels/theme_labels tables for hand
precision-judging, plus prints label-coverage stats. Run AFTER
scripts/12_risk_theme_classification.py has completed and written its output (check its log
for a final "wrote ... risk_labels=... theme_labels=..." line -- geo_ontology.duckdb must not
be open read_only=False by another process when this runs, though this script itself only
opens read_only=True so it's safe to run concurrently with anything else that's read-only).

Usage: python3 scripts/eval_risk_theme_sample.py
Output: scripts/../data/_eval_after_sample.csv (path below) -- read it and hand-judge each
row's (post text, category) pair as TP/FP against CATEGORY_DESCRIPTIONS in
scripts/12_risk_theme_classification.py to get a fresh post-fix precision number, directly
comparable in methodology (same n=42, same 2/category stratification, different seed string
so different actual rows) to the pre-fix baseline sample already judged this session.
"""
import duckdb

con = duckdb.connect('/home/hariramanan/NTRO/EDA/data/geo_ontology.duckdb', read_only=True)
con.execute("ATTACH '/home/hariramanan/NTRO/EDA/data/analytics.duckdb' AS a (READ_ONLY)")
df = con.execute('''
WITH r AS (
  SELECT post_uid, category, score, gray_zone, 'risk' AS grp FROM risk_labels
  UNION ALL
  SELECT post_uid, category, score, gray_zone, 'theme' AS grp FROM theme_labels
),
ranked AS (
  SELECT *, row_number() OVER (PARTITION BY category ORDER BY hash(post_uid || category || 'after_seed_v3')) rn
  FROM r
)
SELECT ranked.post_uid, ranked.grp, ranked.category, ranked.score, ranked.gray_zone,
       substr(a.post_facts.text,1,350) AS text350, a.post_facts.lang
FROM ranked
JOIN a.post_facts ON a.post_facts.post_uid = ranked.post_uid
WHERE rn <= 2
ORDER BY grp, category, rn
''').df()
out = '/home/hariramanan/NTRO/EDA/data/_eval_after_sample.csv'
df.to_csv(out, index=False)
print(len(df), '->', out)

# also coverage + counts for the report
print("\n-- risk_labels per category --")
print(con.execute("SELECT category, COUNT(*) n, ROUND(AVG(score),3) avg_score FROM risk_labels GROUP BY 1 ORDER BY 2 DESC").fetchall())
print("\n-- theme_labels per category --")
print(con.execute("SELECT category, COUNT(*) n, ROUND(AVG(score),3) avg_score FROM theme_labels GROUP BY 1 ORDER BY 2 DESC").fetchall())

n_risk = con.execute("SELECT COUNT(DISTINCT post_uid) FROM risk_labels").fetchone()[0]
n_theme = con.execute("SELECT COUNT(DISTINCT post_uid) FROM theme_labels").fetchone()[0]
n_either = con.execute("SELECT COUNT(*) FROM (SELECT post_uid FROM risk_labels UNION SELECT post_uid FROM theme_labels)").fetchone()[0]
n_total_posts = con.execute("SELECT COUNT(*) FROM a.post_facts").fetchone()[0]
n_embed_posts = con.execute("SELECT COUNT(DISTINCT post_uid) FROM a.post_embeddings WHERE embedding IS NOT NULL").fetchone()[0]
print(f"\ndistinct posts w/ >=1 risk label: {n_risk:,}")
print(f"distinct posts w/ >=1 theme label: {n_theme:,}")
print(f"distinct posts w/ >=1 risk OR theme label: {n_either:,}")
print(f"coverage vs all posts ({n_total_posts:,}): {100*n_either/n_total_posts:.1f}%")
print(f"coverage vs posts-with-embeddings ({n_embed_posts:,}): {100*n_either/n_embed_posts:.1f}%")
con.close()
