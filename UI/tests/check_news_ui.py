#!/usr/bin/env python3
"""Browser test of the news dashboard (headless Chromium via Playwright). Read-only: it only loads pages
from an already-running UI/news_server.py and clicks around. Starts no pipeline and writes nothing but
screenshots.

  .venv/bin/python UI/tests/check_news_ui.py --url http://127.0.0.1:8878 --shots /path/to/dir
  (needs: pip install playwright; a Chromium binary - set CHROME=/path/to/chrome if not auto-detected)

Exit code 0 = every check passed.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def chrome_path() -> str:
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for pat in ("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome", "/usr/bin/chromium-browser", "/snap/bin/chromium"):
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1]
    raise SystemExit("no Chromium found; set CHROME=")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8878")
    ap.add_argument("--shots", default=".")
    a = ap.parse_args()
    shots = Path(a.shots)
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=chrome_path(), headless=True, args=["--no-sandbox"])
        ctx = br.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        errors, bad_req, dialogs = [], [], []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: bad_req.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

        # ---------- load + first AOI ----------
<<<<<<< HEAD
        page.goto(a.url + "/news.html")
=======
        page.goto(a.url + "/")
>>>>>>> origin/main
        page.wait_for_selector("#ev-tbl tbody tr", timeout=60000)
        sub = page.inner_text("#n-sub")
        check("header shows database, range and read-only", "read-only" in sub and "articles published" in sub, sub[:120])
        check("alert export reported in sync", "in sync" in sub, sub[-60:])
        check("map has country paths", page.locator("#map svg path[data-iso]").count() > 200)
        n_ev = page.locator("#ev-tbl tbody tr").count()
        check("events table populated", n_ev > 20, f"{n_ev} rows")
        check("coverage shows unlocated share", "NO event location" in page.inner_text("#cov"))
        check("KPI tiles present", page.locator("#kpis .n-kpi").count() == 6)
        check("topic table populated", page.locator("#nodes tbody tr").count() > 5)
        check("alerts listed", page.locator("#alerts .n-alert").count() > 0)
        check("methodology lists definitions + unsupported metrics", "Metric definitions (19)" in page.inner_text("#method")
              and "likes" in page.text_content("#method"))        # inside a collapsed <details>: textContent, not innerText
        for i, sec in enumerate(["h-aoi", "h-cov", "h-ev", "h-nodes", "h-al"]):
            page.locator(f"#{sec}").locator("xpath=..").screenshot(path=str(shots / f"01{'abcde'[i]}_{sec}.png"))
        check("page is not absurdly tall", page.evaluate("document.documentElement.scrollHeight") < 6500,
              str(page.evaluate("document.documentElement.scrollHeight")) + "px")

        # NULLs are rendered as an em dash, never 0, where unsupported
        txt = page.inner_text("#nodes")
        check("unsupported cells render as dash", "—" in txt)

        # ---------- event drawer ----------
        page.locator("#ev-tbl tbody tr").first.click()
        page.wait_for_selector("#dr-body table.n-tbl tbody tr", timeout=30000)
        body = page.inner_text("#dr-body")
        page.wait_for_timeout(400)                                          # let the slide-in animation finish
        heads = page.eval_on_selector_all("#dr-body thead th", "e=>e.map(x=>x.textContent)")
        check("drawer shows event location vs publisher country separately",
              "Event location" in body and "Pub. country" in heads and "Event location (per article)" in heads
              and "NOT the event location" in page.content())
        # every column of the article table is visible without horizontal scrolling inside the drawer
        wrap = page.locator("#dr-body .n-tbl-wrap").first
        check("drawer table fits (no hidden columns)", wrap.evaluate("e=>e.scrollWidth <= e.clientWidth + 2"))
        check("drawer lists supporting articles", page.locator("#dr-body table.n-tbl tbody tr").count() >= 3)
        check("article links are http(s) and open safely",
              all(h.startswith("http") for h in page.eval_on_selector_all("#dr-body a", "els=>els.map(e=>e.href)"))
              and all("noopener" in (r or "") for r in page.eval_on_selector_all("#dr-body a", "els=>els.map(e=>e.rel)")))
        page.screenshot(path=str(shots / "02_event_drawer.png"))      # after the animation
        page.keyboard.press("Escape")
        check("Escape closes the drawer", "drawer" not in (page.evaluate("document.body.className") or ""))

        # ---------- node chart ----------
        page.locator("#nodes tbody tr").first.click()
        page.wait_for_selector("#node-chart svg", timeout=10000)
        check("clicking a topic draws baseline chart", "own baseline" in page.inner_text("#node-chart"))

        # ---------- sorting + filters ----------
        page.locator("#ev-tbl th[data-k=n_publishers]").click()
        first = int(page.locator("#ev-tbl tbody tr:first-child td:nth-child(4)").inner_text())
        second = int(page.locator("#ev-tbl tbody tr:nth-child(2) td:nth-child(4)").inner_text())
        check("sort by publishers descending", first >= second, f"{first} >= {second}")
        page.check("#ev-filters input[type=checkbox]")
        em = page.locator("#ev-tbl tbody tr").count()
        check("emerging-only filter works", 0 < em < n_ev and "emerging" in page.inner_text("#ev-tbl tbody tr:first-child").lower(), f"{em} rows")
        page.uncheck("#ev-filters input[type=checkbox]")

        # ---------- state: admin-1 ----------
        opts = page.eval_on_selector_all("#sel-adm1 option", "els=>els.map(e=>[e.value,e.textContent])")
        check("admin-1 list loaded for India", len(opts) > 20, f"{len(opts)} options")
        code = next(v for v, t in opts if "(" in t and v)
        page.select_option("#sel-adm1", code)
        page.wait_for_function("document.getElementById('aoi-status').textContent.indexOf('Pre-computed') === 0 && document.getElementById('aoi-status').textContent.indexOf('(IN)') > 0", timeout=60000)
        check("admin-1 AOI loads its own bundle", "adm1" in page.inner_text("#aoi-status") or "(IN)" in page.inner_text("#aoi-status"), page.inner_text("#aoi-status"))
        page.screenshot(path=str(shots / "03_adm1.png"))

        # ---------- mentioned mode ----------
        page.click("#m-mention")
        page.wait_for_function("document.getElementById('aoi-status').textContent.indexOf('mentioned') >= 0 || document.getElementById('events').textContent.indexOf('Events are defined') >= 0", timeout=60000)
        check("mentioned-in mode does not pretend to have events", "Events are defined for event-location areas only" in page.inner_text("#events"))
        check("mentioned-in mode carries a warning note", "merely name" in page.inner_text("#cov"))
        check("state dropdown disabled in mentioned mode", page.is_disabled("#sel-adm1"))
        page.click("#m-event")
        page.wait_for_selector("#ev-tbl tbody tr", timeout=60000)

        # ---------- country with no pre-computed AOI (computed on demand) ----------
        page.select_option("#sel-country", "PK")
        page.wait_for_function("document.getElementById('aoi-status').textContent.indexOf('Computed on demand') === 0", timeout=90000)
        check("non-registered country computed on demand and labelled", "Computed on demand" in page.inner_text("#aoi-status"))
        check("thin AOI is reported, not zero-filled", "Topic baselines unavailable" in page.inner_text("#cov") or page.locator("#nodes tbody tr").count() > 0)
        page.select_option("#sel-country", "IN")
        page.wait_for_selector("#ev-tbl tbody tr", timeout=60000)

        # ---------- draw a custom polygon on the map ----------
        page.click("#d-poly")
        box = page.locator("#map svg").bounding_box()
        view = page.eval_on_selector("#map svg", "e=>e.getAttribute('viewBox').split(' ').map(Number)")
        def at(lon, lat):
            x = (lon + 180) / 360 * 1000; y = (90 - lat) / 180 * 500
            return (box["x"] + (x - view[0]) / view[2] * box["width"], box["y"] + (y - view[1]) / view[3] * box["height"])
        for lon, lat in [(72.6, 18.8), (73.4, 18.8), (73.4, 19.5), (72.6, 19.5)]:
            page.mouse.click(*at(lon, lat))
        check("finish enabled after 3+ vertices", page.is_enabled("#d-finish"))
        page.click("#d-finish")
        page.wait_for_function("document.getElementById('aoi-status').textContent.indexOf('ad-hoc') >= 0", timeout=90000)
        check("polygon AOI computed and flagged ad-hoc", "ad-hoc" in page.inner_text("#aoi-status"))
        check("ad-hoc warning shown (no cross-area FDR control)", "multiplicity" in page.inner_text("#cov"))
        check("polygon outline drawn on map", page.locator("#map path.aoi-shape").count() == 1)
        page.locator("#h-aoi").locator("xpath=..").screenshot(path=str(shots / "04_adhoc_polygon.png"))

        # radius + box
        page.click("#d-rad"); page.fill("#d-km", "50"); page.mouse.click(*at(77.2, 28.6))
        page.wait_for_function("document.getElementById('aoi-status').textContent.indexOf('radius') >= 0 || document.getElementById('aoi-status').textContent.indexOf('ad-hoc') >= 0", timeout=90000)
        check("radius AOI works", page.locator("#map path.aoi-shape").count() == 1)

        # ---------- theme + responsive ----------
        page.click("#n-theme")
        check("light theme applies", page.evaluate("document.documentElement.getAttribute('data-theme')") == "light")
        page.screenshot(path=str(shots / "05_light.png"))
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check("no horizontal page overflow at phone width", overflow <= 1, f"overflow {overflow}px")
        page.screenshot(path=str(shots / "06_mobile.png"))

        # ---------- hostile text from the corpus must be inert ----------
        ctx2 = br.new_context(viewport={"width": 1200, "height": 800})
        p2 = ctx2.new_page()
        p2.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        evil = "<img src=x onerror=alert('xss1')><script>alert('xss2')</script>"

        def tamper(route):
            resp = route.fetch()
            j = json.loads(resp.text())
            for e in j.get("events", [])[:5]:
                e["rep_title"] = evil
                e["topic_name"] = evil
            for n in j.get("nodes", [])[:3]:
                n["name"] = evil
            for al in j.get("alerts", [])[:3]:
                al["rationale"] = evil
            route.fulfill(response=resp, body=json.dumps(j))
        p2.route("**/api/news/aoi?*", tamper)
<<<<<<< HEAD
        p2.goto(a.url + "/news.html")
=======
        p2.goto(a.url + "/")
>>>>>>> origin/main
        p2.wait_for_selector("#ev-tbl tbody tr", timeout=60000)
        check("hostile headline rendered as text, no element injected", p2.locator("#events img").count() == 0 and p2.locator("#events script").count() == 0
              and "<img" in p2.inner_text("#events"))
        check("no script executed", not any("xss" in d for d in dialogs))

        check("no console/page errors", not errors, "; ".join(errors[:3]))
        check("no failed requests", not bad_req, "; ".join(bad_req[:3]))
        br.close()
    print(f"\n{'ALL CHECKS PASSED' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
