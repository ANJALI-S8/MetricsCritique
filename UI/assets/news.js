/* News trending & emerging events dashboard. Vanilla JS, no dependencies, works offline.
   All corpus text is inserted with textContent (never innerHTML); links are limited to http(s). */
(function () {
  'use strict';
  var W = 1000, H = 500;                                   // map: equirectangular, 1000 x 500 for 360 x 180 degrees
  var S = { meta: null, mode: 'event', iso: '', adm1: '', adhoc: null, level: 'topic', tax: '', txi: null, bundle: null, selNode: null,
            evSort: { k: 'last_seen', dir: -1 }, evF: { q: '', minPub: 0, emergingOnly: false, conf: 'any' },
            alF: { kind: '', status: 'active' }, draw: null, gaz: {}, geoC: null, geoA: {}, regIds: {}, view: [0, 0, W, H], req: 0 };

  // ---------- helpers ----------
  function $(id) { return document.getElementById(id); }
  function h(tag, attrs) {
    var e = document.createElement(tag), i, a;
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
  function add(e, k) {
    if (k == null || k === false) return;
    if (Array.isArray(k)) { k.forEach(function (x) { add(e, x); }); return; }
    e.appendChild(typeof k === 'object' ? k : document.createTextNode(String(k)));
  }
  function svg(tag, attrs) {
    var e = document.createElementNS('http://www.w3.org/2000/svg', tag), a;
    if (attrs) for (a in attrs) if (attrs[a] != null) e.setAttribute(a, attrs[a]);
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) e.appendChild(arguments[i]);
    return e;
  }
  function clear(e) { while (e.firstChild) e.removeChild(e.firstChild); return e; }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  var NA_TITLE = 'not available: below the minimum evidence, or not defined here (never shown as 0)';
  function na() { return h('span', { class: 'na', title: NA_TITLE }, '—'); }
  function num(v, dp) { return isNum(v) ? (dp == null ? String(Math.round(v)) : v.toFixed(dp)) : na(); }
  function qfmt(q) { return !isNum(q) ? na() : (q < 0.0001 ? '<0.0001' : q.toFixed(4)); }
  function pct(v) { return isNum(v) ? (100 * v).toFixed(1) + '%' : na(); }
  function when(s) { return s ? String(s).replace('T', ' ').slice(0, 16) + 'Z' : ''; }
  function wshort(s) { return s ? String(s).slice(5, 16).replace('T', ' ') : ''; }
  function day(s) { return s ? String(s).slice(0, 10) : ''; }
  function cname(iso) { var c = S.gaz[iso]; return c ? c.name : (iso || ''); }
  function api(path, body) {
    var opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
    return fetch(path, opt).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) throw new Error(j && j.error ? j.error : ('HTTP ' + r.status)); return j; });
    });
  }
  function errBox(host, msg) { clear(host).appendChild(h('div', { class: 'n-err', role: 'alert' }, 'Could not load: ' + msg)); }
  // ---------- citations: article title + publisher, linked to the ORIGINAL source ----------
  function citeNode(c) {
    return h('span', { class: 'n-cite-i' }, [
      h('b', null, c.publisher || 'unknown publisher'), ' — ',
      c.url ? [h('a', { href: c.url, title: 'Open the original article: ' + c.url }, c.title || '(no title)'), ' ↗']
            : [c.title || '(no title)', ' ', badge('no link', 'b-low', 'the source record has no usable URL')],
      h('span', { class: 'n-muted' }, '  · ' + when(c.published_at))]);
  }
  function citeBlock(list, label, more) {
    if (!list || !list.length) return null;
    return h('div', { class: 'n-cites' }, [label ? h('span', { class: 'n-muted' }, label) : null, list.map(citeNode), more || null]);
  }
  function badge(txt, cls, title) { return h('span', { class: 'n-badge ' + (cls || ''), title: title || null }, txt); }

  // ---------- theme ----------
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

  // ---------- charts ----------
  function spark(vals, base) {
    var v = vals.filter(isNum);
    if (v.length < 2) return h('span', { class: 'na', title: NA_TITLE }, '—');
    var all = vals.concat(base || []).filter(isNum), mx = Math.max.apply(null, all), mn = Math.min(0, Math.min.apply(null, all)), w = 110, hh = 26;
    function pts(a) { var out = []; a.forEach(function (y, i) { if (isNum(y)) out.push((i / (a.length - 1) * (w - 2) + 1).toFixed(1) + ',' + (hh - 2 - (y - mn) / ((mx - mn) || 1) * (hh - 4)).toFixed(1)); }); return out.join(' '); }
    var s = svg('svg', { class: 'spark', viewBox: '0 0 ' + w + ' ' + hh, role: 'img', 'aria-label': 'daily article counts' });
    if (base) s.appendChild(svg('polyline', { class: 'bl', points: pts(base) }));
    s.appendChild(svg('polyline', { class: 'ln', points: pts(vals) }));
    return s;
  }
  function nodeChart(host, code) {
    clear(host);
    var rows = (S.bundle.series || {})[code];
    if (!rows || !rows.length) return;
    var node = (S.bundle.nodes || []).filter(function (n) { return n.node_code === code; })[0];
    var w = 900, hh = 220, L = 38, R = 10, T = 12, B = 22, n = rows.length;
    var ys = []; rows.forEach(function (r) { if (isNum(r[1])) ys.push(r[1]); if (isNum(r[2])) ys.push(r[2]); });
    var mx = Math.max.apply(null, ys.concat([1])) * 1.1;
    function X(i) { return L + (n === 1 ? 0 : i / (n - 1)) * (w - L - R); }
    function Y(v) { return T + (1 - v / mx) * (hh - T - B); }
    var s = svg('svg', { class: 'n-chart', viewBox: '0 0 ' + w + ' ' + hh, role: 'img',
                         'aria-label': 'Daily articles versus this area\'s own baseline for ' + (node ? node.name : code) });
    [0, .5, 1].forEach(function (f) { var y = Y(mx * f); s.appendChild(svg('line', { class: 'g', x1: L, x2: w - R, y1: y, y2: y })); var t = svg('text', { x: 4, y: y + 3 }); t.textContent = Math.round(mx * f); s.appendChild(t); });
    function line(idx, cls) {
      var d = '', pen = false;
      rows.forEach(function (r, i) { if (isNum(r[idx])) { d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(r[idx]).toFixed(1); pen = true; } else pen = false; });
      if (d) s.appendChild(svg('path', { class: cls, d: d }));
    }
    line(2, 'bl'); line(1, 'ln');
    rows.forEach(function (r, i) {
      if (r[4]) { var c = svg('path', { class: 'fl', d: 'M' + X(i) + ' ' + (Y(r[1]) - 7) + 'l5 7l-5 7l-5 -7z' }); var t = svg('title'); t.textContent = day(r[0]) + ': ' + r[1] + ' articles, FDR q=' + (isNum(r[3]) ? r[3].toFixed(4) : '—'); c.appendChild(t); s.appendChild(c); }
      if (i % Math.ceil(n / 8) === 0 || i === n - 1) { var tx = svg('text', { x: X(i) - 16, y: hh - 6 }); tx.textContent = r[0].slice(5); s.appendChild(tx); }
    });
    add(host, h('div', { class: 'n-2', style: 'margin-bottom:4px' }, [h('b', null, node ? node.name : code), ' — daily articles (gold line) vs. this area\'s own baseline (dashed); ◆ = FDR-controlled alert day. Gaps = days without enough data (not zero).']));
    host.appendChild(s);
  }

  // ---------- map ----------
  function P(lon, lat) { return [(lon + 180) / 360 * W, (90 - lat) / 180 * H]; }
  function inv(x, y) { return [x / W * 360 - 180, 90 - y / H * 180]; }
  function ringD(r) { var d = ''; for (var i = 0; i < r.length; i++) { var p = P(r[i][0], r[i][1]); d += (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); } return d + 'Z'; }
  function geomD(g) {
    var d = '';
    if (g.type === 'Polygon') g.coordinates.forEach(function (r) { d += ringD(r); });
    else if (g.type === 'MultiPolygon') g.coordinates.forEach(function (pl) { pl.forEach(function (r) { d += ringD(r); }); });
    return d;
  }
  function geomBox(g) {
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    (function walk(a) { if (typeof a[0] === 'number') { var p = P(a[0], a[1]); x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]); y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]); } else a.forEach(walk); })(g.coordinates);
    return [x0, y0, x1, y1];
  }
  function setView(b) {
    var x0 = b[0], y0 = b[1], x1 = b[2], y1 = b[3], MINSPAN = 30;   // never zoom closer than ~11 degrees: simplified boundaries look jagged beyond that
    if (x1 - x0 < MINSPAN) { var cx = (x0 + x1) / 2; x0 = cx - MINSPAN / 2; x1 = cx + MINSPAN / 2; }
    if (y1 - y0 < MINSPAN / 2) { var cy = (y0 + y1) / 2; y0 = cy - MINSPAN / 4; y1 = cy + MINSPAN / 4; }
    var pad = 0.12 * Math.max(x1 - x0, y1 - y0, 20), w = x1 - x0 + 2 * pad, hh = y1 - y0 + 2 * pad, k = Math.max(w / W, hh / H);
    var vw = W * k, vh = H * k; S.view = [(x0 + x1) / 2 - vw / 2, (y0 + y1) / 2 - vh / 2, vw, vh];
    var s = $('map').querySelector('svg'); if (s) s.setAttribute('viewBox', S.view.join(' '));
  }
  function colorStep(n, mx) { return n <= 0 ? 0 : Math.max(1, Math.min(6, Math.ceil(Math.log(n + 1) / Math.log(mx + 1) * 6))); }
  var mapSvg = null, gCountries = null, gAdm = null, gShape = null, gDraft = null;
  function buildMap() {
    var host = clear($('map'));
    mapSvg = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Map: click a country, or draw an area' });
    gCountries = svg('g'); gAdm = svg('g'); gShape = svg('g'); gDraft = svg('g');
    mapSvg.appendChild(gCountries); mapSvg.appendChild(gAdm); mapSvg.appendChild(gShape); mapSvg.appendChild(gDraft);
    host.appendChild(mapSvg);
    host.appendChild(h('div', { class: 'n-map-hint', id: 'map-hint' }, 'Click a country to select it.'));
    mapSvg.addEventListener('click', onMapClick);
    var mx = 1; Object.keys(S.gaz).forEach(function (k) { mx = Math.max(mx, S.gaz[k].n_articles); });
    S.geoC.features.forEach(function (f) {
      var iso = f.properties.id, n = (S.gaz[iso] || {}).n_articles || 0;
      var p = svg('path', { d: geomD(f.geometry), class: n > 0 ? 'has' : '', 'data-iso': iso, style: 'fill:var(--seq-' + colorStep(n, mx) + ')' });
      var t = svg('title'); t.textContent = f.properties.name + (n ? ' — ' + n + ' articles with an event location' : ' — no located articles in this database'); p.appendChild(t);
      p.addEventListener('click', function (ev) { if (S.draw) return; ev.stopPropagation(); pickCountry(iso); });
      gCountries.appendChild(p);
    });
    var lg = clear($('map-legend')); lg.appendChild(document.createTextNode('Located articles in database: fewer '));
    for (var i = 0; i <= 6; i++) lg.appendChild(h('i', { style: 'background:var(--seq-' + i + ')' }));
    lg.appendChild(document.createTextNode(' more (log scale)'));
  }
  function markSelected() {
    gCountries.querySelectorAll('path').forEach(function (p) { p.classList.toggle('sel', p.getAttribute('data-iso') === S.iso && !S.adhoc); });
  }
  function drawAdm(iso) {
    clear(gAdm);
    if (!iso) return;
    var go = function (gj) {
      if (S.iso !== iso) return;
      gj.features.forEach(function (f) {
        var code = f.properties.id, n = (S.admN || {})[code] || 0;
        var p = svg('path', { d: geomD(f.geometry), class: 'has' + (S.adm1 === code ? ' sel' : ''), style: 'fill:var(--seq-' + (n ? 3 : 1) + ')' });
        var t = svg('title'); t.textContent = f.properties.name + (n ? ' — ' + n + ' located articles' : ''); p.appendChild(t);
        p.addEventListener('click', function (ev) { if (S.draw) return; ev.stopPropagation(); $('sel-adm1').value = code; S.adm1 = code; S.adhoc = null; load(); });
        gAdm.appendChild(p);
      });
    };
    if (S.geoA[iso]) go(S.geoA[iso]); else api('/api/news/boundaries?level=adm1&iso2=' + encodeURIComponent(iso)).then(function (gj) { S.geoA[iso] = gj; go(gj); }).catch(function () { });
  }
  function zoomCountry(iso) {
    var f = S.geoC.features.filter(function (x) { return x.properties.id === iso; })[0];
    if (f) setView(geomBox(f.geometry));
  }
  function showShape(aoi) {
    clear(gShape);
    if (aoi && aoi.geometry) { gShape.appendChild(svg('path', { class: 'aoi-shape', d: geomD(aoi.geometry) })); setView(geomBox(aoi.geometry)); }
  }

  // ---------- drawing a custom area ----------
  function svgPoint(ev) {
    var pt = mapSvg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    var m = mapSvg.getScreenCTM(); if (!m) return null;
    var q = pt.matrixTransform(m.inverse()); return inv(q.x, q.y);
  }
  function startDraw(type) {
    S.draw = { type: type, pts: [] };
    ['d-poly', 'd-box', 'd-rad'].forEach(function (id) { $(id).setAttribute('aria-pressed', 'false'); });
    $({ polygon: 'd-poly', box: 'd-box', radius: 'd-rad' }[type]).setAttribute('aria-pressed', 'true');
    $('d-cancel').disabled = false; $('d-finish').disabled = true;
    $('map-hint').textContent = { polygon: 'Click vertices on the map, then “Finish polygon”.', box: 'Click two opposite corners.', radius: 'Click the centre point.' }[type];
    redrawDraft();
  }
  function endDraw() {
    S.draw = null; clear(gDraft);
    ['d-poly', 'd-box', 'd-rad'].forEach(function (id) { $(id).setAttribute('aria-pressed', 'false'); });
    $('d-cancel').disabled = true; $('d-finish').disabled = true;
    var hnt = $('map-hint'); if (hnt) hnt.textContent = 'Click a country to select it.';
  }
  function redrawDraft() {
    clear(gDraft);
    if (!S.draw) return;
    var pp = S.draw.pts.map(function (p) { return P(p[0], p[1]); });
    pp.forEach(function (p) { gDraft.appendChild(svg('circle', { class: 'vtx', cx: p[0], cy: p[1], r: S.view[2] / 220 })); });
    if (pp.length > 1) gDraft.appendChild(svg('polyline', { class: 'draft', points: pp.map(function (p) { return p.join(','); }).join(' ') }));
  }
  function onMapClick(ev) {
    if (!S.draw) return;
    var p = svgPoint(ev); if (!p) return;
    S.draw.pts.push(p); redrawDraft();
    var d = S.draw;
    if (d.type === 'polygon') $('d-finish').disabled = d.pts.length < 3;
    else if (d.type === 'box' && d.pts.length === 2) {
      var a = d.pts[0], b = d.pts[1];
      finishSpec({ kind: 'bbox', bbox: [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])], label: 'custom box' });
    } else if (d.type === 'radius' && d.pts.length === 1) {
      var km = Math.max(1, Math.min(2000, parseFloat($('d-km').value) || 100));
      finishSpec({ kind: 'radius', lon: d.pts[0][0], lat: d.pts[0][1], radius_km: km, label: 'custom ' + km + ' km radius' });
    }
  }
  function finishSpec(spec) {
    spec.min_precision = $('d-country').checked ? 'country' : 'city';
    endDraw(); S.mode = 'event'; setMode(); S.adhoc = spec; S.iso = ''; S.adm1 = '';
    $('sel-country').value = ''; fillAdm1(''); load();
  }

  // ---------- AOI selection ----------
  function setMode() {
    $('m-event').setAttribute('aria-pressed', S.mode === 'event'); $('m-mention').setAttribute('aria-pressed', S.mode === 'mentioned');
    $('sel-adm1').disabled = S.mode === 'mentioned' || !S.iso;
    ['d-poly', 'd-box', 'd-rad'].forEach(function (id) { $(id).disabled = S.mode === 'mentioned'; });
  }
  function pickCountry(iso) { $('sel-country').value = iso; S.iso = iso; S.adm1 = ''; S.adhoc = null; fillAdm1(iso); load(); }
  function fillAdm1(iso) {
    var sel = clear($('sel-adm1')); sel.appendChild(h('option', { value: '' }, 'All of country'));
    sel.disabled = !iso || S.mode === 'mentioned';
    S.admN = {};
    if (!iso) return;
    api('/api/news/admin1?iso2=' + encodeURIComponent(iso)).then(function (r) {
      if (S.iso !== iso) return;
      r.admin1.forEach(function (a) { S.admN[a.code] = a.n_articles; });
      r.admin1.sort(function (a, b) { return b.n_articles - a.n_articles || a.name.localeCompare(b.name); });
      r.admin1.forEach(function (a) { sel.appendChild(h('option', { value: a.code }, a.name + (a.n_articles ? ' (' + a.n_articles + ')' : ''))); });
      sel.value = S.adm1 || '';
      drawAdm(iso);
    }).catch(function () { });
  }
  function aoiRef() {   // the area currently selected: a registered id or an ad-hoc spec
    if (S.adhoc) return { aoi: S.adhoc };
    if (!S.iso) return null;
    var id = S.adm1 && S.mode === 'event' ? 'adm1:' + S.adm1 + ':event' : 'country:' + S.iso + ':' + S.mode;
    if (S.regIds[id]) return { aoi_id: id };
    return { aoi: S.adm1 && S.mode === 'event' ? { kind: 'adm1', key: S.adm1 } : { kind: 'country', key: S.iso, mode: S.mode } };
  }
  function currentRequest() {
    var ref = aoiRef(); if (!ref) return null;
    var tq = S.tax ? '&tax=' + encodeURIComponent(S.tax) : '';
    if (ref.aoi_id) return { get: '/api/news/aoi?id=' + encodeURIComponent(ref.aoi_id) + '&level=' + S.level + tq };
    return { post: { aoi: ref.aoi, level: S.level, tax: S.tax || null } };
  }
  function fetchArticles(extra) {   // same area + topic filter as the view, plus optional node / day / paging
    var b = aoiRef(); if (!b) return Promise.reject(new Error('no area selected'));
    var body = { aoi_id: b.aoi_id, aoi: b.aoi, tax: S.tax || null, limit: 50 };
    for (var k in extra) body[k] = extra[k];
    return api('/api/news/articles', body);
  }
  function load() {
    var rq = currentRequest(), my = ++S.req;
    setMode(); markSelected();
    if (!rq) { return; }
    $('aoi-status').textContent = 'computing…';
    ['cov', 'kpis', 'events', 'nodes', 'alerts', 'node-chart', 'articles'].forEach(function (id) { clear($(id)).appendChild(h('div', { class: 'n-spin' }, 'loading…')); });
    (rq.get ? api(rq.get) : api('/api/news/adhoc', rq.post)).then(function (b) {
      if (my !== S.req) return;
      S.bundle = b; S.selNode = null;
      $('aoi-status').textContent = (b.aoi.registered ? 'Pre-computed area: ' : 'Computed on demand: ') + b.aoi.name + (b.aoi.adhoc ? ' (ad-hoc)' : '');
      if (S.adhoc) showShape(b.aoi); else { clear(gShape); if (S.adm1 && S.geoA[S.iso]) { var f = S.geoA[S.iso].features.filter(function (x) { return x.properties.id === S.adm1; })[0]; if (f) setView(geomBox(f.geometry)); } else if (S.iso) zoomCountry(S.iso); }
      if (S.iso) drawAdm(S.iso);
      renderAll();
    }).catch(function (e) {
      if (my !== S.req) return;
      $('aoi-status').textContent = '';
      ['cov', 'kpis', 'events', 'nodes', 'alerts', 'articles'].forEach(function (id) { errBox($(id), e.message); });
    });
  }

  // ---------- render ----------
  function renderAll() { renderTaxbar(); renderCoverage(); renderKpis(); renderEvents(); renderNodes(); renderAlerts(); renderArticles(); }
  function sum(a, k) { return a.reduce(function (s, r) { return s + (isNum(r[k]) ? r[k] : 0); }, 0); }
  function renderCoverage() {
    var b = S.bundle, host = clear($('cov')), cv = b.coverage || [];
    var total = sum(cv, 'n_scope_total'), unl = sum(cv, 'n_scope_unlocated'), inA = sum(cv, 'n_articles'), men = sum(cv, 'n_mention_only');
    if (!cv.length || !total) { host.appendChild(h('div', { class: 'n-note warn' }, 'No articles in this area in the processed period.' + (b.support && b.support.reason ? ' ' + b.support.reason : ''))); return; }
    var locOther = Math.max(0, total - unl - inA);
    host.appendChild(h('div', { class: 'n-cov' }, [
      h('div', { class: 'n-bar', role: 'img', 'aria-label': 'Share of processed articles' }, [
        h('i', { class: 'c-loc', style: 'width:' + (100 * inA / total) + '%', title: 'in this area' }),
        h('i', { class: 'c-mention', style: 'width:' + (100 * Math.min(men, total - inA - unl) / total) + '%', title: 'mention it, located elsewhere or unlocated' }),
        h('i', { class: 'c-unloc', style: 'width:' + (100 * unl / total) + '%', title: 'no event location' })]),
      h('div', { class: 'n-key' }, [
        h('span', null, [h('u', { class: 'c-loc' }), inA.toLocaleString() + ' articles in this area (' + (100 * inA / total).toFixed(1) + '% of ' + total.toLocaleString() + ' processed)']),
        h('span', null, [h('u', { class: 'c-mention' }), men.toLocaleString() + ' mention it but are not matched to it']),
        h('span', null, [h('u', { class: 'c-unloc' }), unl.toLocaleString() + ' (' + (100 * unl / total).toFixed(1) + '%) have NO event location — they cannot appear in any event-location area'])]),
      h('div', { class: 'n-note' }, 'Event location = GDELT ActionGeo, available for roughly 35–40% of articles corpus-wide. Unlocated articles are shown here, never hidden or guessed. ' + locOther.toLocaleString() + ' processed articles are located in other places.')]));
    var sp = b.support || {};
    if (!sp.supported) host.appendChild(h('div', { class: 'n-note warn' }, 'Topic baselines unavailable for this area: ' + (sp.reason || 'insufficient labelled articles') + '. Counts are still shown; no trend or alert is computed on thin evidence.'));
    else if (sp.gap_days) host.appendChild(h('div', { class: 'n-note' }, sp.gap_days + ' day(s) had too few articles in this area to test (treated as data gaps, not zero).'));
    (b.notes || []).forEach(function (n) { host.appendChild(h('div', { class: 'n-note warn' }, n)); });
    var tx = b.tax;
    if (tx) host.appendChild(h('div', { class: 'n-note' }, [h('b', null, 'Topic filter: '), tx.n_match.toLocaleString() + ' of ' + tx.n_area.toLocaleString() + ' articles in this area carry the stored label “' + tx.name + '” (' + (tx.n_area ? (100 * tx.n_match / tx.n_area).toFixed(1) : '0') + '%). Bars below: grey = all articles in the area, gold = matching the filter.']));
    host.appendChild(dayBars(cv, tx));
  }
  function dayBars(cv, tx) {
    var tmap = {}; if (tx) (tx.days || []).forEach(function (r) { tmap[day(r.day)] = r.n; });
    var w = 900, hh = 90, L = 30, n = cv.length, mx = Math.max.apply(null, cv.map(function (r) { return r.n_articles || 0; }).concat([1])), bw = (w - L - 6) / n;
    var s = svg('svg', { class: 'n-chart', viewBox: '0 0 ' + w + ' ' + hh, role: 'img', 'aria-label': 'Articles per day in this area' });
    cv.forEach(function (r, i) {
      var v = r.n_articles || 0, bh = v / mx * (hh - 22), rect = svg('rect', { class: 'bar' + (tx ? ' ctx' : ''), x: L + i * bw + 1, y: hh - 16 - bh, width: Math.max(1, bw - 2), height: bh });
      if (tx) { var mv = tmap[day(r.day)] || 0, mh = mv / mx * (hh - 22); s.appendChild(svg('rect', { class: 'bar sel', x: L + i * bw + 1, y: hh - 16 - mh, width: Math.max(1, bw - 2), height: mh })); }
      var t = svg('title'); t.textContent = day(r.day) + ': ' + v + ' articles in this area; ' + (r.n_scope_unlocated || 0) + ' of ' + (r.n_scope_total || 0) + ' processed have no event location'; rect.appendChild(t); s.appendChild(rect);
      if (i % Math.ceil(n / 7) === 0) { var tx = svg('text', { x: L + i * bw, y: hh - 3 }); tx.textContent = day(r.day).slice(5); s.appendChild(tx); }
    });
    var t0 = svg('text', { x: 2, y: 12 }); t0.textContent = mx; s.appendChild(t0);
    return s;
  }
  function kpi(label, val, sub) { return h('div', { class: 'n-kpi' }, [h('span', null, label), h('b', null, val), sub ? h('small', null, sub) : null]); }
  function renderKpis() {
    var b = S.bundle, host = clear($('kpis')), cv = b.coverage || [], ev = b.events || [], ac = b.alert_counts || {};
    var pubs = null; // distinct publishers is not additive across days: show max-day value only
    if (b.tax) host.appendChild(kpi('Matching articles', b.tax.n_match.toLocaleString(), 'of ' + b.tax.n_area.toLocaleString() + ' in this area · “' + b.tax.name + '”'));
    else host.appendChild(kpi('Articles in area', sum(cv, 'n_articles').toLocaleString(), 'de-duplicated, whole period'));
    host.appendChild(kpi('Events', b.aoi.mode === 'event' ? String(ev.length) : '—', b.aoi.mode === 'event' ? '≥3 articles, ≥2 publishers' : 'not defined for “mentioned in”'));
    host.appendChild(kpi('Emerging events', b.aoi.mode === 'event' ? String(ac.emerging_event || 0) : '—', 'FDR q<0.05, high-confidence location'));
    host.appendChild(kpi('New developments', b.aoi.mode === 'event' ? String(ac.new_development || 0) : '—', 'inside existing events'));
    host.appendChild(kpi('Trending-topic alerts', b.support && b.support.supported ? String(ac.node_trend || 0) : '—', b.support && b.support.supported ? 'vs this area’s own baseline' : 'insufficient evidence'));
    host.appendChild(kpi('Days with data', String(b.tax ? (b.tax.days || []).length : cv.filter(function (r) { return r.n_articles > 0; }).length), 'of ' + cv.length + ' processed' + (b.tax ? ' (days with matching articles)' : '')));
  }

  // ----- events -----
  var EVCOLS = [['rep_title', 'Event', 0, 1], ['first_seen', 'First seen', 1, 0], ['n_articles', 'Articles', 1, 1], ['n_publishers', 'Publishers', 1, 1],
    ['growth_24h', 'Growth 24h', 1, 1], ['accel_6h', 'Accel.', 1, 1], ['duration_hours', 'Lasted (h)', 1, 1], ['loc_confidence', 'Event location', 0, 1],
    ['emerging_q', 'Emerging q', 1, 1], ['topic_name', 'Topic', 0, 1]];
  var CONF_RANK = { high: 3, medium: 2, low: 1 };
  function eventFilters() {
    var f = clear($('ev-filters'));
    f.appendChild(h('input', { type: 'search', placeholder: 'Search headline…', 'aria-label': 'Search headline', value: S.evF.q, oninput: function (e) { S.evF.q = e.target.value; renderEventRows(); } }));
    f.appendChild(h('label', null, ['Min publishers ', h('input', { type: 'number', min: 0, value: S.evF.minPub, onchange: function (e) { S.evF.minPub = +e.target.value || 0; renderEventRows(); } })]));
    f.appendChild(h('label', null, [h('input', { type: 'checkbox', checked: S.evF.emergingOnly, onchange: function (e) { S.evF.emergingOnly = e.target.checked; renderEventRows(); } }), ' emerging only']));
    f.appendChild(h('label', null, ['Location confidence ', h('select', { onchange: function (e) { S.evF.conf = e.target.value; renderEventRows(); } },
      [['any', 'any'], ['medium', 'medium or high'], ['high', 'high only']].map(function (o) { return h('option', { value: o[0], selected: S.evF.conf === o[0] }, o[1]); }))]));
  }
  function renderEvents() {
    var host = clear($('events')), b = S.bundle;
    if (b.aoi.mode !== 'event') { host.appendChild(h('div', { class: 'n-note' }, 'Events are defined for event-location areas only. Switch “Location meaning” to Event location.')); clear($('ev-filters')); return; }
    if (!(b.events || []).length) { clear($('ev-filters')); host.appendChild(h('div', { class: 'n-note warn' }, 'No multi-publisher events with a usable event location in this area for the processed period.')); return; }
    eventFilters();
    host.appendChild(h('div', { class: 'n-tbl-wrap' }, h('table', { class: 'n-tbl', id: 'ev-tbl' }, [h('thead'), h('tbody')])));
    var tr = h('tr');
    EVCOLS.forEach(function (c) { tr.appendChild(h('th', { 'data-k': c[0], class: c[2] ? 'num' : '', title: 'Sort', onclick: function () { S.evSort = { k: c[0], dir: S.evSort.k === c[0] ? -S.evSort.dir : (c[3] ? -1 : 1) }; renderEventRows(); } }, c[1] + (S.evSort.k === c[0] ? (S.evSort.dir < 0 ? ' ↓' : ' ↑') : ''))); });
    $('ev-tbl').querySelector('thead').appendChild(tr);
    renderEventRows();
  }
  function renderEventRows() {
    var tbl = $('ev-tbl'); if (!tbl) return;
    var tb = clear(tbl.querySelector('tbody')), k = S.evSort.k, d = S.evSort.dir, f = S.evF;
    var rows = (S.bundle.events || []).filter(function (e) {
      return (!f.q || (e.rep_title || '').toLowerCase().indexOf(f.q.toLowerCase()) >= 0) && e.n_publishers >= f.minPub && (!f.emergingOnly || e.emerging_alert) &&
        (f.conf === 'any' || (CONF_RANK[e.loc_confidence] || 0) >= CONF_RANK[f.conf]);
    });
    rows.sort(function (a, b) { var x = a[k], y = b[k]; if (k === 'loc_confidence') { x = CONF_RANK[x] || 0; y = CONF_RANK[y] || 0; } if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1; return (x < y ? -1 : x > y ? 1 : 0) * d; });
    rows.slice(0, 200).forEach(function (e) {
      var loc = [e.loc_country_iso ? cname(e.loc_country_iso) : 'unlocated', badge(e.loc_confidence || 'none', e.loc_confidence === 'high' ? 'b-hi' : e.loc_confidence === 'medium' ? 'b-med' : 'b-low',
        'event-location confidence' + (e.loc_share != null ? '; ' + Math.round(100 * e.loc_share) + '% of ' + e.n_located + ' located articles agree' : ''))];
      tb.appendChild(h('tr', { class: 'clk', tabindex: 0, onclick: function () { openEvent(e.event_id); }, onkeydown: function (ev) { if (ev.key === 'Enter') openEvent(e.event_id); } }, [
        h('td', { class: 'ttl' }, [e.emerging_alert ? badge('emerging', 'b-em', 'FDR-controlled emerging-event alert') : null, e.n_developments > 0 ? badge('dev ×' + e.n_developments, 'b-dev', 'new development(s) detected inside this event') : null, e.rep_title || '(no title)',
          isNum(e.tax_share) && S.bundle.tax ? h('div', { class: 'n-muted n-sm' }, Math.round(100 * e.tax_share) + '% of this event’s labelled articles are “' + S.bundle.tax.name + '”') : null,
          (e.sources && e.sources.length) ? h('details', { class: 'n-cite', onclick: function (ev) { ev.stopPropagation(); }, onkeydown: function (ev) { ev.stopPropagation(); } }, [h('summary', null, 'Sources (' + e.sources.length + ' of ' + e.n_articles + ' articles)'), e.sources.map(citeNode)]) : null]),
        h('td', null, when(e.first_seen)), h('td', { class: 'num' }, String(e.n_articles)), h('td', { class: 'num' }, String(e.n_publishers)),
        h('td', { class: 'num' }, num(e.growth_24h, 2)), h('td', { class: 'num' }, num(e.accel_6h, 2)), h('td', { class: 'num' }, num(e.duration_hours, 0)),
<<<<<<< HEAD
        h('td', null, loc), h('td', { class: 'num', title: e.emerging_note || '' }, qfmt(e.emerging_q)), h('td', { title: taxPath(e) }, e.topic_name || (TAXST[e.tax_status] || ''))]));
=======
        h('td', null, loc), h('td', { class: 'num', title: e.emerging_note || '' }, qfmt(e.emerging_q)), h('td', null, e.topic_name || '')]));
>>>>>>> origin/main
    });
    if (rows.length > 200) tb.appendChild(h('tr', null, h('td', { colspan: EVCOLS.length, class: 'n-muted' }, 'Showing 200 of ' + rows.length + '. Narrow the filters.')));
    if (!rows.length) tb.appendChild(h('tr', null, h('td', { colspan: EVCOLS.length, class: 'n-muted' }, 'No events match the filters.')));
  }

  // ----- nodes -----
  function renderNodes() {
    var b = S.bundle, host = clear($('nodes')); clear($('node-chart'));
    if (!(b.nodes || []).length) { host.appendChild(h('div', { class: 'n-note warn' }, 'No topic series: ' + ((b.support && b.support.reason) || 'insufficient evidence') + '.')); return; }
    var tb = h('tbody');
    b.nodes.forEach(function (n) {
      var ser = (b.series[n.node_code] || []);
      tb.appendChild(h('tr', { class: 'clk', tabindex: 0, onclick: function () { pickNode(n); }, onkeydown: function (e) { if (e.key === 'Enter') pickNode(n); } }, [
        h('td', null, [n.alert_flag ? badge('alert', 'b-em', 'FDR-controlled alert on the latest day') : (n.any_alert ? badge('alert ' + n.n_alert_days + 'd', 'b-dev', 'alert on an earlier day') : null), n.name]),
        h('td', { class: 'num' }, num(n.V)), h('td', { class: 'num' }, n.support_ok ? num(n.baseline_mu, 1) : na()), h('td', { class: 'num' }, qfmt(n.surprise_q)),
        h('td', { class: 'num' }, num(n.growth, 2)), h('td', { class: 'num' }, num(n.accel_smooth, 2)),
        h('td', { class: 'num' }, isNum(n.publisher_pie) ? n.publisher_pie.toFixed(2) : na()),
        h('td', null, spark(ser.map(function (r) { return r[1]; }), ser.map(function (r) { return r[2]; })))]));
    });
    host.appendChild(h('div', { class: 'n-tbl-wrap' }, h('table', { class: 'n-tbl' }, [
      h('thead', null, h('tr', null, [['Topic', 0, ''], ['Articles (latest day)', 1, ''], ['Baseline', 1, 'This area\'s own expected count'], ['FDR q', 1, ''], ['Growth', 1, 'log-rate, day over day'],
        ['Accel.', 1, 'smoothed, causal'], ['Publisher diversity', 1, 'Hurlbert PIE; — when <2 articles'], ['Daily trend', 0, '']].map(function (c) { return h('th', { class: c[1] ? 'num' : '', title: c[2] }, c[0]); }))), tb])));
  }

  function pickNode(n) {
    S.selNode = n.node_code; nodeChart($('node-chart'), n.node_code);
    var host = $('node-chart'), box = host.appendChild(h('div', { id: 'node-src', class: 'n-cites', style: 'margin-top:10px' }, h('span', { class: 'n-muted' }, 'loading source articles…')));
    fetchArticles({ node: { level: S.level, code: n.node_code } }).then(function (r) {
      clear(box);
      box.appendChild(h('div', null, [h('b', null, 'Source articles for “' + n.name + '”'), ' ', h('span', { class: 'n-muted' }, '(' + r.total.toLocaleString() + ' in this view, newest first)')]));
      r.items.slice(0, 6).forEach(function (c) { box.appendChild(citeNode(c)); });
      if (r.total > 6) box.appendChild(h('button', { style: 'margin-top:6px', onclick: function () { openArticles('Source articles — ' + n.name, { node: { level: S.level, code: n.node_code } }); } }, 'Show all ' + r.total.toLocaleString()));
    }).catch(function (e) { clear(box).appendChild(h('span', { class: 'n-err' }, e.message)); });
  }

  // ----- alerts -----
  function renderAlerts() {
    var b = S.bundle, host = clear($('alerts')), fl = clear($('al-filters'));
    fl.appendChild(h('label', null, ['Kind ', h('select', { onchange: function (e) { S.alF.kind = e.target.value; renderAlerts(); } },
      [['', 'all'], ['emerging_event', 'emerging events'], ['new_development', 'new developments'], ['node_trend', 'topic trends']].map(function (o) { return h('option', { value: o[0], selected: S.alF.kind === o[0] }, o[1]); }))]));
    fl.appendChild(h('label', null, ['Status ', h('select', { onchange: function (e) { S.alF.status = e.target.value; renderAlerts(); } },
      [['active', 'active'], ['retracted', 'retracted (revised by late data)'], ['all', 'all']].map(function (o) { return h('option', { value: o[0], selected: S.alF.status === o[0] }, o[1]); }))]));
    var rows = (b.alerts || []).filter(function (a) { return (!S.alF.kind || a.kind === S.alF.kind) && (S.alF.status === 'all' || a.status === S.alF.status); });
    if (!rows.length) { host.appendChild(h('div', { class: 'n-muted' }, (b.alerts || []).length ? 'No alerts match the filters.' : 'No alerts for this area.')); return; }
    host = host.appendChild(h('div', { class: 'n-scroll', tabindex: 0, role: 'region', 'aria-label': 'Alert list (scrollable)' }));
    var kinds = { emerging_event: 'EMERGING EVENT', new_development: 'NEW DEVELOPMENT', node_trend: 'TOPIC TREND' };
    rows.slice(0, 120).forEach(function (a) {
      host.appendChild(h('div', { class: 'n-alert' + (a.status === 'retracted' ? ' n-retracted' : '') }, [
        h('div', null, [h('b', { class: 'sev-' + a.severity }, a.severity ? a.severity.toUpperCase() : ''), h('div', { class: 'n-muted' }, kinds[a.kind] || a.kind)]),
        h('div', null, [h('div', null, [h('b', null, a.entity_name || ''), ' ', h('span', { class: 'n-muted' }, when(a.as_of)), a.kind !== 'node_trend' && a.entity_type === 'event' ? [' ', h('button', { onclick: function () { openEvent(a.entity_id); } }, 'open event')] : null]),
          h('div', { class: 'why' }, a.rationale || ''), h('div', { class: 'n-muted' }, 'q=' + (isNum(a.q) ? a.q.toFixed(4) : '—') + ' · effect ' + (isNum(a.effect) ? a.effect.toFixed(2) : '—') + (a.status === 'retracted' ? ' · RETRACTED' : '')),
          citeBlock(a.sources, 'Sources: ', (a.kind === 'node_trend' && a.n_sources > (a.sources || []).length) ? h('button', { class: 'n-sm', style: 'margin-left:6px', onclick: function () { openArticles('Sources — ' + (a.entity_name || a.entity_id) + ' · ' + day(a.day), { node: { level: String(a.entity_type).replace('node_', ''), code: a.entity_id }, day: day(a.day) }); } }, 'all ' + a.n_sources) : null)])]));
    });
    if (rows.length > 120) host.appendChild(h('div', { class: 'n-muted' }, 'Showing 120 of ' + rows.length + '. Use the filters to narrow.'));
  }

  // ----- event drawer -----
  function openEvent(id) {
    var body = clear($('dr-body')); $('dr-title').textContent = 'Loading…';
    document.body.classList.add('drawer'); $('n-drawer').setAttribute('aria-hidden', 'false');
    body.appendChild(h('div', { class: 'n-spin' }, 'loading…'));
    api('/api/news/event?id=' + encodeURIComponent(id)).then(function (d) { renderEvent(d); }).catch(function (e) { errBox(clear($('dr-body')), e.message); });
  }
  function closeEvent() { document.body.classList.remove('drawer'); $('n-drawer').setAttribute('aria-hidden', 'true'); }
  function kv(label, v) { return h('div', { class: 'n-kpi' }, [h('span', null, label), h('b', { style: 'font-size:16px' }, v)]); }
<<<<<<< HEAD
  // taxonomy the event's articles AGREE on (strict majority); otherwise an honest status instead of a guessed label
  var TAXST = { mixed: 'Mixed topics', outside: 'Outside the taxonomy', unlabelled: 'Unlabelled' };
  function taxPath(e) { var p = [e.domain_name, e.topic_name, e.category_name, e.subtopic_name].filter(Boolean); return p.length ? p.join(' › ') : (TAXST[e.tax_status] || 'Unlabelled'); }
  function renderEvent(d) {
    var e = d.event, m = d.metrics || {}, body = clear($('dr-body'));
    $('dr-title').textContent = e.rep_title || e.event_id;
    body.appendChild(h('div', { class: 'n-2', style: 'margin:6px 0' }, [taxPath(e), ' · first seen ' + when(e.first_seen) + ' · last ' + when(e.last_seen)]));
=======
  function renderEvent(d) {
    var e = d.event, m = d.metrics || {}, body = clear($('dr-body'));
    $('dr-title').textContent = e.rep_title || e.event_id;
    body.appendChild(h('div', { class: 'n-2', style: 'margin:6px 0' }, [(e.domain_name || '') + ' › ' + (e.topic_name || ''), ' · first seen ' + when(e.first_seen) + ' · last ' + when(e.last_seen)]));
>>>>>>> origin/main
    body.appendChild(h('div', { class: 'n-grid2' }, [kv('Articles', String(e.n_articles)), kv('Publishers', String(e.n_publishers)), kv('Syndicated copies', String(d.n_copies_exact)),
      kv('Growth 24h', num(m.growth_24h, 2)), kv('Acceleration', num(m.accel_6h, 2)), kv('Duration (h)', num(m.duration_hours, 0)), kv('Half-life (d)', num(m.half_life_days, 1)),
      kv('Publisher diversity', isNum(m.publisher_pie) ? m.publisher_pie.toFixed(2) : na()), kv('Cross-source ratio', num(m.cross_source_ratio, 2))]));
    if (m.support_note) body.appendChild(h('div', { class: 'n-note' }, 'Not computed (minimum support): ' + m.support_note));
    // location (separate from publisher location)
    var lc = d.location_counts || {}, parts = Object.keys(lc).map(function (k) { return (k === 'unlocated' ? 'unlocated' : cname(k)) + ' ' + lc[k]; });
    body.appendChild(h('div', { class: 'n-note' + (e.loc_confidence === 'high' ? '' : ' warn') }, [h('b', null, 'Event location: '), (e.loc_country_iso ? cname(e.loc_country_iso) : 'unlocated'), ' · confidence ', badge(e.loc_confidence || 'none', e.loc_confidence === 'high' ? 'b-hi' : 'b-med'),
      ' · located articles by country: ' + parts.join(', '), h('br'), d.location_note]));
    if (d.timeline_3h && d.timeline_3h.length) body.appendChild(timeline(d.timeline_3h));
    // tests + developments
    (d.tests || []).forEach(function (t) { body.appendChild(h('div', { class: 'n-note' }, [h('b', null, 'Emerging test (' + t.aoi_id + '): '), t.support_ok ? ('p=' + t.p.toFixed(4) + ', q=' + t.q.toFixed(4) + ', ' + t.n_publishers_at_age + ' publishers at age ' + Math.round(t.age_hours) + 'h vs ' + t.n_ref + ' reference clusters (' + t.stratum + ')' + (t.alert_flag ? ' — ALERT' : '')) : ('not computed: ' + t.support_note)])); });
    (d.developments || []).forEach(function (v) { body.appendChild(h('div', { class: 'n-note' }, [h('b', null, 'Development ' + when(v.detected_asof) + ': '), v.n_new + ' new articles / ' + v.n_new_publishers + ' publishers' + (v.alert_flag ? ' — ALERT' : ' — below threshold') + ' · ' + (v.rep_title || '')])); });
    // articles
    var tb = h('tbody');
    d.articles.forEach(function (a) {
      var pc = a.publisher_country ? a.publisher_country + (a.publisher_country_status === 'verified' ? '' : '?') : '?';
      tb.appendChild(h('tr', { class: a.is_duplicate ? 'n-muted' : '' }, [
        h('td', { class: 'ttl' }, [a.is_duplicate ? badge('copy', 'b-dev', 'identical text of an earlier article; not counted') : null, a.url ? h('a', { href: a.url }, a.title || '(no title)') : (a.title || '(no title)')]),
        h('td', null, a.source_domain || ''), h('td', { title: a.publisher_country ? 'Publisher country from the registry (' + a.publisher_country_status + ') — NOT the event location' : 'publisher country unknown' }, pc),
        h('td', { title: 'published ' + when(a.published_at) }, when(a.published_at)), h('td', { title: a.ev_precision ? 'precision: ' + a.ev_precision : '' }, a.loc_status === 'located' ? ((a.ev_place || cname(a.ev_country_iso)) + (a.geo_inconsistent ? ' ⚠' : '')) : 'unlocated')]));
    });
    body.appendChild(h('details', { class: 'n-det', open: true }, [h('summary', null, 'Supporting articles (' + d.articles.length + ')'),
      h('div', { class: 'n-muted', style: 'font-size:11.5px;margin:2px 0 6px' }, 'Pub. country = where the outlet is based (registry; “?” = not yet human-verified, “?” alone = unknown). Event location = where the event happened, per GDELT. They are different things.'),
      h('div', { class: 'n-tbl-wrap' }, h('table', { class: 'n-tbl' }, [h('thead', null, h('tr', null, ['Headline', 'Publisher', 'Pub. country', 'Published', 'Event location (per article)'].map(function (c) { return h('th', null, c); }))), tb]))]));
  }
  function timeline(rows) {
    var w = 700, hh = 80, L = 22, n = rows.length, mx = Math.max.apply(null, rows.map(function (r) { return r[1]; }).concat([1])), bw = (w - L) / n;
    var s = svg('svg', { class: 'n-chart', viewBox: '0 0 ' + w + ' ' + hh, role: 'img', 'aria-label': 'Articles per 3 hours' });
    rows.forEach(function (r, i) { var bh = r[1] / mx * (hh - 20), rc = svg('rect', { class: 'bar', x: L + i * bw + 0.5, y: hh - 14 - bh, width: Math.max(1, bw - 1), height: bh }); var t = svg('title'); t.textContent = when(r[0]) + ': ' + r[1]; rc.appendChild(t); s.appendChild(rc); });
    var a = svg('text', { x: 2, y: 10 }); a.textContent = mx; s.appendChild(a);
    var b = svg('text', { x: L, y: hh - 2 }); b.textContent = when(rows[0][0]); s.appendChild(b);
    var c = svg('text', { x: w - 110, y: hh - 2 }); c.textContent = when(rows[n - 1][0]); s.appendChild(c);
    return h('div', null, [h('div', { class: 'n-2' }, 'Articles per 3 hours'), s]);
  }

  // ----- articles (title, publisher, publication date, link to the original) -----
  function artRow(c) {
    var meta = [h('span', null, [h('b', null, c.publisher || 'unknown publisher')]), h('span', null, when(c.published_at))];
    var tx = c.tax_subtopic || c.tax_topic; if (tx) meta.push(h('span', { title: [c.tax_domain, c.tax_topic, c.tax_subtopic].filter(Boolean).join(' › ') }, '▸ ' + tx));
    if (c.loc_status === 'located' && (c.place || c.country)) meta.push(h('span', { title: 'event location (GDELT), not publisher location' }, '📍 ' + (c.place || cname(c.country))));
    if (c.event_id && S.bundle && S.bundle.aoi.mode === 'event') meta.push(h('a', { href: '#', onclick: function (e) { e.preventDefault(); openEvent(c.event_id); } }, 'event ›'));
    return h('div', { class: 'n-art' }, [
      h('div', { class: 't' }, c.url ? [h('a', { href: c.url, title: 'Open the original article: ' + c.url }, c.title || '(no title)'), ' ↗'] : [c.title || '(no title)', ' ', badge('no link', 'b-low')]),
      h('div', { class: 'm' }, meta)]);
  }
  function pagedList(host, first, total, loader) {
    var list = h('div', null, first.map(artRow)), btn = null, shown = first.length;
    host.appendChild(list);
    function more() {
      if (shown >= total) return;
      btn = btn || host.appendChild(h('button', { style: 'margin-top:8px', onclick: function () { btn.disabled = true; loader(shown).then(function (r) { r.items.forEach(function (c) { list.appendChild(artRow(c)); }); shown += r.items.length; btn.disabled = false; sync(); }).catch(function (e) { btn.disabled = false; btn.textContent = 'Retry (' + e.message + ')'; }); } }, ''));
      sync();
    }
    function sync() { if (!btn) return; if (shown >= total) { btn.remove(); } else btn.textContent = 'Show more (' + (total - shown).toLocaleString() + ' remaining)'; }
    more();
  }
  function renderArticles() {
    var b = S.bundle, host = clear($('articles')), a = b.articles || { total: 0, items: [] };
    if (!a.total) { host.appendChild(h('div', { class: 'n-note warn' }, b.tax ? 'No articles in this area carry the selected topic label.' : 'No articles in this area.')); return; }
    host.appendChild(h('div', { class: 'n-2', style: 'margin-bottom:6px' }, a.total.toLocaleString() + ' article' + (a.total === 1 ? '' : 's') + (b.tax ? ' labelled “' + b.tax.name + '”' : '') + ' in this area · exact duplicates counted once'));
    pagedList(host, a.items, a.total, function (off) { return fetchArticles({ offset: off }); });
  }
  function openArticles(title, extra) {
    var body = clear($('dr-body')); $('dr-title').textContent = title;
    document.body.classList.add('drawer'); $('n-drawer').setAttribute('aria-hidden', 'false');
    body.appendChild(h('div', { class: 'n-spin' }, 'loading…'));
    fetchArticles(extra).then(function (r) {
      clear(body); body.appendChild(h('div', { class: 'n-2', style: 'margin:6px 0' }, r.total.toLocaleString() + ' source article' + (r.total === 1 ? '' : 's') + ', newest first. Links open the original publisher’s page.'));
      pagedList(body, r.items, r.total, function (off) { return fetchArticles(Object.assign({}, extra, { offset: off })); });
    }).catch(function (e) { errBox(clear(body), e.message); });
  }

  // ----- topic filter: Domain > Topic > Subtopic (hierarchy from taxonomy_node) -----
  function buildTaxIndex() {
    var by = {}, kids = {};
    (S.meta.taxonomy || []).forEach(function (t) { if (t.in_scope) { by[t.code] = t; (kids[t.parent || ''] = kids[t.parent || ''] || []).push(t); } });
    Object.keys(kids).forEach(function (k) { kids[k].sort(function (a, b) { return a.code < b.code ? -1 : 1; }); });
    S.txi = { by: by, kids: kids };
  }
  function taxParts(code) {   // 'D01.T02.C01.S03' -> ['D01','D01.T02','D01.T02.C01','D01.T02.C01.S03']
    if (!code) return []; var p = code.split('.'), out = [], c = ''; p.forEach(function (x, i) { c = i ? c + '.' + x : x; out.push(c); }); return out;
  }
  function tname(code) { return S.txi.by[code] ? S.txi.by[code].name : code; }
  function opt(v, label, sel, n) { return h('option', { value: v, selected: sel }, label + (n != null ? ' (' + n.toLocaleString() + ')' : '')); }
  function renderTaxbar() { fillTax(); }
  function fillTax() {
    if (!S.txi) return;
    var cnt = (S.bundle && S.bundle.taxonomy_counts) || null, parts = taxParts(S.tax), dom = parts[0] || '', top = parts[1] || '', sub = (parts[3] || '');
    var n = function (c) { return cnt ? (cnt[c] || 0) : null; };
    var sd = clear($('tx-domain')); sd.appendChild(opt('', 'All domains', !dom));
    (S.txi.kids[''] || []).forEach(function (d) { sd.appendChild(opt(d.code, d.name, d.code === dom, n(d.code))); });
    var st = clear($('tx-topic')); st.appendChild(opt('', dom ? 'All topics in this domain' : 'Choose a domain first', !top)); st.disabled = !dom;
    (S.txi.kids[dom] || []).forEach(function (t) { st.appendChild(opt(t.code, t.name, t.code === top, n(t.code))); });
    var ss = clear($('tx-sub')); ss.appendChild(opt('', top ? 'All subtopics in this topic' : 'Choose a topic first', !sub)); ss.disabled = !top;
    (S.txi.kids[top] || []).forEach(function (cat) {
      var g = h('optgroup', { label: cat.name }); (S.txi.kids[cat.code] || []).forEach(function (x) { g.appendChild(opt(x.code, x.name, x.code === sub, n(x.code))); }); ss.appendChild(g);
    });
    $('tx-all').setAttribute('aria-pressed', S.tax ? 'false' : 'true');
    var cr = clear($('tx-crumb'));
    if (!S.tax) { cr.appendChild(document.createTextNode('Showing: All Topics' + (cnt ? ' — ' + Object.keys(cnt).filter(function (k) { return k.indexOf('.') < 0; }).reduce(function (s, k) { return s + cnt[k]; }, 0).toLocaleString() + ' labelled articles in this area' : ''))); return; }
    cr.appendChild(document.createTextNode('Showing: '));
    cr.appendChild(h('button', { onclick: function () { setTax(''); } }, 'All Topics'));
    taxParts(S.tax).forEach(function (c) { var t = S.txi.by[c]; if (!t || t.tier === 'category') return; cr.appendChild(document.createTextNode(' › ')); cr.appendChild(h('button', { 'aria-pressed': c === S.tax, onclick: function () { setTax(c); } }, t.name)); });
    if (S.bundle && S.bundle.tax) cr.appendChild(h('span', { class: 'n-muted' }, '  · ' + S.bundle.tax.n_match.toLocaleString() + ' matching articles in this area'));
  }
  function setTax(code) {
    S.tax = code || '';
    var t = S.tax && S.txi.by[S.tax];
    if (t && t.tier === 'subtopic' && S.level !== 'subtopic') { S.level = 'subtopic'; syncLevel(); }
    fillTax(); if (S.iso || S.adhoc) load();
  }

  // ----- methodology -----
  function renderMethod() {
    var m = S.meta, host = clear($('method')), fam = {};
    host.appendChild(h('div', { class: 'n-note' }, 'Statistical methods: negative-binomial baselines fitted on each area’s own history with a smoothed exposure offset, no look-ahead; support gates (NULL, never 0); Benjamini–Hochberg FDR per area and day; alerts also need an effect-size floor.'));
    host.appendChild(h('div', { class: 'n-note warn' }, [h('b', null, 'Limits: '), 'event location is machine-extracted (GDELT) and available for ~35–40% of articles; ~20% of events with several located articles have members geocoded to different countries, so only high-confidence locations can raise alerts; clusters are story-level and not human-validated; short histories leave few testable days; ad-hoc areas get no cross-area multiplicity control.']));
    var cc = (m.corpus_coverage || []).filter(function (r) { return r.n_dated_total; });
    if (cc.length) { var a = cc.reduce(function (s, r) { return s + r.n_action_country / r.n_dated_total; }, 0) / cc.length; host.appendChild(h('div', { class: 'n-2' }, 'Corpus-wide event-location coverage over ' + cc.length + ' measured days: ' + (100 * a).toFixed(1) + '% of dated articles. Labels missing for ' + m.pending_labels + ' articles (they still form events; excluded from topic series).')); }
    host.appendChild(h('details', { class: 'n-det' }, [h('summary', null, 'Metric definitions (' + m.metric_meta.length + ')'), h('div', { class: 'n-tbl-wrap' }, h('table', { class: 'n-tbl' }, [
      h('thead', null, h('tr', null, ['Metric', 'Definition', 'Units', 'Caveats / support gate'].map(function (c) { return h('th', null, c); }))),
      h('tbody', null, m.metric_meta.map(function (r) { return h('tr', null, [h('td', null, [h('b', null, r.display_name), h('div', { class: 'n-muted' }, r.metric_key)]), h('td', null, r.formula), h('td', null, r.units), h('td', null, r.caveats + ' [gate: ' + r.support_gate + ']')]); }))]))]));
    host.appendChild(h('details', { class: 'n-det' }, [h('summary', null, 'Social-media metrics deliberately not produced (' + m.unsupported.length + ')'), h('ul', null, m.unsupported.map(function (u) { return h('li', null, [h('b', null, u.metric), ' — ' + u.reason]); }))]));
    var pk = Object.keys(m.params).map(function (k) { return k + '=' + m.params[k]; }).join(' · ');
    host.appendChild(h('div', { class: 'n-muted', style: 'margin-top:8px;font-size:12px' }, 'Parameters: ' + pk));
  }

  // ---------- boot ----------
  function boot() {
    initTheme();
    Promise.all([api('/api/news/meta'), api('/api/news/gazetteer'), api('/api/news/boundaries?level=country')]).then(function (r) {
      S.meta = r[0]; r[1].countries.forEach(function (c) { S.gaz[c.iso2] = c; }); S.geoC = r[2];
      S.meta.aois.forEach(function (a) { S.regIds[a.aoi_id] = a; });
      var lr = S.meta.last_run;
      $('n-sub').textContent = 'database ' + S.meta.database + ' · articles published ' + day(S.meta.articles_from) + ' → ' + day(S.meta.articles_to) + ' · ' + S.meta.n_articles.toLocaleString() + ' articles' + (lr ? ' · last successful run ' + when(lr.finished_at) + ' (' + lr.mode + ')' : '') + ' · read-only' + (S.meta.alert_export && S.meta.alert_export.present ? ' · alert export ' + (S.meta.alert_export.in_sync ? 'in sync (' + S.meta.alert_export.n_alerts + ')' : 'NOT in sync with database') : '');
      var sel = clear($('sel-country')); sel.appendChild(h('option', { value: '' }, 'Choose a country…'));
      var withData = r[1].countries.filter(function (c) { return c.n_articles > 0; }), without = r[1].countries.filter(function (c) { return !c.n_articles; });
      var g1 = h('optgroup', { label: 'Countries with located articles' }), g2 = h('optgroup', { label: 'Other countries (no located articles)' });
      withData.forEach(function (c) { g1.appendChild(h('option', { value: c.iso2 }, c.name + ' (' + c.n_articles.toLocaleString() + (S.regIds['country:' + c.iso2 + ':event'] ? ', pre-computed' : '') + ')')); });
      without.forEach(function (c) { g2.appendChild(h('option', { value: c.iso2 }, c.name)); });
      sel.appendChild(g1); sel.appendChild(g2);
      buildTaxIndex(); fillTax(); buildMap(); setMode(); renderMethod();
      var first = (S.meta.aois.filter(function (a) { return a.kind === 'country' && a.mode === 'event' && a.n_series > 0; })[0] || S.meta.aois.filter(function (a) { return a.kind === 'country' && a.mode === 'event'; })[0]);
      if (first) pickCountry(first.iso2);
    }).catch(function (e) { errBox($('map'), e.message); $('n-sub').textContent = 'could not load'; });
    $('sel-country').addEventListener('change', function (e) { if (e.target.value) pickCountry(e.target.value); else { S.iso = ''; S.adm1 = ''; fillAdm1(''); } });
    $('sel-adm1').addEventListener('change', function (e) { S.adm1 = e.target.value; S.adhoc = null; load(); });
    $('m-event').addEventListener('click', function () { S.mode = 'event'; if (S.iso || S.adhoc) load(); else setMode(); });
    $('m-mention').addEventListener('click', function () { S.mode = 'mentioned'; S.adm1 = ''; $('sel-adm1').value = ''; if (S.adhoc) { S.adhoc = null; } if (S.iso) load(); else setMode(); });
    $('lv-topic').addEventListener('click', function () { setLevel('topic'); });
    $('lv-category').addEventListener('click', function () { setLevel('category'); });
    $('lv-subtopic').addEventListener('click', function () { setLevel('subtopic'); });
    $('tx-all').addEventListener('click', function () { setTax(''); });
    $('tx-domain').addEventListener('change', function (e) { setTax(e.target.value); });
    $('tx-topic').addEventListener('change', function (e) { setTax(e.target.value || $('tx-domain').value); });
    $('tx-sub').addEventListener('change', function (e) { setTax(e.target.value || $('tx-topic').value); });
    $('d-poly').addEventListener('click', function () { startDraw('polygon'); });
    $('d-box').addEventListener('click', function () { startDraw('box'); });
    $('d-rad').addEventListener('click', function () { startDraw('radius'); });
    $('d-cancel').addEventListener('click', endDraw);
    $('d-finish').addEventListener('click', function () {
      if (!S.draw || S.draw.pts.length < 3) return;
      var ring = S.draw.pts.map(function (p) { return [+p[0].toFixed(4), +p[1].toFixed(4)]; });
      finishSpec({ kind: 'polygon', coords: ring, label: 'custom polygon' });
    });
    $('dr-close').addEventListener('click', closeEvent); $('n-backdrop').addEventListener('click', closeEvent);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { if (S.draw) endDraw(); else closeEvent(); } });
  }
  function syncLevel() { ['topic', 'category', 'subtopic'].forEach(function (k) { $('lv-' + k).setAttribute('aria-pressed', S.level === k); }); }
  function setLevel(l) {
    S.level = l; syncLevel();
    if (S.bundle) load();
  }
  boot();
})();
