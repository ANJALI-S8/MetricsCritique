/* Trending news page. Vanilla JS. All corpus text is inserted with textContent (never innerHTML); links are limited to http(s). */
(function () {
  'use strict';
  var S = { meta: null, country: '', tax: '', win: '3d', limit: 10, req: 0, res: null, by: {}, kids: {}, cnames: {} };

  // ---------- helpers ----------
  function $(id) { return document.getElementById(id); }
  function add(e, k) {
    if (k == null || k === false) return;
    if (Array.isArray(k)) { k.forEach(function (x) { add(e, x); }); return; }
    e.appendChild(typeof k === 'object' ? k : document.createTextNode(String(k)));
  }
  function h(tag, attrs) {
    var e = document.createElement(tag), a, i;
    if (attrs) for (a in attrs) {
      if (attrs[a] == null || attrs[a] === false) continue;
      if (a === 'class') e.className = attrs[a];
      else if (a.slice(0, 2) === 'on') e.addEventListener(a.slice(2), attrs[a]);
      else if (a === 'href') { if (/^https?:\/\//i.test(attrs[a])) { e.setAttribute('href', attrs[a]); e.setAttribute('target', '_blank'); e.setAttribute('rel', 'noopener noreferrer'); } }
      else e.setAttribute(a, attrs[a] === true ? '' : attrs[a]);
    }
    for (i = 2; i < arguments.length; i++) add(e, arguments[i]);
    return e;
  }
  function clear(e) { while (e.firstChild) e.removeChild(e.firstChild); return e; }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function when(s) { return s ? String(s).replace('T', ' ').slice(0, 16) + 'Z' : ''; }
  function api(path) {
    return fetch(path).then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j && j.error ? j.error : 'HTTP ' + r.status); return j; }); });
  }
  function badge(t, cls, title) { return h('span', { class: 'n-badge ' + (cls || ''), title: title || null }, t); }
  function errBox(host, m) { clear(host).appendChild(h('div', { class: 'n-err', role: 'alert' }, 'Could not load: ' + m)); }

  function initTheme() {
    var t = 'dark';
    try { t = localStorage.getItem('news-theme') || 'dark'; } catch (e) { /* storage may be unavailable */ }
    document.documentElement.setAttribute('data-theme', t);
    $('n-theme').addEventListener('click', function () {
      t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', t);
      try { localStorage.setItem('news-theme', t); } catch (e) { /* ignore */ }
    });
  }

  // ---------- citations ----------
  function citeNode(c) {
    return h('span', { class: 'n-cite-i' }, [h('b', null, c.publisher || 'unknown publisher'), ' — ',
      c.url ? [h('a', { href: c.url, title: 'Open the original article: ' + c.url }, c.title || '(no title)'), ' ↗'] : [c.title || '(no title)', ' ', badge('no link', 'b-low')],
      h('span', { class: 'n-muted' }, '  · ' + when(c.published_at))]);
  }

  // ---------- taxonomy (Domain > Topic > Subtopic) ----------
  function buildTax() {
    S.meta.taxonomy.forEach(function (t) { S.by[t.code] = t; (S.kids[t.parent || ''] = S.kids[t.parent || ''] || []).push(t); });
    Object.keys(S.kids).forEach(function (k) { S.kids[k].sort(function (a, b) { return a.code < b.code ? -1 : 1; }); });
  }
  function parts(code) { if (!code) return []; var p = code.split('.'), c = '', out = []; p.forEach(function (x, i) { c = i ? c + '.' + x : x; out.push(c); }); return out; }
  function opt(v, label, sel) { return h('option', { value: v, selected: sel }, label); }
  function fillCountry() {
    var s = clear($('t-country')); s.appendChild(opt('', 'Any country', !S.country));
    S.meta.countries.forEach(function (c) { s.appendChild(opt(c.iso2, c.name + ' (' + c.n_stories.toLocaleString() + ')', c.iso2 === S.country)); });
  }
  function fillTax() {
    var p = parts(S.tax), dom = p[0] || '', top = p[1] || '', sub = p[3] || '';
    var d = clear($('t-domain')); d.appendChild(opt('', 'All topics', !dom));
    (S.kids[''] || []).forEach(function (x) { d.appendChild(opt(x.code, x.name, x.code === dom)); });
    var t = clear($('t-topic')); t.appendChild(opt('', dom ? 'All topics in this domain' : '—', !top)); t.disabled = !dom;
    (S.kids[dom] || []).forEach(function (x) { t.appendChild(opt(x.code, x.name, x.code === top)); });
    var u = clear($('t-sub-sel')); u.appendChild(opt('', top ? 'All subtopics in this topic' : '—', !sub)); u.disabled = !top;
    (S.kids[top] || []).forEach(function (cat) {
      var g = h('optgroup', { label: cat.name }); (S.kids[cat.code] || []).forEach(function (x) { g.appendChild(opt(x.code, x.name, x.code === sub)); }); u.appendChild(g);
    });
  }
  function fillWindow() {
    var seg = clear($('t-window'));
    S.meta.windows.forEach(function (w) { seg.appendChild(h('button', { type: 'button', 'aria-pressed': String(w.key === S.win), onclick: function () { S.win = w.key; S.limit = S.meta.top_n; fillWindow(); load(); } }, w.label.replace('last ', 'Last '))); });
  }
  function changed() { S.limit = S.meta.top_n; fillTax(); load(); }

  // ---------- results ----------
  function prompt(msg) {
    clear($('t-results')).appendChild(h('div', { class: 'n-card t-empty' }, [h('b', null, 'Select what you want to follow'), msg || 'Trending depends on the area or topic you care about. Choose a country, a topic, or both above.']));
  }
  function scopeText() {
    var c = S.country ? (S.cnames[S.country] || S.country) : null, t = S.tax && S.by[S.tax] ? S.by[S.tax].name : null;
    return c && t ? t + ' in ' + c : (c ? c + ' — all topics' : t + ' — all countries');
  }
  function load() {
    var my = ++S.req, host = $('t-results');
    if (!S.country && !S.tax) { S.res = null; prompt(); $('t-hint').textContent = 'Choose a country, a topic, or both. With a country only you get its trending stories across every topic; with a topic only, across every country.'; return; }
    $('t-hint').textContent = scopeText() + ' · ' + S.meta.windows.filter(function (w) { return w.key === S.win; })[0].label;
    clear(host).appendChild(h('div', { class: 'n-card n-spin' }, 'Ranking stories…'));
    var q = '/api/news/trending?window=' + encodeURIComponent(S.win) + '&limit=' + S.limit + (S.country ? '&country=' + encodeURIComponent(S.country) : '') + (S.tax ? '&tax=' + encodeURIComponent(S.tax) : '');
    api(q).then(function (r) { if (my !== S.req) return; S.res = r; render(r); }).catch(function (e) { if (my !== S.req) return; errBox(clear(host), e.message); });
  }
  function render(r) {
    var host = clear($('t-results'));
    if (r.needs_selection) { prompt(r.message); return; }
    host.appendChild(h('div', { class: 't-summary' }, [
      h('h2', null, (r.stories.length ? 'Top ' + r.stories.length + ' trending stories' : 'No trending stories') + ' · ' + scopeText()),
      h('span', { class: 'n-muted' }, r.window.label + ' · data up to ' + when(r.as_of) + ' · ranked from ' + r.n_candidates.toLocaleString() + ' active stories (of ' + r.n_scope_stories.toLocaleString() + ' in ' + (S.country ? (S.cnames[S.country] || S.country) : 'all countries') + ')')]));
    (r.notes || []).forEach(function (n) { host.appendChild(h('div', { class: 't-note' }, n)); });
    if (!r.stories.length) { host.appendChild(h('div', { class: 'n-card t-empty' }, [h('b', null, 'Nothing is trending here right now'), (r.notes && r.notes[0]) || 'No story has enough recent activity.', ' Try a longer period or a broader selection.'])); return; }
    var list = h('div', { class: 't-list' }); host.appendChild(list);
    r.stories.forEach(function (s) { list.appendChild(card(s, r)); });
    if (r.stories.length >= S.limit && S.limit < S.meta.max_n && r.n_candidates > r.stories.length)
      host.appendChild(h('button', { class: 't-more', onclick: function () { S.limit = Math.min(S.meta.max_n, S.limit + 10); load(); } }, 'Show more stories'));
  }
  function relCount(s) {   // articles of closely related stories folded under this one (they are in the drawer too)
    var n = (s.related || []).reduce(function (a, x) { return a + x.n_articles; }, 0);
    return n ? ' (+' + n.toLocaleString() + ' in closely related coverage)' : '';
  }
  var TAX_STATUS = { mixed: ['Mixed topics', 'The articles in this story are labelled with different topics and no single one has majority support, so no category is shown.'],
                     outside: ['Outside the taxonomy', 'Most of this story\u2019s articles are labelled Non-relevant / Other.'],
                     unlabelled: ['Unlabelled', 'None of this story\u2019s articles carries a taxonomy label.'] };
  // the path a MAJORITY of the story's labelled articles agree on (Domain › Topic › Category › Subtopic), or an honest status
  function taxChip(s) {
    var t = s.taxonomy, path = [t.domain, t.topic, t.category, t.subtopic].filter(Boolean).map(function (x) { return x.name; });
    if (path.length) return h('span', { class: 't-chip tax', title: 'Domain › Topic › Category › Subtopic that most of this story\u2019s labelled articles agree on' }, path.join(' › '));
    var st = TAX_STATUS[s.taxonomy_status] || TAX_STATUS.unlabelled;
    return h('span', { class: 't-chip tax none', title: st[1] }, st[0]);
  }
  function locChip(s) {
    var l = s.location, name = l.country ? (S.cnames[l.country] || l.country) : null;
    if (!name) return h('span', { class: 't-chip loc', title: 'No usable event location for this story' }, '📍 Location unknown');
    return h('span', { class: 't-chip loc', title: 'Where the event happened (GDELT). ' + (l.uncertain ? 'Uncertain: few located articles, or they disagree.' : '') }, '📍 ' + name + (l.place ? ' · ' + l.place : '') + (l.uncertain ? ' (uncertain)' : ''));
  }
  function card(s, r) {
    var w = s.in_window, wl = r.window.label.replace('last ', '');
    return h('article', { class: 't-card', 'aria-label': 'Trending story ' + s.rank }, [
      h('div', { class: 't-rank', 'aria-hidden': 'true' }, String(s.rank)),
      h('div', null, [
        h('h3', { class: 't-title' }, h('button', { type: 'button', onclick: function () { openStory(s, r); } }, s.title || '(no title)')),
        h('div', { class: 't-chips' }, [taxChip(s), locChip(s)]),
        h('div', { class: 't-stats' }, [h('b', null, s.n_articles.toLocaleString()), ' related articles' + relCount(s) + ' · ', h('b', null, w.articles.toLocaleString()), ' in the ' + wl + ' · ', h('b', null, s.n_publishers.toLocaleString()), ' publishers · latest activity ',
          h('b', null, when(s.latest_activity))]),
        h('ul', { class: 't-why', 'aria-label': 'Why it is trending' }, s.reasons.map(function (t, i) { return h('li', { class: i >= 2 && /pipeline|Flagged|detected|ran ×/.test(t) ? 'ev' : null }, t); })),
        (s.sources && s.sources.length) ? h('div', { class: 't-src' }, [h('span', { class: 'n-muted' }, 'Latest coverage: '), s.sources.map(citeNode)]) : null,
        (s.related && s.related.length) ? h('div', { class: 't-related' }, 'Also covered (closely related): ' + s.related.slice(0, 2).map(function (x) { return x.title + ' (' + x.articles_in_window + ' in the ' + wl + ')'; }).join(' · ')) : null,
        h('div', { class: 't-actions' }, h('button', { type: 'button', onclick: function () { openStory(s, r); } }, 'View related articles'))]),
      h('div', { class: 't-score', title: 'Relative ranking inside this view (weighted percentile of volume, publisher breadth, growth, recency and statistical evidence). Not a probability.' }, [h('b', null, String(Math.round(s.score))), h('span', null, 'Trend score'), h('div', { class: 't-bar' }, h('i', { style: 'width:' + Math.max(2, Math.min(100, s.score)) + '%' }))])]);
  }

  // ---------- story drawer ----------
  function artRow(c, relIds) {
    var meta = [h('span', null, h('b', null, c.publisher || 'unknown publisher')), h('span', null, when(c.published_at))];
    if (c.loc_status === 'located' && (c.place || c.country)) meta.push(h('span', { title: 'event location (GDELT)' }, '📍 ' + (c.place || S.cnames[c.country] || c.country)));
    if (c.tax_subtopic || c.tax_topic) meta.push(h('span', null, '▸ ' + (c.tax_subtopic || c.tax_topic)));
    if (relIds && c.event_id && relIds[c.event_id]) meta.push(h('span', { class: 't-tag', title: 'from a closely related story listed under this one' }, 'related story'));
    return h('div', { class: 'n-art' }, [h('div', { class: 't' }, c.url ? [h('a', { href: c.url, title: 'Open the original article: ' + c.url }, c.title || '(no title)'), ' ↗'] : [c.title || '(no title)', ' ', badge('no link', 'b-low')]), h('div', { class: 'm' }, meta)]);
  }
  function openStory(s, r) {
    var body = clear($('dr-body')), ids = s.event_ids.slice(), rel = {};
    (s.related || []).forEach(function (x) { x.event_ids.forEach(function (e) { ids.push(e); rel[e] = true; }); });
    ids = ids.slice(0, 100);
    $('dr-title').textContent = s.title || 'Story';
    document.body.classList.add('drawer'); $('n-drawer').setAttribute('aria-hidden', 'false');
    var wl = r.window.label.replace('last ', '');
    body.appendChild(h('div', { class: 't-chips', style: 'margin:8px 0' }, [taxChip(s), locChip(s)]));
    body.appendChild(h('div', { class: 't-stats-grid' }, [kv('Related articles', s.n_articles.toLocaleString() + relCount(s).replace(' in closely related coverage', ' related')), kv('Publishers', s.n_publishers.toLocaleString()), kv('In the ' + wl, s.in_window.articles.toLocaleString()),
      kv('First reported', when(s.first_seen)), kv('Latest activity', when(s.latest_activity)), kv('Trend score', String(Math.round(s.score)) + ' / 100')]));
    body.appendChild(h('div', { class: 'n-2', style: 'margin:6px 0 2px' }, h('b', null, 'Why it is trending')));
    body.appendChild(h('ul', { class: 't-why' }, s.reasons.map(function (t) { return h('li', null, t); })));
    if (s.related && s.related.length) body.appendChild(h('div', { class: 't-note' }, 'Includes closely related coverage: ' + s.related.map(function (x) { return x.title + ' (' + x.n_articles + ' articles)'; }).join(' · ')));
    var box = body.appendChild(h('div', { id: 'story-articles' }, h('div', { class: 'n-spin' }, 'loading articles…')));
    function fetchPage(off) { return api('/api/news/story_articles?ids=' + ids.join(',') + '&offset=' + off + '&limit=50'); }
    fetchPage(0).then(function (p) {
      clear(box);
      box.appendChild(h('div', { class: 'n-2', style: 'margin:10px 0 4px' }, [h('b', null, 'Related articles'), ' — ' + p.total.toLocaleString() + ' (newest first)' + (p.exact_copies ? ' · ' + p.exact_copies.toLocaleString() + ' exact copies not listed' : '') + '. Links open the original publisher.']));
      var list = h('div', null, p.items.map(function (c) { return artRow(c, rel); })), shown = p.items.length, btn = null;
      box.appendChild(list);
      function sync() { if (shown >= p.total) { if (btn) btn.remove(); return; } btn = btn || box.appendChild(h('button', { style: 'margin-top:8px', onclick: function () { btn.disabled = true; fetchPage(shown).then(function (q) { q.items.forEach(function (c) { list.appendChild(artRow(c, rel)); }); shown += q.items.length; btn.disabled = false; sync(); }).catch(function (e) { btn.disabled = false; btn.textContent = 'Retry (' + e.message + ')'; }); } }, '')); btn.textContent = 'Show more (' + (p.total - shown).toLocaleString() + ' remaining)'; }
      sync();
    }).catch(function (e) { errBox(clear(box), e.message); });
  }
  function kv(l, v) { return h('div', { class: 'n-kpi' }, [h('span', null, l), h('b', { style: 'font-size:16px' }, v)]); }
  function closeStory() { document.body.classList.remove('drawer'); $('n-drawer').setAttribute('aria-hidden', 'true'); }

  // ---------- method ----------
  function renderHow() {
    var m = S.meta, b = clear($('t-how-body'));
    b.appendChild(h('p', { class: 'n-lede' }, 'A story is a group of articles about the same event (found from the articles’ meaning, not keywords). Near-identical groups are merged, so one story is listed once.'));
    b.appendChild(h('ul', { class: 't-why' }, [
      h('li', null, 'Volume — articles in the chosen period. Breadth — distinct publishers in that period (so one outlet repeating itself does not count as wide coverage).'),
      h('li', null, 'Growth — this period against the one before it (shown only when there are at least 5 articles across both; a story with no earlier coverage is marked “new”).'),
      h('li', null, 'Typical coverage — how the story compares with other multi-publisher stories in the same place and subject in earlier periods. This is a comparison, not a significance test.'),
      h('li', null, 'Statistical evidence — taken only from the pipeline’s own FDR-controlled tests (emerging event, new development, topic surge), and only where they were run (countries pre-computed in the pipeline). Elsewhere no significance claim is made.'),
      h('li', null, 'Trend score — a relative ranking inside the current view: weighted percentile ranks of volume 30%, breadth 30%, growth 20%, recency 10% and evidence 10% (dropped when unavailable).')]));
    b.appendChild(h('div', { class: 't-note' }, 'Limits: country and place come from GDELT’s event location (machine-extracted; available for roughly 35–40% of articles), so stories without a usable location appear only when you select a topic without a country. Topic labels are the stored classifier labels. The data is a pilot snapshot (' + (m.data_start || '').slice(0, 10) + ' → ' + (m.as_of || '').slice(0, 10) + '), centred on India and Bangladesh coverage; other countries appear when those articles report on them.'));
  }

  // ---------- boot ----------
  function boot() {
    initTheme();
    api('/api/news/trending_meta').then(function (m) {
      S.meta = m; m.countries.forEach(function (c) { S.cnames[c.iso2] = c.name; });
      buildTax(); fillCountry(); fillTax(); fillWindow(); renderHow();
      $('t-sub').textContent = 'Data up to ' + when(m.as_of) + ' (' + (m.data_start || '').slice(0, 10) + ' → ' + (m.as_of || '').slice(0, 10) + ') · ' + m.n_stories.toLocaleString() + ' stories from ' + m.n_events.toLocaleString() + ' event clusters · read-only';
      prompt();
    }).catch(function (e) { $('t-sub').textContent = 'could not load'; errBox($('t-results'), e.message); });
    $('t-country').addEventListener('change', function (e) { S.country = e.target.value; changed(); });
    $('t-domain').addEventListener('change', function (e) { S.tax = e.target.value; changed(); });
    $('t-topic').addEventListener('change', function (e) { S.tax = e.target.value || $('t-domain').value; changed(); });
    $('t-sub-sel').addEventListener('change', function (e) { S.tax = e.target.value || $('t-topic').value; changed(); });
    $('t-clear').addEventListener('click', function () { S.country = ''; S.tax = ''; fillCountry(); changed(); });
    $('dr-close').addEventListener('click', closeStory); $('n-backdrop').addEventListener('click', closeStory);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeStory(); });
  }
  boot();
})();
