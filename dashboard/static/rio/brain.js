/* RIO Brain + Model Router (Phase 5) — READ-ONLY view of the EXISTING
 * integrations. Nothing here routes: planning goes through
 * POST /api/rio/brain/route (real IntentClassifier + router._plan) and live
 * calls go through POST /api/rio/brain/probe (real ModelRouter.route).
 * API keys are never rendered — the backend only reports configuration
 * presence and probe results.
 */
"use strict";

const RIOBrain = (() => {

  let data = null;
  let rootEl = null;
  let routeResult = null;
  let probeResult = null;
  let lastError = null;
  let flash = null;
  let probing = false;

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Authorization": `Bearer ${token()}`,
                 "Content-Type": "application/json" },
      cache: "no-store",
    }, opts));
    let j = null;
    try { j = await r.json(); } catch (_) { /* empty */ }
    if (r.status === 401) throw Object.assign(new Error("pairing required"), { code: 401 });
    if (!r.ok) throw Object.assign(
      new Error((j && (j.detail || j.error || j.explanation)) || ("HTTP " + r.status)),
      { code: r.status, body: j });
    return j;
  }

  async function load() {
    try {
      data = await api("/api/rio/brain");
      lastError = null;
    } catch (e) {
      lastError = e.code === 401
        ? "Pairing required — open /login in this browser first."
        : `backend unavailable: ${e.message}`;
    }
    return data;
  }

  const CHIP = {
    "up": "bp-up", "configured": "bp-conf", "cooling": "bp-cool",
    "down": "bp-down", "unconfigured": "bp-unconf",
  };

  // ── shell ────────────────────────────────────────────────────────────
  function build() {
    const frag = document.createDocumentFragment();
    rootEl = document.createElement("div");
    rootEl.className = "bp-wrap";
    rootEl.innerHTML = `
      <h1>Brain</h1>
      <p class="sub">The existing model integrations — providers, health,
        usage and the live routing table computed by the real
        CapabilityRegistry. No keys are ever read into this page.</p>
      <div class="bp-statusbar" id="bp-statusbar"></div>
      <div class="bp-flash" id="bp-flash" hidden></div>

      <h2 class="bp-h2">Pipeline</h2>
      <ol class="bp-pipe" id="bp-pipe"></ol>

      <h2 class="bp-h2">Providers</h2>
      <div class="bp-grid" id="bp-grid"></div>

      <h2 class="bp-h2">Routing table <span class="bp-note">(computed live — cheapest capable rung first)</span></h2>
      <div class="bp-routing" id="bp-routing"></div>

      <h2 class="bp-h2">Example routes <span class="bp-note">(computed live, never hard-coded)</span></h2>
      <div class="bp-examples" id="bp-examples"></div>

      <h2 class="bp-h2">Route a request <span class="bp-note">(planning only — no model is called)</span></h2>
      <div class="bp-sim">
        <textarea id="bp-text" rows="2"
          placeholder="e.g. write a python function to parse CSV files"
          aria-label="Request text"></textarea>
        <button class="btn btn-primary" id="bp-route-btn">Route</button>
      </div>
      <div id="bp-route-result"></div>

      <h2 class="bp-h2">Live probe <span class="bp-note">(makes ONE real model call through the router)</span></h2>
      <div class="bp-sim">
        <input id="bp-probe-prompt" type="text" value="Reply with the word OK."
               aria-label="Probe prompt">
        <button class="btn" id="bp-probe-btn">Run live probe</button>
      </div>
      <div id="bp-probe-result"></div>

      <h2 class="bp-h2">Recent routed calls <span class="bp-note">(from the usage journals — real fallback trails)</span></h2>
      <div id="bp-recent"></div>`;
    frag.append(rootEl);

    rootEl.querySelector("#bp-route-btn").addEventListener("click", doRoute);
    rootEl.querySelector("#bp-probe-btn").addEventListener("click", doProbe);
    load().then(() => paint());
    return frag;
  }

  function paint() {
    if (!rootEl || !document.body.contains(rootEl)) return;
    paintStatusbar();
    paintPipe();
    paintProviders();
    paintRouting();
    paintExamples();
    paintRecent();
    paintRouteResult();
    paintProbeResult();
    const fb = rootEl.querySelector("#bp-flash");
    if (fb) {
      if (lastError || flash) {
        fb.hidden = false;
        fb.className = "bp-flash fb-" + (lastError ? "err" : (flash && flash.kind) || "ok");
        fb.textContent = lastError || (flash && flash.text) || "";
      } else fb.hidden = true;
    }
  }

  function paintStatusbar() {
    const el = rootEl.querySelector("#bp-statusbar");
    if (!el) return;
    if (!data) { el.innerHTML = `<span class="dim">loading model-router status…</span>`; return; }
    const cfg = data.config || {};
    el.innerHTML = [
      `<span class="chip">mode <b>${esc(data.mode || "?")}</b><span class="chip-state">config: router_mode</span></span>`,
      `<span class="chip">keys <b>${cfg.gemini_keys || 0} gemini · ${cfg.openrouter_keys || 0} openrouter · ${cfg.freellm_key ? "1" : "0"} freellm</b><span class="chip-state">presence only — values never leave the config file</span></span>`,
      `<button class="btn btn-mini" id="bp-refresh">↻ Refresh</button>`,
    ].join("");
    el.querySelector("#bp-refresh")?.addEventListener("click", () =>
      load().then(() => paint()));
  }

  function paintPipe() {
    const el = rootEl.querySelector("#bp-pipe");
    if (!el) return;
    const stages = (data && data.pipeline) || [];
    el.replaceChildren(...stages.map((s, i) => {
      const li = document.createElement("li");
      li.className = "bp-stage";
      li.innerHTML = `<b>${esc(s.stage)}</b><span>${esc(s.impl)}</span>` +
        (i < stages.length - 1 ? `<i class="bp-arrow">↓</i>` : "");
      return li;
    }));
  }

  function paintProviders() {
    const el = rootEl.querySelector("#bp-grid");
    if (!el) return;
    if (!data) { el.innerHTML = `<div class="empty">provider status unavailable</div>`; return; }
    el.replaceChildren(...data.providers.map(p => {
      const c = document.createElement("div");
      c.className = "bp-card";
      c.dataset.key = p.key;
      const chip = CHIP[p.status] || "bp-unconf";
      const u = p.usage || {};
      const lat = p.probe_latency_ms != null
        ? `${p.probe_latency_ms} ms (probe)`
        : (p.latency_s != null ? `${p.latency_s} s (avg call)` : "—");
      c.innerHTML = `
        <div class="bp-card-head">
          <span class="bp-name">${esc(p.name)}</span>
          <span class="bp-chip ${chip}">${esc(p.status)}</span>
        </div>
        <div class="bp-model">${esc(p.model || "— no model reported —")}</div>
        <div class="bp-fields">
          <div class="bp-f"><span>Purpose</span><b>${esc(p.purpose || "—")}</b></div>
          <div class="bp-f"><span>Latency</span><b>${esc(lat)}</b></div>
          <div class="bp-f"><span>Probe</span><b>${esc(p.detail || "—")}</b></div>
          <div class="bp-f"><span>Last error</span><b class="${p.last_error ? "err-txt" : ""}">${
            esc(p.last_error || "none recorded")}</b></div>
          <div class="bp-f"><span>Usage</span><b>${u.calls || 0} calls · ${
            u.ok || 0} ✓ / ${u.fail || 0} ✗${
            u.avg_latency_s != null ? ` · ${u.avg_latency_s}s avg` : ""}</b></div>
          <div class="bp-f"><span>Router rung</span><b>${
            p.router_rung ? "yes" : "no — " + esc(p.purpose_note || "house runtime")}</b></div>
        </div>`;
      return c;
    }));
  }

  const STATE_CLS = { "available": "bp-av", "cooling": "bp-cool",
                      "unconfigured": "bp-unconf", "unavailable": "bp-down" };

  function paintRouting() {
    const el = rootEl.querySelector("#bp-routing");
    if (!el) return;
    if (!data || !(data.routing || []).length) {
      el.innerHTML = `<div class="empty">routing table unavailable${
        data && data.routing_error ? " — " + esc(data.routing_error) : ""}</div>`;
      return;
    }
    el.replaceChildren(...data.routing.map(r => {
      const row = document.createElement("div");
      row.className = "bp-rrow";
      row.innerHTML = `<span class="bp-rlabel">${esc(r.label)}</span>
        <span class="bp-chain">${r.chain.map((s, i) =>
          `<span class="bp-rung ${STATE_CLS[s.state] || ""}"
                 title="${esc(s.model_id + (s.why ? " — " + s.why : ""))}">${
            i + 1}. ${esc(s.name)}</span>` +
          (i < r.chain.length - 1 ? `<span class="bp-to">→</span>` : "")
        ).join("")}</span>`;
      return row;
    }));
  }

  function paintExamples() {
    const el = rootEl.querySelector("#bp-examples");
    if (!el) return;
    const ex = (data && data.examples) || [];
    el.replaceChildren(...ex.map(e => {
      const row = document.createElement("div");
      row.className = "bp-exrow";
      row.innerHTML = `<b>${esc(e.label)}</b><span>${esc(e.route || "—")}</span>`;
      return row;
    }));
  }

  // ── planning (pure, real backend) ───────────────────────────────────
  async function doRoute() {
    const text = (rootEl.querySelector("#bp-text").value || "").trim();
    if (!text) { flash = { text: "Type a request first.", kind: "warn" }; paint(); return; }
    try {
      routeResult = await api("/api/rio/brain/route",
        { method: "POST", body: JSON.stringify({ text }) });
      lastError = null; flash = null;
    } catch (e) {
      routeResult = null;
      lastError = `route failed: ${e.message}`;
    }
    paint();
  }

  function paintRouteResult() {
    const el = rootEl.querySelector("#bp-route-result");
    if (!el) return;
    if (!routeResult) { el.innerHTML = ""; return; }
    if (!routeResult.ok) {
      el.innerHTML = `<div class="bp-result bp-res-err">${
        esc(routeResult.error || "routing failed")}</div>`;
      return;
    }
    const intent = routeResult.intent;
    el.innerHTML = `
      <div class="bp-result">
        <div class="bp-res-line">${intent
          ? `<span>intent: <b>${esc(intent.task_type)}</b> (confidence ${
              intent.confidence}) — ${esc(intent.reason)}</span>`
          : `<span>task forced: <b>${esc(routeResult.model_task)}</b></span>`}
          <span>model task: <b>${esc(routeResult.model_task)}</b></span>
          <span>mode: <b>${esc(routeResult.mode)}</b></span>
        </div>
        <div class="bp-chain">${routeResult.chain.map((s, i) =>
          `<span class="bp-rung ${s.selected ? "bp-sel" : ""} ${
            s.in_plan ? "bp-av" : "bp-down"}">${i + 1}. ${esc(s.name)}${
            s.selected ? " ✓" : ""}</span>` +
          (i < routeResult.chain.length - 1 ? `<span class="bp-to">→</span>` : "")
        ).join("")}</div>
        <div class="bp-res-why">${esc(routeResult.explanation)}</div>
        <div class="bp-res-why dim">${routeResult.chain.filter(s => s.reason)
          .map(s => `${esc(s.name)}: ${esc(s.reason)}`).join(" · ") || ""}</div>
      </div>`;
  }

  // ── live probe (ONE real call through ModelRouter.route) ───────────
  async function doProbe() {
    if (probing) return;
    probing = true;
    const btn = rootEl.querySelector("#bp-probe-btn");
    btn.disabled = true; btn.textContent = "Probing…";
    flash = { text: "Making one real model call through the router…", kind: "ok" };
    lastError = null;
    paint();
    try {
      const prompt = rootEl.querySelector("#bp-probe-prompt").value || "";
      probeResult = await api("/api/rio/brain/probe",
        { method: "POST", body: JSON.stringify({ prompt }) });
      flash = null;
    } catch (e) {
      probeResult = { ok: false, explanation: e.message,
                      http_code: e.code, body: e.body };
      flash = null;
      if (e.code !== 429) lastError = null;   // cooldown shown in the result box
    }
    probing = false;
    btn.disabled = false; btn.textContent = "Run live probe";
    paint();
  }

  function paintProbeResult() {
    const el = rootEl.querySelector("#bp-probe-result");
    if (!el) return;
    if (!probeResult) { el.innerHTML = ""; return; }
    if (probeResult.http_code === 429 || probeResult.error === "cooldown") {
      el.innerHTML = `<div class="bp-result bp-res-warn">${
        esc((probeResult.body && probeResult.body.detail) ||
            probeResult.explanation || "cooldown")}</div>`;
      return;
    }
    if (probeResult.ok) {
      el.innerHTML = `
        <div class="bp-result ${probeResult.fell_back ? "bp-res-warn" : "bp-res-ok"}">
          <div class="bp-res-line">
            <span>answered by: <b>${esc(probeResult.provider)}</b> · ${
              esc(probeResult.model)}</span>
            <span>latency: <b>${esc(probeResult.latency_s)}s</b></span>
            <span>fallback: <b>${probeResult.fell_back ? "yes" : "no (first choice)"}</b></span>
          </div>
          <div class="bp-res-why">${esc(probeResult.explanation || "")}</div>
          <div class="bp-res-text">${esc(probeResult.text || "")}</div>
          ${(probeResult.hops || []).length
            ? `<div class="bp-res-why dim">${probeResult.hops.map(h =>
                `${esc(h.provider)} → ${esc(h.status)}: ${esc(h.detail)}`
              ).join(" · ")}</div>` : ""}
        </div>`;
    } else {
      el.innerHTML = `
        <div class="bp-result bp-res-err">
          <div class="bp-res-line"><span>no provider answered</span></div>
          <div class="bp-res-why">${esc(probeResult.explanation ||
            probeResult.error || "unknown failure")}</div>
          ${(probeResult.hops || []).length
            ? `<div class="bp-res-why dim">${probeResult.hops.map(h =>
                `${esc(h.provider)} → ${esc(h.status)}: ${esc(h.detail)}`
              ).join(" · ")}</div>` : ""}
        </div>`;
    }
  }

  // ── recent routed calls ─────────────────────────────────────────────
  function paintRecent() {
    const el = rootEl.querySelector("#bp-recent");
    if (!el) return;
    const rows = (data && data.recent_calls) || [];
    if (!rows.length) {
      el.innerHTML = `<div class="empty">no routed calls recorded yet</div>`;
      return;
    }
    el.innerHTML = `<div class="bp-recent">` + rows.map(r => `
      <div class="bp-rc-row">
        <span class="bp-rc-ts">${esc(String(r.ts).replace("T", " ").slice(0, 19))}</span>
        <span class="bp-rc-prov ${r.ok ? "ok-txt" : "err-txt"}">${
          esc(r.provider || "—")}${r.ok ? " ✓" : " ✗"}</span>
        <span class="bp-rc-task">${esc(r.task || "—")}</span>
        <span class="bp-rc-lat">${r.latency_s != null ? esc(r.latency_s) + "s" : "—"}</span>
        <span class="bp-rc-note">${esc(r.error || (r.hops && r.hops.length
          ? "fell back: " + r.hops.map(h => h.provider).join(" → ")
          : ""))}</span>
      </div>`).join("") + `</div>`;
  }

  return { build, _state: () => ({ data, routeResult, probeResult, lastError }) };
})();
window.RIOBrain = RIOBrain;
