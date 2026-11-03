/* RIO — NEWS & IMPACT daily market briefing.
 *
 * Data:  GET  /api/rio/news/briefing?date=&refresh=
 *        POST /api/rio/news/briefing/refresh
 *        GET  /api/rio/news/briefing/history
 *
 * Every number on this page comes from the briefing payload and keeps its
 * source + timestamp. Labels such as "Positive"/"Bearish" are rules-based
 * interpretations and are marked as such. Nothing is filled in when a
 * provider fails: the card says what is missing instead.
 */
"use strict";

const RIONews = (() => {

  const SECTIONS = ["A","B","C","D","E","F","G","H","I","J","K","L","M","N","O","P"];

  const state = {
    data: null,
    loading: false,
    refreshing: false,
    error: null,
    date: "",                 // "" = live
    history: [],
    filter: "all",
    query: "",
    autoMs: 300000,
    lastLoaded: null,
    details: new Map(),
    detailSeq: 0,
    sourcesOpen: false,
  };

  let rootEl = null;
  let timer = null;
  let loadSeq = 0;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({
      headers: { "Authorization": `Bearer ${token()}` },
      cache: "no-store",
    }, opts || {}));
    let j = null;
    try { j = await r.json(); } catch (_) { /* empty body */ }
    if (r.status === 401) throw Object.assign(new Error("pairing required"), { code: 401 });
    if (!r.ok) throw Object.assign(
      new Error((j && (j.detail || j.error)) || `HTTP ${r.status}`),
      { code: r.status });
    return j || {};
  }

  function safeUrl(u) {
    try {
      const url = new URL(String(u || ""), location.origin);
      return /^https?:$/.test(url.protocol) ? url.href : "";
    } catch (_) { return ""; }
  }

  function relTime(iso) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const s = Math.round((Date.now() - t) / 1000);
    if (s < 0) {
      const ahead = -s;
      if (ahead < 3600) return `in ${Math.round(ahead / 60)}m`;
      if (ahead < 86400) return `in ${Math.round(ahead / 3600)}h`;
      return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    if (s < 86400 * 7) return `${Math.round(s / 86400)}d ago`;
    return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  const num = (v, d = 2) => {
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString(undefined, {
      minimumFractionDigits: d, maximumFractionDigits: d }) : "—";
  };
  const pct = (v) => (v == null || !Number.isFinite(Number(v))) ? "—"
    : `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
  const dirClass = (v) => {
    const n = Number(v);
    return !Number.isFinite(n) || n === 0 ? "flat" : n > 0 ? "pos" : "neg";
  };
  const sentimentClass = (s) => {
    s = String(s || "").toLowerCase();
    return s === "positive" ? "pos" : s === "negative" ? "neg" : "flat";
  };
  const importanceClass = (s) => String(s || "").toLowerCase() === "high" ? "hi"
    : String(s || "").toLowerCase() === "medium" ? "mid" : "lo";

  function ic(name) {
    try { return (window.RIOIcons && RIOIcons.get(name)) || ""; } catch (_) { return ""; }
  }

  function registerDetail(obj) {
    const id = `d${++state.detailSeq}`;
    state.details.set(id, obj);
    return id;
  }

  function detailButton(obj, inner, cls) {
    const id = registerDetail(obj);
    return `<button type="button" class="ni-row ${cls || ""}" data-detail="${id}">${inner}</button>`;
  }

  function extLink(url, text, cls) {
    const u = safeUrl(url);
    if (!u) return `<span class="${cls || ""}">${esc(text)}</span>`;
    return `<a class="${cls || ""}" href="${esc(u)}" target="_blank"
      rel="noopener noreferrer" data-ext="1">${esc(text)}</a>`;
  }

  // ── page shell ──────────────────────────────────────────────────────
  function build() {
    const wrap = document.createElement("div");
    wrap.className = "ni-wrap";
    wrap.innerHTML = `
      <header class="ni-head">
        <div class="ni-head-main">
          <div class="ni-eyebrow">${ic("radio")} JARVIS · MARKET INTELLIGENCE</div>
          <h1>NEWS <span>&amp;</span> IMPACT</h1>
          <p class="ni-sub">A sourced daily briefing for Indian markets. Prices,
          flows and filings carry their provider and timestamp; interpretation
          is labelled and never mixed with measured data.</p>
        </div>
        <div class="ni-head-side">
          <div class="ni-head-badges" id="ni-badges">
            <span class="ni-badge skel">loading briefing…</span>
          </div>
          <div class="ni-controls">
            <label class="ni-select-wrap" title="Load a stored historical briefing">
              <span>Briefing</span>
              <select id="ni-date" aria-label="Briefing date">
                <option value="">Live · latest</option>
              </select>
            </label>
            <label class="ni-select-wrap" title="Auto-refresh interval while this tab is visible">
              <span>Auto</span>
              <select id="ni-auto" aria-label="Auto refresh interval">
                <option value="60000">1 min</option>
                <option value="300000" selected>5 min</option>
                <option value="900000">15 min</option>
                <option value="1800000">30 min</option>
                <option value="0">Off</option>
              </select>
            </label>
            <button class="btn ni-refresh" id="ni-refresh" type="button">
              ${ic("refresh")} <span>Refresh</span>
            </button>
          </div>
        </div>
      </header>

      <div class="ni-status" id="ni-status">
        <span class="skel">fetching sources and building the briefing…</span>
      </div>

      <section class="ni-pulse" id="ni-pulse" aria-label="Market pulse"></section>

      <div class="ni-toolbar">
        <div class="ni-filters" id="ni-filters" role="tablist" aria-label="Jump to section">
          <button class="ni-chip active" data-filter="all" role="tab">All sections</button>
          ${SECTIONS.map(k => `<button class="ni-chip" data-filter="${k}" role="tab">${k}</button>`).join("")}
        </div>
        <form class="ni-search" id="ni-search" role="search">
          <span class="ni-search-ico">${ic("search")}</span>
          <input id="ni-q" type="search" autocomplete="off"
                 placeholder="Filter this briefing — company, sector, source…"
                 aria-label="Filter briefing" />
        </form>
        <button class="btn ni-ghost" id="ni-sources-toggle" type="button">
          ${ic("database")} <span>Sources</span>
        </button>
      </div>

      <div class="ni-sources" id="ni-sources" hidden></div>

      <div class="ni-notes" id="ni-notes"></div>

      <main id="ni-body" class="ni-body">
        ${skeletons()}
      </main>

      <footer class="ni-foot" id="ni-foot"></footer>

      <div class="ni-modal" id="ni-modal" hidden>
        <div class="ni-modal-card" role="dialog" aria-modal="true" aria-labelledby="ni-modal-title">
          <button class="ni-modal-x" type="button" data-modal-close aria-label="Close">×</button>
          <div id="ni-modal-body"></div>
        </div>
      </div>`;
    rootEl = wrap;

    wrap.querySelector("#ni-refresh").addEventListener("click", () => load(true));
    wrap.querySelector("#ni-date").addEventListener("change", (e) => {
      state.date = e.target.value;
      load(false);
    });
    wrap.querySelector("#ni-auto").addEventListener("change", (e) => {
      state.autoMs = Number(e.target.value) || 0;
      try { localStorage.setItem("ni_auto_ms", String(state.autoMs)); } catch (_) {}
      startAuto();
    });
    wrap.querySelector("#ni-filters").addEventListener("click", (e) => {
      const b = e.target.closest("[data-filter]");
      if (!b) return;
      state.filter = b.dataset.filter;
      syncFilter();
      applyFilters();
    });
    wrap.querySelector("#ni-search").addEventListener("submit", (e) => {
      e.preventDefault(); applyFilters();
    });
    wrap.querySelector("#ni-q").addEventListener("input", (e) => {
      state.query = e.target.value.trim().toLowerCase();
      applyFilters();
    });
    wrap.querySelector("#ni-sources-toggle").addEventListener("click", () => {
      state.sourcesOpen = !state.sourcesOpen;
      const box = wrap.querySelector("#ni-sources");
      if (box) box.hidden = !state.sourcesOpen;
    });

    wrap.addEventListener("click", (e) => {
      if (e.target.closest("[data-modal-close]")) { closeModal(); return; }
      if (e.target.closest("#ni-modal") && !e.target.closest(".ni-modal-card")) {
        closeModal(); return;
      }
      if (e.target.closest("[data-ext]")) return;
      const retry = e.target.closest("[data-ni-retry]");
      if (retry) { load(true); return; }
      const row = e.target.closest("[data-detail]");
      if (row) {
        const d = state.details.get(row.dataset.detail);
        if (d) openDetail(d);
      }
    });
    wrap.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeModal();
    });

    try {
      const rawAuto = localStorage.getItem("ni_auto_ms");
      const saved = rawAuto == null ? NaN : Number(rawAuto);
      if (Number.isFinite(saved) && saved >= 0) {
        state.autoMs = saved;
        wrap.querySelector("#ni-auto").value = String(saved);
      }
    } catch (_) {}

    startAuto();
    queueMicrotask(() => { load(false); loadHistory(); });
    if (window.RIOHermesDock) {
      RIOHermesDock.attach(wrap, {
        page: "news", label: "News & Impact", context: hermesContext,
      });
    }
    return wrap;
  }

  // ── data ────────────────────────────────────────────────────────────
  async function loadHistory() {
    try {
      const d = await api("/api/rio/news/briefing/history?limit=30");
      state.history = Array.isArray(d.items) ? d.items : [];
      const sel = rootEl && rootEl.querySelector("#ni-date");
      if (!sel) return;
      const opts = [`<option value="">Live · latest</option>`].concat(
        state.history.map(h => `<option value="${esc(h.date)}">${esc(h.date_label || h.date)}</option>`));
      sel.innerHTML = opts.join("");
      sel.value = state.date;
    } catch (_) { /* history is optional; the live view still works */ }
  }

  async function load(force) {
    const seq = ++loadSeq;
    const live = !state.date;
    state.loading = !state.data;
    state.refreshing = !!state.data;
    state.error = null;
    paintStatus();

    try {
      let data;
      if (force && live) {
        data = await api("/api/rio/news/briefing/refresh", { method: "POST" });
      } else {
        const qs = new URLSearchParams();
        if (state.date) qs.set("date", state.date);
        if (force) qs.set("refresh", "1");
        data = await api(`/api/rio/news/briefing?${qs.toString()}`);
      }
      if (seq !== loadSeq || !rootEl || !rootEl.isConnected) return;
      state.data = data;
      state.lastLoaded = new Date();
      if (data && data.ok === false) {
        state.error = (data.errors && data.errors[0])
          || data.error || "the briefing provider returned no verified data";
      }
    } catch (e) {
      if (seq !== loadSeq || !rootEl || !rootEl.isConnected) return;
      state.error = e.code === 401
        ? "Pairing required — reload the page and enter the dashboard token."
        : e.message;
    } finally {
      if (seq !== loadSeq) return;
      state.loading = false;
      state.refreshing = false;
      if (rootEl && rootEl.isConnected) paint();
    }
  }

  function startAuto() {
    clearInterval(timer);
    if (!state.autoMs) return;
    timer = setInterval(() => {
      if (!rootEl || !rootEl.isConnected) { clearInterval(timer); return; }
      if (document.hidden || state.loading || state.date) return;
      load(false);
    }, state.autoMs);
  }

  // ── rendering helpers ───────────────────────────────────────────────
  function skeletons() {
    return `<div class="ni-section ni-skel-wrap">
      <div class="ni-skel ni-skel-hero"></div>
      <div class="ni-skel-grid">
        ${Array.from({ length: 6 }, () => `<div class="ni-skel ni-skel-card"></div>`).join("")}
      </div>
    </div>`;
  }

  const empty = (msg) => `<div class="ni-empty">${esc(msg || "No verified data for this section.")}</div>`;

  function badge(text, cls) {
    return `<span class="ni-badge ${cls || ""}">${esc(text)}</span>`;
  }

  function changeChip(v) {
    return `<span class="ni-chg ${dirClass(v)}">${esc(pct(v))}</span>`;
  }

  function quoteCard(q, compact) {
    if (!q) return empty("Quote not served.");
    if (q.ok === false) return empty(q.error || "This instrument did not answer the provider.");
    return `<div class="ni-quote ${compact ? "compact" : ""}">
      <div class="ni-quote-top">
        <span class="ni-quote-name">${esc(q.name || q.symbol || "—")}</span>
        <span class="ni-chg-wrap">${changeChip(q.change_pct)}
          ${q.change_basis ? `<span class="ni-basis">${esc(q.change_basis)}</span>` : ""}</span>
      </div>
      <div class="ni-quote-px">${esc(q.price == null ? "—" : num(q.price, 2))}
        <small>${esc(q.currency || "")}</small></div>
      <div class="ni-quote-meta">
        <span>${esc(q.as_of_label || q.session_label || "")}</span>
        <span>${esc(q.source || "")}${q.provenance ? ` · ${esc(q.provenance)}` : ""}</span>
      </div>
    </div>`;
  }

  function factorRows(factors) {
    if (!Array.isArray(factors) || !factors.length) return empty("No measured inputs were served.");
    return `<div class="ni-factors">${factors.map(f => {
      const v = f.value != null ? f.value : f.change_label;
      const sign = String(f.direction) === "negative" ? -1
        : String(f.direction) === "positive" ? 1
        : (f.change_pct != null ? Number(f.change_pct) : 0);
      return `<div class="ni-factor">
        <div class="ni-factor-l">${esc(f.label || "")}</div>
        <div class="ni-factor-v ${dirClass(sign)}">${esc(v || "—")}</div>
        ${f.invert_note ? `<div class="ni-factor-n">${esc(f.invert_note)}</div>` : ""}
        ${f.note ? `<div class="ni-factor-n">${esc(f.note)}</div>` : ""}
        <div class="ni-factor-s">${esc(f.as_of_label || "")}${f.source ? ` · ${esc(f.source)}` : ""}</div>
      </div>`;
    }).join("")}</div>`;
  }

  function newsRow(a, opts) {
    const o = opts || {};
    const when = a.published_label || relTime(a.published);
    const sent = String(a.sentiment || "unclear");
    const inner = `
      <div class="ni-news-top">
        <span class="ni-src">${esc(a.source || a.feed || "source")}</span>
        <span class="ni-when">${esc(when || "time not served")}</span>
      </div>
      <div class="ni-news-title">${esc(a.title || "(untitled)")}</div>
      ${o.summary !== false && a.summary
        ? `<p class="ni-news-sum">${esc(a.summary)}</p>` : ""}
      <div class="ni-news-foot">
        <span class="ni-sent ${sentimentClass(sent)}">${esc(sent)}</span>
        <span class="ni-imp ${importanceClass(a.importance)}">${esc(a.importance || "—")}</span>
        ${(a.categories || []).slice(0, 3).map(c => `<span class="ni-tag">${esc(c)}</span>`).join("")}
        ${a.url ? `<span class="ni-open">Read source →</span>` : ""}
      </div>`;
    return detailButton(a, inner, "ni-news");
  }

  function newsList(items, n) {
    const rows = (Array.isArray(items) ? items : []).slice(0, n || 8);
    if (!rows.length) return empty("No verified item matched this section.");
    return `<div class="ni-news-list">${rows.map(a => newsRow(a)).join("")}</div>`;
  }

  function sectionShell(key, title, meta, body, opts) {
    const o = opts || {};
    const status = o.status ? `<span class="ni-sec-status ${o.status === "ok" ? "ok" : o.status === "partial" ? "mid" : "bad"}">${esc(o.status)}</span>` : "";
    return `<section class="ni-section" data-section="${key}" id="ni-sec-${key}">
      <div class="ni-sec-head">
        <div class="ni-sec-title">
          <span class="ni-sec-key">${key}</span>
          <span class="ni-sec-ico">${ic(o.icon || "newspaper")}</span>
          <h2>${esc(title)}</h2>
          ${status}
        </div>
        <div class="ni-sec-meta">${meta || ""}</div>
      </div>
      <div class="ni-sec-body ${o.wide ? "wide" : ""}">${body}</div>
      ${o.note ? `<div class="ni-sec-note">${esc(o.note)}</div>` : ""}
    </section>`;
  }

  function table(headers, rows, cls) {
    if (!rows.length) return empty("No rows were served.");
    return `<div class="ni-table-wrap"><table class="ni-table ${cls || ""}">
      <thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody>
    </table></div>`;
  }

  function statusOf(sec) {
    return (sec && (sec.status || (sec.data && sec.data.status))) || "";
  }

  // ── section renderers ───────────────────────────────────────────────
  const renderers = {
    A(sec) {
      const items = sec.items || [];
      return sectionShell("A", sec.title, `${items.length} item${items.length === 1 ? "" : "s"}`,
        newsList(items, 10), { icon: "newspaper", status: sec.status, note: sec.note });
    },

    B(sec) {
      const d = sec.data || {};
      if (!d.direction) return sectionShell("B", sec.title, "", empty("Outlook inputs were not available."),
        { icon: "target", status: sec.status });
      const factors = factorRows(d.factors);
      const triggers = (d.change_triggers || []).map(t => `<li>${esc(t)}</li>`).join("");
      const events = (d.events_today || []).slice(0, 6);
      const cal = (d.calendar_high_impact || []).slice(0, 6);
      const nifty = (d.levels || {}).nifty || [];
      return sectionShell("B", sec.title,
        `<span class="ni-dir ${String(d.direction).toLowerCase() === "bullish" ? "pos" : String(d.direction).toLowerCase() === "bearish" ? "neg" : "flat"}">${esc(d.direction)}</span>
         <span class="ni-confidence">${esc(d.confidence || "")}</span>`,
        `<p class="ni-lead">${esc(d.summary || "")}</p>
         <div class="ni-two">
           <div>
             <h3 class="ni-h3">Measured inputs</h3>
             ${factors}
           </div>
           <div>
             <h3 class="ni-h3">What would change the read</h3>
             <ul class="ni-list">${triggers || "<li>No verified trigger.</li>"}</ul>
             <h3 class="ni-h3">NIFTY reference levels</h3>
             <div class="ni-levels">${nifty.slice(0, 5).map(l =>
               `<span class="ni-level ${l.kind}">${esc(l.label)} <b>${num(l.price)}</b></span>`).join("") || empty("Levels unavailable.")}</div>
           </div>
         </div>
         ${events.length ? `<h3 class="ni-h3">On the calendar today</h3>
           <div class="ni-mini-list">${events.map(e =>
             `<div class="ni-mini"><span>${esc(e.company || "")}</span>
              <small>${esc(e.purpose || "")} · ${esc(e.date_label || "")}</small></div>`).join("")}</div>` : ""}
         ${cal.length ? `<h3 class="ni-h3">High-impact global events</h3>
           <div class="ni-mini-list">${cal.map(e =>
             `<div class="ni-mini"><span>${esc(e.title || "")}</span>
              <small>${esc(e.country || e.currency || "")} · ${esc(e.date_label || e.date || "")} ${esc(e.time_label || "")} · ${esc(e.impact || "")}</small></div>`).join("")}</div>` : ""}`,
        { icon: "target", status: sec.status, wide: true,
          note: (sec.note || "") + " Direction and confidence are transparent rules over the measured inputs, not a forecast." });
    },

    C(sec) {
      const regions = (sec.data || {}).regions || [];
      const crypto = (sec.data || {}).crypto || [];
      if (!regions.length && !crypto.length) return sectionShell("C", sec.title, "", empty("No global quotes were served."),
        { icon: "gauge", status: sec.status });
      const regionHtml = regions.length ? `<div class="ni-regions">${regions.map(r => `
          <div class="ni-region">
            <div class="ni-region-h">${esc(r.region)} <small>${r.status === "ok" ? "live / delayed" : esc(r.status || "")}</small></div>
            <div class="ni-quote-grid">${(r.items || []).map(q => quoteCard(q, true)).join("")}</div>
          </div>`).join("")}</div>` : "";
      const cryptoHtml = crypto.length ? `<h3 class="ni-h3">Crypto · 24/7</h3>
        <div class="ni-quote-grid">${crypto.map(q => quoteCard(q, true)).join("")}</div>
        ${sec.data && sec.data.crypto_note ? `<div class="ni-inline-note">${esc(sec.data.crypto_note)}</div>` : ""}` : "";
      return sectionShell("C", sec.title, `${regions.length} regions · ${crypto.length} crypto`,
        `${regionHtml}${cryptoHtml}`,
        { icon: "gauge", status: sec.status, note: sec.note, wide: true });
    },

    D(sec) { return commoditySection("D", sec, "Brent crude"); },
    E(sec) { return commoditySection("E", sec, "Gold"); },

    F(sec) {
      const d = sec.data || {};
      return sectionShell("F", sec.title, d.instrument || "",
        `${quoteCard(d.quote)}
         ${d.factors && d.factors.length ? `<h3 class="ni-h3">Transmission factors</h3>${factorRows(d.factors)}` : ""}
         ${d.implications && d.implications.length ? `<h3 class="ni-h3">Why it matters</h3>
           <ul class="ni-list">${d.implications.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
         ${newsList(sec.items, 5)}`,
        { icon: "trending-up", status: statusOf(sec), note: sec.note,
          wide: true });
    },

    G(sec) {
      const d = sec.data || {};
      const rows = (d.rows || []).map(r => [
        `<b>${esc(r.category)}</b>`,
        num(r.buy), num(r.sell),
        `<span class="${dirClass(r.net)}">${esc(r.net_label || num(r.net))}</span>`,
      ]);
      return sectionShell("G", sec.title, d.date ? `NSE · ${esc(d.date)}` : "NSE",
        `${table(["Category","Buy (₹ cr)","Sell (₹ cr)","Net"], rows, "ni-flows")}
         ${d.error ? empty(d.error) : ""}`,
        { icon: "database", status: statusOf(sec), note: sec.note,
          source: d.source, wide: false });
    },

    H(sec) {
      const items = ((sec.data || {}).items) || [];
      if (!items.length) return sectionShell("H", sec.title, "", empty("No focus item was verified."),
        { icon: "zap", status: sec.status });
      return sectionShell("H", sec.title, `${items.length} ranked`,
        `<ol class="ni-focus">${items.map(it => `
          <li>
            <div class="ni-focus-top">
              <span class="ni-rank">${it.rank || ""}</span>
              <span class="ni-focus-title">${extLink(it.url, it.title || "")}</span>
            </div>
            <p>${esc(it.why || "")}</p>
            <div class="ni-focus-meta">
              <span class="ni-sent ${sentimentClass(it.sentiment)}">${esc(it.sentiment || "—")}</span>
              <span>${esc(it.source || "")}</span>
              <span>${esc(it.published_label || "")}</span>
            </div>
          </li>`).join("")}</ol>`,
        { icon: "zap", status: sec.status, note: sec.note, wide: true });
    },

    I(sec) {
      const rows = ((sec.data || {}).sectors) || [];
      if (!rows.length) return sectionShell("I", sec.title, "", empty("No sector instrument answered."),
        { icon: "list", status: sec.status });
      const body = `<div class="ni-sectors">${rows.map(s => {
        const impactCls = String(s.impact || "").toLowerCase().startsWith("pos") ? "pos"
          : String(s.impact || "").toLowerCase().startsWith("neg") ? "neg" : "flat";
        return `<div class="ni-sector">
          <div class="ni-sector-top">
            <span class="ni-sector-name">${esc(s.name)}</span>
            ${changeChip(s.change_pct)}
            <span class="ni-impact ${impactCls}">${esc(s.impact || "—")}</span>
          </div>
          <div class="ni-sector-trend">${esc(s.trend || "")}${s.history_bars != null ? ` · ${s.history_bars} bars` : ""}</div>
          ${s.catalyst ? `<div class="ni-sector-cat">${extLink(s.catalyst_url, s.catalyst)}
            <small>${esc(s.catalyst_source || "")}</small></div>` : `<div class="ni-sector-cat muted">No verified sector catalyst.</div>`}
          <div class="ni-sector-meta">
            <span>${esc(s.price_source || "")}</span>
            <span>${esc(s.as_of_label || "")}</span>
            <span class="ni-ev ${s.evidence === "sufficient" ? "ok" : "mid"}">${esc(s.evidence || "")}</span>
          </div>
        </div>`;
      }).join("")}</div>`;
      return sectionShell("I", sec.title, `${rows.length} sectors`, body,
        { icon: "list", status: sec.status, note: (sec.data || {}).note || sec.note, wide: true });
    },

    J(sec) {
      const d = sec.data || {};
      const recent = d.recent || [];
      const upcoming = d.upcoming || [];
      const cards = recent.map(r => {
        const catCls = /strong/i.test(r.category) ? "pos" : /weak|below/i.test(r.category) ? "neg" : "flat";
        return `<div class="ni-result">
          <div class="ni-result-top">
            <span class="ni-result-co">${esc(r.company)}</span>
            <span class="ni-impact ${catCls}">${esc(r.category || "—")}</span>
          </div>
          <div class="ni-result-q">${esc(r.quarter || "")} · reported ${esc(r.reported_label || r.reported_date || "")}</div>
          <div class="ni-result-grid">
            <div><span>Revenue</span><b>${esc(r.revenue_label || "—")}</b>
              <small class="${dirClass(r.revenue_yoy)}">${esc(r.revenue_yoy_label || "")} YoY</small></div>
            <div><span>Net profit</span><b>${esc(r.net_profit_label || "—")}</b>
              <small class="${dirClass(r.net_profit_yoy)}">${esc(r.net_profit_yoy_label || "")} YoY</small></div>
            <div><span>EPS vs est.</span><b>${esc(r.surprise_label || "—")}</b>
              <small>${r.eps_actual != null ? `actual ${num(r.eps_actual)}` : ""}</small></div>
          </div>
          <p class="ni-result-why">${esc(r.category_reason || "")}</p>
          <div class="ni-result-meta">
            <span>${esc(r.source || "")}</span>
            ${r.filing_url ? extLink(r.filing_url, "Exchange filing →") : ""}
          </div>
        </div>`;
      }).join("");
      return sectionShell("J", sec.title, `${recent.length} reported · ${upcoming.length} upcoming`,
        `${recent.length ? `<div class="ni-results">${cards}</div>` : empty("No recent result could be verified.")}
         ${upcoming.length ? `<h3 class="ni-h3">Upcoming board meetings</h3>
           <div class="ni-mini-list">${upcoming.map(e =>
             `<div class="ni-mini"><span>${esc(e.company || "")}</span>
              <small>${esc(e.purpose || "")} · ${esc(e.date_label || e.date || "")}</small></div>`).join("")}</div>` : ""}
         ${d.note ? `<div class="ni-inline-note">${esc(d.note)}</div>` : ""}`,
        { icon: "gauge", status: sec.status, note: "Revenue and profit are shown only when the provider's statement matches the reported quarter.", wide: true });
    },

    K(sec) { return sectionShell("K", sec.title, `${(sec.items || []).length} filings`,
      newsList(sec.items, 12), { icon: "inbox", status: sec.status, note: sec.note, wide: true }); },

    L(sec) {
      const rows = ((sec.data || {}).indices) || [];
      if (!rows.length) return sectionShell("L", sec.title, "", empty("No index history was served."),
        { icon: "activity", status: sec.status });
      return sectionShell("L", sec.title, "rules-based technicals",
        `<div class="ni-tech-grid">${rows.map(t => `
          <div class="ni-tech">
            <div class="ni-tech-top">
              <span>${esc(t.name)}</span>${changeChip(t.change_pct)}
            </div>
            <div class="ni-tech-px">${num(t.last)}</div>
            <div class="ni-tech-src">${esc(t.price_source || "")} · ${esc(t.as_of_label || "")}</div>
            <div class="ni-tech-rows">
              <div><span>Trend</span><b>${esc(t.trend || "—")}</b></div>
              <div><span>Momentum</span><b>${esc(t.momentum || "—")}</b></div>
              <div><span>20 / 50 / 200 DMA</span><b>${num(t.sma20)} / ${num(t.sma50)} / ${num(t.sma200)}</b></div>
              <div><span>RSI(14) · ATR(14)</span><b>${num(t.rsi14)} · ${num(t.atr14)}</b></div>
              <div><span>VWAP</span><b>${t.vwap != null ? num(t.vwap) : "—"}</b></div>
            </div>
            <div class="ni-levels">${(t.levels || []).slice(0, 7).map(l =>
              `<span class="ni-level ${l.kind}">${esc(l.label)} <b>${num(l.price)}</b></span>`).join("")}</div>
            <div class="ni-scenarios">
              <div class="pos"><b>Bull</b> ${esc((t.scenarios || {}).bullish || "—")}</div>
              <div class="neg"><b>Bear</b> ${esc((t.scenarios || {}).bearish || "—")}</div>
              <div><b>Neutral</b> ${esc((t.scenarios || {}).neutral || "—")}</div>
            </div>
            ${t.price_note ? `<div class="ni-inline-note">${esc(t.price_note)}</div>` : ""}
            ${t.vwap_note ? `<div class="ni-inline-note">${esc(t.vwap_note)}</div>` : ""}
          </div>`).join("")}</div>`,
        { icon: "activity", status: sec.status, note: (sec.data || {}).note || sec.note, wide: true });
    },

    M(sec) {
      const items = sec.items || [];
      if (!items.length) return sectionShell("M", sec.title, "",
        empty("No broker note with an explicit rating or target was found in the feeds."),
        { icon: "user", status: sec.status });
      return sectionShell("M", sec.title, `${items.length} reported notes`,
        `<div class="ni-brokers">${items.map(b => `
          <div class="ni-broker">
            <div class="ni-broker-top">
              <span class="ni-broker-co">${esc(b.company || "")}</span>
              <span class="ni-badge">${esc(b.broker || "")}</span>
            </div>
            <div class="ni-broker-rec">
              <span>${esc(b.recommendation || "")}</span>
              <b>${esc(b.target || "")}</b>
            </div>
            <div class="ni-broker-title">${extLink(b.url, b.title || "")}</div>
            <div class="ni-broker-meta">${esc(b.source || "")} · ${esc(b.date_label || "")}</div>
            <p class="ni-inline-note">${esc(b.risks || "")}</p>
          </div>`).join("")}</div>`,
        { icon: "user", status: sec.status, note: sec.note, wide: true });
    },

    N(sec) {
      const d = sec.data || {};
      const ipos = d.ipos || [];
      const actions = d.actions || [];
      const ipoRows = ipos.map(i => [
        `<b>${esc(i.company)}</b><br><small>${esc(i.symbol || "")}</small>`,
        esc(i.open || "—"), esc(i.close || "—"), esc(i.price_band || "—"),
        `<span class="${Number(i.subscription_times) >= 1 ? "pos" : "flat"}">${esc(i.subscription_label || "—")}</span>`,
        esc(i.status || "—"),
      ]);
      const actRows = actions.map(a => [
        `<b>${esc(a.company || a.symbol || "")}</b>`,
        esc(a.subject || a.purpose || a.action || ""),
        esc(a.ex_date || a.date || ""),
        esc(a.detail || a.remarks || a.status || a.record_date || ""),
      ]);
      return sectionShell("N", sec.title, `${ipos.length} IPOs · ${actions.length} actions`,
        `${ipoRows.length ? table(["Company","Open","Close","Band","Subscription","Status"], ipoRows, "ni-ipo") : empty("No live IPO was served by NSE.")}
         ${actRows.length ? `<h3 class="ni-h3">Corporate actions</h3>${table(["Company","Action","Ex-date","Detail"], actRows, "ni-actions")}` : ""}
         ${newsList(sec.items, 5)}`,
        { icon: "calendar", status: sec.status, note: sec.note, wide: true });
    },

    O(sec) {
      const ideas = ((sec.data || {}).ideas) || [];
      if (!ideas.length) return sectionShell("O", sec.title, "", empty("No watchlist name passed the technical screen."),
        { icon: "search", status: sec.status });
      return sectionShell("O", sec.title, `${ideas.length} research candidates`,
        `<div class="ni-ideas">${ideas.map(i => `
          <div class="ni-idea">
            <div class="ni-idea-top">
              <span>${esc(i.name)} <small>${esc(i.symbol)}</small></span>
              ${changeChip(i.change_pct)}
            </div>
            <div class="ni-idea-class">${esc(i.classification || "")}</div>
            <p>${esc(i.appearance_reason || "")}</p>
            ${i.catalyst ? `<div class="ni-idea-cat">${extLink(i.catalyst_url, i.catalyst)}</div>` : ""}
            <ul class="ni-list">${(i.technical_evidence || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
            <div class="ni-idea-risk">${esc(i.risk || "")}</div>
            <div class="ni-broker-meta">${esc(i.as_of_label || "")} · ${esc(i.evidence_strength || "")} evidence</div>
          </div>`).join("")}</div>`,
        { icon: "search", status: sec.status, note: sec.note, wide: true });
    },

    P(sec) {
      const d = sec.data || {};
      const idx = d.indices || [];
      const leaders = d.leaders || [];
      const laggards = d.laggards || [];
      const flows = d.flows;
      const drivers = d.drivers || [];
      const carry = d.carry_over || [];
      return sectionShell("P", sec.title, esc(d.session_label || ""),
        `<div class="ni-two">
          <div>
            <h3 class="ni-h3">Indices</h3>
            <div class="ni-mini-list">${idx.map(i =>
              `<div class="ni-mini"><span>${esc(i.name)}</span>
                <small>${num(i.close)} · <b class="${dirClass(i.change_pct)}">${esc(i.change_label || pct(i.change_pct))}</b> · ${esc(i.source || "")}</small></div>`).join("") || empty("No index row.")}</div>
            ${leaders.length ? `<h3 class="ni-h3">Sector leaders</h3>
              <div class="ni-mini-list">${leaders.map(s => `<div class="ni-mini"><span>${esc(s.name)}</span><small class="pos">${esc(s.change_label)}</small></div>`).join("")}</div>` : ""}
            ${laggards.length ? `<h3 class="ni-h3">Sector laggards</h3>
              <div class="ni-mini-list">${laggards.map(s => `<div class="ni-mini"><span>${esc(s.name)}</span><small class="neg">${esc(s.change_label)}</small></div>`).join("")}</div>` : ""}
          </div>
          <div>
            <h3 class="ni-h3">Session drivers</h3>
            ${newsList(drivers, 5)}
            ${flows && flows.rows ? `<h3 class="ni-h3">Flows</h3>
              <div class="ni-mini-list">${flows.rows.map(r =>
                `<div class="ni-mini"><span>${esc(r.category)}</span><small class="${dirClass(r.net)}">${esc(r.net_label)}</small></div>`).join("")}</div>` : ""}
            ${carry.length ? `<h3 class="ni-h3">Carry-over events</h3>
              <div class="ni-mini-list">${carry.map(e => `<div class="ni-mini"><span>${esc(e.company || "")}</span><small>${esc(e.purpose || "")} · ${esc(e.date_label || e.date || "")}</small></div>`).join("")}</div>` : ""}
          </div>
        </div>`,
        { icon: "clock", status: sec.status, note: sec.note, wide: true });
    },
  };

  function commoditySection(key, sec, label) {
    const d = sec.data || {};
    const q = d.quote;
    if (!q && !d.factors) return sectionShell(key, sec.title, "", empty(`${label} quote was not served.`),
      { icon: key === "E" ? "sparkles" : "activity", status: sec.status, note: sec.note });
    const drivers = factorRows(d.drivers);
    return sectionShell(key, sec.title, d.instrument || "",
      `${quoteCard(q)}
       ${d.spot_quote ? `<div class="ni-inline-note">Spot reference: ${esc(d.spot_quote.price)}
         ${esc(d.spot_quote.change_label || "")} · ${esc(d.spot_quote.source || "")}</div>` : ""}
       ${d.spot_note ? `<div class="ni-inline-note">${esc(d.spot_note)}</div>` : ""}
       ${d.drivers && d.drivers.length ? `<h3 class="ni-h3">Drivers</h3>${drivers}` : ""}
       ${d.factors && d.factors.length ? `<h3 class="ni-h3">Headline factors</h3>
         <div class="ni-mini-list">${d.factors.slice(0, 5).map(f =>
           `<div class="ni-mini"><span>${esc(f.label || "")}</span>
            <small class="${sentimentClass(f.sentiment)}">${esc(f.sentiment || "")} · ${esc(f.source || "")}</small></div>`).join("")}</div>` : ""}
       ${d.implications && d.implications.length ? `<h3 class="ni-h3">Why it matters</h3>
         <ul class="ni-list">${d.implications.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
       ${newsList(sec.items, 5)}`,
      { icon: key === "E" ? "sparkles" : "activity", status: sec.status, note: sec.note,
        wide: true });
  }

  // ── page paint ──────────────────────────────────────────────────────
  function paintStatus() {
    const box = rootEl && rootEl.querySelector("#ni-status");
    if (!box) return;
    if (state.loading && !state.data) {
      box.innerHTML = `<span class="skel">fetching live sources and building the briefing…</span>`;
      return;
    }
    if (state.error && !state.data) {
      box.innerHTML = `<span class="ni-err">${esc(state.error)}</span>`;
      return;
    }
    const d = state.data;
    if (!d) { box.innerHTML = ""; return; }
    const fresh = d.freshness || {};
    const meta = d.meta || {};
    const srcs = Array.isArray(d.sources) ? d.sources : [];
    const okSrc = srcs.filter(s => s.ok).length;
    const parts = [];
    parts.push(`<span>${esc((d.session || {}).date_label || d.date || "")}</span>`);
    parts.push(`<span>${esc((d.session || {}).phase_label || "")}</span>`);
    if (fresh.generated_at) {
      parts.push(`<span>built ${esc(new Date(fresh.generated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}
        (${esc(relTime(fresh.generated_at))})</span>`);
    }
    if (d.sections_ok != null) parts.push(`<span>${d.sections_ok}/${d.sections_total || 16} sections</span>`);
    if (srcs.length) parts.push(`<span>${okSrc}/${srcs.length} sources</span>`);
    if (meta.elapsed_ms != null) parts.push(`<span>${meta.elapsed_ms} ms</span>`);
    if (meta.cached) parts.push(`<span>cached</span>`);
    if (state.refreshing) parts.push(`<span class="ni-pulse-dot">refreshing…</span>`);
    if (fresh.stale) parts.push(`<span class="ni-stale">stale: ${esc(fresh.stale_reason || "data older than the refresh interval")}</span>`);
    if ((d.errors || []).length) parts.push(`<span class="ni-warn">${d.errors.length} provider note${d.errors.length === 1 ? "" : "s"}</span>`);
    box.innerHTML = parts.join('<span class="sep">·</span>');
  }

  function paintBadges() {
    const box = rootEl && rootEl.querySelector("#ni-badges");
    if (!box) return;
    const d = state.data;
    if (!d) {
      box.innerHTML = `<span class="ni-badge skel">no payload</span>`;
      return;
    }
    const s = d.session || {};
    const hist = (d.meta || {}).historical;
    box.innerHTML = [
      badge(s.date_label || d.date || "—", "strong"),
      badge(s.phase_label || "—", "phase"),
      hist ? badge("Historical", "hist") : badge("Live", "live"),
      d.partial ? badge("Partial data", "warn") : badge("Complete", "ok"),
    ].join("");
  }

  function paintPulse() {
    const box = rootEl && rootEl.querySelector("#ni-pulse");
    if (!box) return;
    const d = state.data;
    const pulse = (d && Array.isArray(d.pulse)) ? d.pulse : [];
    if (!pulse.length) { box.innerHTML = ""; return; }
    const india = pulse.filter(q => q.group === "india").slice(0, 6);
    const global = pulse.filter(q => q.group !== "india").slice(0, 8);
    const tile = q => `<div class="ni-pulse-tile ${q.ok === false ? "bad" : ""}">
      <div class="ni-pulse-name">${esc(q.name || q.symbol)}</div>
      <div class="ni-pulse-px">${q.ok === false ? "n/a" : num(q.price)}</div>
      ${q.ok === false ? `<div class="ni-pulse-chg flat">${esc(q.error || "unavailable")}</div>`
        : `<div class="ni-pulse-chg ${dirClass(q.change_pct)}">${esc(q.change_label || pct(q.change_pct))}</div>`}
      <div class="ni-pulse-meta">${esc(q.as_of_label || "")}</div>
    </div>`;
    box.innerHTML = `
      <div class="ni-pulse-label">India</div>
      <div class="ni-pulse-row">${india.map(tile).join("")}</div>
      <div class="ni-pulse-label">Global</div>
      <div class="ni-pulse-row">${global.map(tile).join("")}</div>`;
  }

  function paintSources() {
    const box = rootEl && rootEl.querySelector("#ni-sources");
    if (!box) return;
    const d = state.data;
    const srcs = (d && Array.isArray(d.sources)) ? d.sources : [];
    if (!srcs.length) { box.innerHTML = empty("No source status was reported."); return; }
    const ok = srcs.filter(s => s.ok).length;
    box.innerHTML = `<div class="ni-sources-head">
        <b>${ok}/${srcs.length} sources answered</b>
        <span>Every source is listed with its own status. A failed source is never replaced with invented content.</span>
      </div>
      <div class="ni-sources-grid">${srcs.map(s => `
        <div class="ni-source ${s.ok ? "ok" : "bad"}">
          <div><i class="dot ${s.ok ? "dot-ok" : "dot-err"}"></i>${esc(s.name)}</div>
          <small>${s.ok ? `${s.count || 0} items · ${s.ms != null ? `${s.ms}ms` : ""}`
            : esc(s.detail || "failed")}</small>
        </div>`).join("")}</div>`;
  }

  function paintNotes() {
    const box = rootEl && rootEl.querySelector("#ni-notes");
    if (!box) return;
    const d = state.data;
    if (!d) { box.innerHTML = ""; return; }
    const errors = d.errors || [];
    const hist = (d.meta || {}).historical;
    box.innerHTML = `
      ${hist ? `<div class="ni-banner hist">You are reading a stored briefing for ${esc(d.date)}. It is a snapshot, not live.</div>` : ""}
      ${d.partial && !errors.length ? `<div class="ni-banner warn">Some providers did not answer. Those cards say so; no value was filled in.</div>` : ""}
      ${errors.length ? `<details class="ni-banner warn"><summary>${errors.length} provider note${errors.length === 1 ? "" : "s"}</summary>
        <ul>${errors.map(e => `<li>${esc(e)}</li>`).join("")}</ul></details>` : ""}`;
  }

  function paintFoot() {
    const box = rootEl && rootEl.querySelector("#ni-foot");
    if (!box) return;
    const d = state.data;
    if (!d) { box.innerHTML = ""; return; }
    const meta = d.meta || {};
    box.innerHTML = `<div>${esc(d.disclaimer || "Research and information only. Not investment advice.")}</div>
      <div class="ni-foot-meta">
        <span>${esc(meta.interpretation || "Labels are rules-based; prices are provider-delivered.")}</span>
        <span>${esc(meta.provider || "news_briefing")}</span>
      </div>`;
  }

  function syncFilter() {
    if (!rootEl) return;
    rootEl.querySelectorAll("#ni-filters .ni-chip").forEach(b => {
      const on = b.dataset.filter === state.filter;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function applyFilters() {
    if (!rootEl) return;
    const secs = rootEl.querySelectorAll(".ni-section[data-section]");
    secs.forEach(s => {
      const key = s.dataset.section;
      let show = state.filter === "all" || state.filter === key;
      if (show && state.query) {
        show = (s.textContent || "").toLowerCase().includes(state.query);
      }
      s.hidden = !show;
    });
    const none = ![...secs].some(s => !s.hidden);
    let note = rootEl.querySelector(".ni-no-match");
    if (none && secs.length) {
      if (!note) {
        note = document.createElement("div");
        note.className = "ni-empty ni-no-match";
        note.innerHTML = `<strong>Nothing matched that filter.</strong>
          <p>Clear the search or choose “All sections”. No content is invented to fill the page.</p>`;
        rootEl.querySelector("#ni-body").appendChild(note);
      }
      note.hidden = false;
    } else if (note) {
      note.hidden = true;
    }
  }

  function paint() {
    if (!rootEl || !rootEl.isConnected) return;
    paintStatus();
    paintBadges();
    paintPulse();
    paintSources();
    paintNotes();
    paintFoot();

    const body = rootEl.querySelector("#ni-body");
    if (!body) return;
    state.details.clear();
    state.detailSeq = 0;

    if (!state.data && state.loading) {
      body.innerHTML = skeletons();
      return;
    }
    if (state.error && !state.data) {
      body.innerHTML = `<div class="ni-error">
        <strong>Briefing unavailable.</strong>
        <p>${esc(state.error)}</p>
        <p class="ni-inline-note">No mock briefing is shown in its place.</p>
        <button class="btn" type="button" data-ni-retry>Retry</button>
      </div>`;
      return;
    }
    const d = state.data;
    if (!d) { body.innerHTML = ""; return; }

    const sections = d.sections || {};
    const html = SECTIONS.map(k => {
      const sec = sections[k];
      if (!sec) {
        return sectionShell(k, `Section ${k}`, "", empty("This section was not returned."),
          { icon: "newspaper", status: "unavailable" });
      }
      const fn = renderers[k];
      try {
        return fn ? fn(sec) : sectionShell(k, sec.title || `Section ${k}`, "",
          newsList(sec.items, 8) || empty("No data."),
          { icon: sec.icon || "newspaper", status: sec.status, note: sec.note });
      } catch (e) {
        return sectionShell(k, sec.title || `Section ${k}`, "",
          empty(`This section could not be rendered: ${e.message}`),
          { icon: "newspaper", status: "error" });
      }
    }).join("");

    body.innerHTML = html || `<div class="ni-empty">The briefing returned no sections.</div>`;
    syncFilter();
    applyFilters();
  }

  // ── detail modal ────────────────────────────────────────────────────
  function openDetail(obj) {
    const modal = rootEl && rootEl.querySelector("#ni-modal");
    const box = rootEl && rootEl.querySelector("#ni-modal-body");
    if (!modal || !box) return;
    const isNews = obj.title || obj.summary || obj.url;
    box.innerHTML = isNews ? `
      <div class="ni-modal-kicker">${esc(obj.source || obj.feed || "")} · ${esc(obj.published_label || obj.date_label || "")}</div>
      <h2 id="ni-modal-title">${esc(obj.title || obj.company || "Detail")}</h2>
      ${obj.company && obj.title ? `<div class="ni-modal-sub">${esc(obj.company)} ${obj.symbol ? `· ${esc(obj.symbol)}` : ""}</div>` : ""}
      ${obj.summary ? `<p>${esc(obj.summary)}</p>` : ""}
      ${obj.category_reason ? `<p>${esc(obj.category_reason)}</p>` : ""}
      ${obj.why ? `<div class="ni-modal-block"><b>Why it matters</b><p>${esc(obj.why)}</p></div>` : ""}
      ${obj.sentiment ? `<div class="ni-modal-block"><b>Direction label</b>
        <p><span class="ni-sent ${sentimentClass(obj.sentiment)}">${esc(obj.sentiment)}</span>
        ${esc(obj.sentiment_evidence || "")}</p></div>` : ""}
      ${obj.impact ? `<div class="ni-modal-block"><b>Rule-based impact</b><p>${esc(obj.impact)}</p></div>` : ""}
      ${obj.recommendation ? `<div class="ni-modal-block"><b>Reported view</b>
        <p>${esc(obj.broker || "")}: ${esc(obj.recommendation)} · target ${esc(obj.target || "—")}</p></div>` : ""}
      ${obj.risks ? `<div class="ni-modal-block"><b>Risks</b><p>${esc(obj.risks)}</p></div>` : ""}
      ${obj.url ? `<a class="btn" href="${esc(safeUrl(obj.url))}" target="_blank" rel="noopener noreferrer">Open original source →</a>` : ""}
      <div class="ni-modal-foot">Labels are rules-based interpretations. Prices, flows and filings are provider data with the timestamp shown above.</div>
    ` : `<h2 id="ni-modal-title">${esc(obj.title || "Detail")}</h2>
      <pre class="ni-json">${esc(JSON.stringify(obj, null, 2))}</pre>`;
    modal.hidden = false;
    document.body.classList.add("ni-modal-open");
  }

  function closeModal() {
    const modal = rootEl && rootEl.querySelector("#ni-modal");
    if (modal) modal.hidden = true;
    document.body.classList.remove("ni-modal-open");
  }

  function hermesContext() {
    const d = state.data;
    const lines = ["Page: NEWS & IMPACT"];
    if (!d) {
      lines.push(`Data: ${state.loading ? "loading" : state.error ? `error — ${state.error}` : "not loaded"}`);
      return lines.join("\n");
    }
    const s = d.session || {};
    const B = ((d.sections || {}).B || {}).data || {};
    lines.push(`Briefing: ${s.date_label || d.date || ""} (${s.phase_label || ""})`);
    lines.push(`View: ${state.date ? `historical ${state.date}` : "live"} · filter ${state.filter} · search ${state.query || "(none)"}`);
    lines.push(`Outlook: ${B.direction || "—"} — ${B.confidence || ""}`);
    const fresh = d.freshness || {};
    lines.push(`Freshness: ${fresh.stale ? `STALE (${fresh.stale_reason})` : "fresh"} · generated ${fresh.generated_at || "—"}`);
    const srcs = Array.isArray(d.sources) ? d.sources : [];
    const failed = srcs.filter(x => !x.ok);
    if (srcs.length) {
      lines.push(`Sources: ${srcs.length - failed.length}/${srcs.length} answered` +
        (failed.length ? `; failed: ${failed.map(x => x.name).join(", ")}` : ""));
    }
    if ((d.errors || []).length) lines.push(`Provider notes: ${d.errors.join("; ")}`);
    const H = (((d.sections || {}).H || {}).data || {}).items || [];
    if (H.length) {
      lines.push("Top focus:");
      H.slice(0, 6).forEach(x => lines.push(`- ${x.title} (${x.source})`));
    }
    const G = (((d.sections || {}).G || {}).data || {}).rows || [];
    if (G.length) {
      lines.push("Flows: " + G.map(r => `${r.category} ${r.net_label}`).join(" · "));
    }
    return lines.join("\n");
  }

  return { build, context: hermesContext };
})();

window.RIONews = RIONews;
