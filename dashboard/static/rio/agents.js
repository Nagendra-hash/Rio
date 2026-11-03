/* RIO Agent Center (Phase 4) — REAL registry, REAL history, REAL controls.
 *
 * Data:   GET  /api/rio/agents       → live orchestrator registry +
 *                                       orchestration journal + autonomy
 *                                       engine (no cache, no simulated state)
 * Run:    POST /api/rio/agents/run    → dispatches one REAL agent
 *                                       (agent.handle — the same call the
 *                                       orchestrator router makes); refused
 *                                       honestly for stubs (409) / absent (404)
 * Pause:  POST /api/autonomy/agent    → the backend's own per-agent
 *                                       enable/disable (persisted + audited)
 * Stop:   NOT RENDERED — the backend has no per-agent stop/cancel API.
 *
 * Statuses are read, never invented: READY / RUNNING / PAUSED / ERROR /
 * OFFLINE / NOT IMPLEMENTED. A stub never shows READY.
 */
"use strict";

const RIOAgents = (() => {

  // ── state ────────────────────────────────────────────────────────────
  let data = null;            // last GET /api/rio/agents payload
  let selected = null;        // selected card key
  let rootEl = null;
  let pollTimer = null;
  let draftPrompt = "";       // survives re-renders during polling
  let flash = null;           // {text, kind} shown above the grid
  let lastError = null;       // run/pause error detail

  const DEFAULT_PROMPT = "Status check from the RIO Agent Center.";

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
    try { j = await r.json(); } catch (_) { /* empty body */ }
    if (r.status === 401) throw Object.assign(new Error("pairing required"), { code: 401 });
    if (!r.ok) throw Object.assign(
      new Error((j && (j.detail || j.error)) || ("HTTP " + r.status)),
      { code: r.status, body: j });
    return j;
  }

  async function load() {
    try {
      data = await api("/api/rio/agents");
      lastError = null;
      return data;
    } catch (e) {
      lastError = e.code === 401
        ? "Pairing required — open /login in this browser first."
        : `backend unavailable: ${e.message}`;
      return null;
    }
  }

  const byKey = k => (data && data.agents || []).find(a => a.key === k) || null;

  // ── shell ────────────────────────────────────────────────────────────
  function build() {
    const frag = document.createDocumentFragment();
    rootEl = document.createElement("div");
    rootEl.className = "ac-wrap";
    rootEl.innerHTML = `
      <h1>Agent Center</h1>
      <p class="sub">Live view of the orchestrator registry, execution history
         and autonomy controls. Statuses are measured — an agent that cannot
         execute never shows READY.</p>
      <div class="ac-statusbar" id="ac-statusbar"></div>
      <div class="ac-flash" id="ac-flash" hidden></div>
      <div class="ac-grid" id="ac-grid"></div>
      <div class="ac-detail" id="ac-detail"></div>`;
    frag.append(rootEl);
    load().then(() => paint());
    return frag;
  }

  function paint() {
    if (!rootEl || !document.body.contains(rootEl)) return;
    paintStatusbar();
    paintGrid();
    paintDetail();
    const fb = rootEl.querySelector("#ac-flash");
    if (fb) {
      if (lastError || flash) {
        fb.hidden = false;
        fb.className = "ac-flash fb-" + (lastError ? "err" : (flash && flash.kind) || "ok");
        fb.textContent = lastError || (flash && flash.text) || "";
      } else fb.hidden = true;
    }
  }

  function paintStatusbar() {
    const el = rootEl.querySelector("#ac-statusbar");
    if (!el) return;
    if (!data) { el.innerHTML = `<span class="dim">loading registry…</span>`; return; }
    const orch = data.orchestrator || {};
    const aut = data.autonomy || {};
    const bits = [];
    bits.push(`<span class="chip">orchestrator <b>${esc(orch.mode || "?")}</b>` +
      `<span class="chip-state">${orch.available ? orch.registered_count + " registered" : "unavailable"}</span></span>`);
    if (aut.available) {
      bits.push(`<span class="chip">autonomy <b>${aut.emergency_stop ? "STOPPED" : "running"}</b>` +
        `<span class="chip-state">${(aut.paused_agents || []).length} paused</span></span>`);
      if (aut.emergency_stop)
        bits.push(`<span class="chip" style="border-color:rgba(244,63,94,.5);color:var(--err)">emergency stop active</span>`);
    } else {
      bits.push(`<span class="chip">autonomy <b>unavailable</b></span>`);
    }
    bits.push(`<button class="btn btn-mini" id="ac-refresh">↻ Refresh</button>`);
    el.innerHTML = bits.join("");
    el.querySelector("#ac-refresh")?.addEventListener("click", () =>
      load().then(() => paint()));
  }

  const CHIP = {
    "READY": "ac-ready", "RUNNING": "ac-running", "PAUSED": "ac-paused",
    "ERROR": "ac-error", "OFFLINE": "ac-offline",
    "NOT IMPLEMENTED": "ac-ni",
  };

  function ageStr(s) {
    if (s == null) return "—";
    if (s < 90) return Math.round(s) + "s ago";
    if (s < 5400) return Math.round(s / 60) + "m ago";
    return (s / 3600).toFixed(1) + "h ago";
  }

  function paintGrid() {
    const el = rootEl.querySelector("#ac-grid");
    if (!el) return;
    if (!data) {
      el.innerHTML = `<div class="empty">registry unavailable${lastError ? " — " + esc(lastError) : ""}</div>`;
      return;
    }
    el.replaceChildren(...data.agents.map(card));
  }

  // ── one agent card ─────────────────────────────────────────────────────
  function card(a) {
    const el = document.createElement("div");
    el.className = "ac-card" + (selected === a.key ? " sel" : "");
    el.dataset.key = a.key;
    const chipCls = CHIP[a.status] || "ac-offline";
    const last = a.last_execution;
    const stats = a.stats || { ok: 0, fail: 0, other: 0 };
    const cur = a.current_task;
    el.innerHTML = `
      <div class="ac-card-head">
        <span class="ac-name">${esc(a.name)}</span>
        <span class="ac-chip ${chipCls}">${esc(a.status)}</span>
      </div>
      <div class="ac-desc">${esc(a.description || "—")}</div>
      <div class="ac-fields">
        <div class="ac-f"><span>Model</span><b>${a.model ? esc(a.model) : "—"}</b></div>
        <div class="ac-f"><span>Current task</span><b>${cur ? esc(cur.task_id) : "— idle —"}</b></div>
        <div class="ac-f"><span>Last execution</span><b>${last
          ? esc(ageStr(last.age_s)) + " · " + esc(last.status)
          : "never"}</b></div>
        <div class="ac-f"><span>Success / failure</span><b>` +
          `<i class="ok-txt">✓ ${stats.ok}</i> · <i class="err-txt">✗ ${stats.fail}</i>` +
          `</b></div>
      </div>`;
    el.addEventListener("click", () => {
      selected = (selected === a.key) ? null : a.key;
      draftPrompt = "";
      paint();
      if (selected)
        rootEl?.querySelector("#ac-detail")
          ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    return el;
  }

  // ── detail panel ──────────────────────────────────────────────────────
  function paintDetail() {
    const el = rootEl.querySelector("#ac-detail");
    if (!el) return;
    const a = selected ? byKey(selected) : null;
    if (!data || !a) {
      el.className = "ac-detail";
      el.innerHTML = `<div class="empty">Select an agent card for its full
        detail: execution history, tools, configuration and controls.</div>`;
      return;
    }
    el.className = "ac-detail open";
    const cfg = a.config || {};
    const acts = a.actions || {};
    const chipCls = CHIP[a.status] || "ac-offline";
    const cur = a.current_task;

    const hist = (a.history || []).length
      ? (a.history || []).map(h => `
          <div class="ac-h-row">
            <span class="ac-h-ts">${esc((h.ts || "").replace("T", " ").replace("Z", ""))}</span>
            <span class="ac-h-st ${h.status === "completed" ? "ok-txt" :
                                  h.status === "failed" ? "err-txt" : ""}">${esc(h.status)}</span>
            <span class="ac-h-txt">${esc(h.detail || h.task_id || "")}</span>
          </div>`).join("")
      : `<div class="dim">no executions recorded in the journal yet</div>`;

    const tools = (a.tools || []).length
      ? a.tools.map(t => `<span class="ac-tool">${esc(t)}</span>`).join("")
      : `<span class="dim">— none wired —</span>`;

    const gate = cfg.env_gate
      ? `<div class="ac-f"><span>Env gate</span><b>${esc(cfg.env_gate)} = ${esc(cfg.env_gate_value)}` +
        `${cfg.env_gate_wired ? "" : " (documented, not wired — audit B2)"}</b></div>`
      : "";
    const approval = cfg.require_approval == null ? "" :
      `<div class="ac-f"><span>Approval</span><b>${cfg.require_approval
        ? "required by placeholder" : "not required"}</b></div>`;
    const aut = (data.autonomy || {}).available
      ? `<div class="ac-f"><span>Autonomy</span><b>${a.paused
          ? "PAUSED (disabled in engine)" : "enabled"}${
          (data.autonomy.levels || {})[a.registry] != null
            ? " · level " + data.autonomy.levels[a.registry] : ""}</b></div>`
      : "";

    const controls = [];
    if (acts.run) {
      controls.push(`<button class="btn btn-primary" id="ac-run"${
        a.status === "RUNNING" ? " disabled" : ""}>▶ RUN</button>`);
    }
    if (acts.pause) {
      controls.push(a.paused
        ? `<button class="btn" id="ac-pause">▶ Resume</button>`
        : `<button class="btn" id="ac-pause">⏸ Pause</button>`);
    }
    if (!acts.run)
      controls.push(`<span class="dim ac-noctl">RUN unavailable — ${
        esc(a.status === "NOT IMPLEMENTED"
          ? "not implemented in the backend (no such agent in the registry)"
          : a.status_detail || "registered placeholder")}</span>`);

    el.innerHTML = `
      <div class="ac-detail-head">
        <h2>${esc(a.name)}</h2>
        <span class="ac-chip ${chipCls}">${esc(a.status)}</span>
        <button class="btn btn-mini" id="ac-close">✕ Close</button>
      </div>
      <div class="ac-status-detail">${esc(a.status_detail || "")}</div>
      <div class="ac-detail-grid">
        <section class="ac-sec">
          <h3>Current task</h3>
          <div class="ac-f"><span>State</span><b>${cur ? "RUNNING" : "idle"}</b></div>
          ${cur ? `<div class="ac-f"><span>Task</span><b>${esc(cur.task_id)}</b></div>
                   <div class="ac-f"><span>Prompt</span><b>${esc(cur.prompt)}</b></div>
                   <div class="ac-f"><span>Elapsed</span><b id="ac-elapsed">${
                     (Date.now() / 1000 - (cur.started || 0)).toFixed(1)}s</b></div>`
                 : `<div class="dim">nothing running</div>`}
        </section>
        <section class="ac-sec">
          <h3>Model</h3>
          <div>${a.model ? `<b>${esc(a.model)}</b>` : `<b>— no model bound —</b>`}</div>
          <div class="dim">${esc(a.model_note || "")}</div>
        </section>
        <section class="ac-sec ac-span2">
          <h3>Execution history</h3>
          <div class="ac-hist">${hist}</div>
        </section>
        <section class="ac-sec ac-span2">
          <h3>Tools</h3><div class="ac-tools">${tools}</div>
        </section>
        <section class="ac-sec ac-span2">
          <h3>Configuration</h3>
          <div class="ac-f"><span>Registry name</span><b>${esc(a.registry || "—")}</b></div>
          <div class="ac-f"><span>Registered class</span><b>${esc(cfg.class || "—")} (${
            a.impl === "real" ? "real implementation" :
            a.impl === "stub" ? "placeholder stub" : "absent"})</b></div>
          <div class="ac-f"><span>Task types</span><b>${
            (cfg.task_types || []).join(", ") || "—"}</b></div>
          ${gate}${approval}${aut}
          <div class="ac-f"><span>Orchestrator</span><b>mode ${
            esc((data.orchestrator || {}).mode || "?")}</b></div>
        </section>
      </div>
      ${acts.run ? `
      <div class="ac-runbar">
        <textarea id="ac-prompt" rows="2"
          placeholder="Prompt for this agent (defaults to a harmless status check)…"
          aria-label="Run prompt">${esc(draftPrompt || DEFAULT_PROMPT)}</textarea>
        <div class="ac-runbtns">${controls.join("")}</div>
      </div>` : `<div class="ac-runbar">${controls.join("")}</div>`}
      <div class="ac-stopnote">STOP / PAUSE availability: ${
        acts.stop ? "" : esc(acts.stop_note || "")}${
        acts.pause ? " · PAUSE uses the autonomy engine's per-agent disable " +
                     "(persisted + audited)." : " · PAUSE unavailable " +
                     "(autonomy engine not running)."}</div>`;

    el.querySelector("#ac-close")?.addEventListener("click", () => {
      selected = null; paint();
    });
    const ta0 = el.querySelector("#ac-prompt");
    if (ta0) ta0.addEventListener("input", () => { draftPrompt = ta0.value; });
    el.querySelector("#ac-run")?.addEventListener("click", () => {
      const ta = el.querySelector("#ac-prompt");
      run(a.registry, (ta && ta.value || "").trim());
    });
    el.querySelector("#ac-pause")?.addEventListener("click", () =>
      pause(a.registry, !a.paused));
  }

  // ── controls ───────────────────────────────────────────────────────────
  async function run(name, prompt) {
    try {
      const res = await api("/api/rio/agents/run", {
        method: "POST",
        body: JSON.stringify({ name, prompt: prompt || DEFAULT_PROMPT }),
      });
      flash = { text: `RUN dispatched — ${res.task_id}`, kind: "ok" };
      lastError = null;
      await load(); paint();
      pollUntilSettled(name, res.task_id);
    } catch (e) {
      flash = null;
      lastError = e.code === 404
        ? "NOT IMPLEMENTED — this agent does not exist in the backend."
        : e.code === 409
          ? (e.body && e.body.error === "already_running"
             ? `Already running (${e.body.task_id}) — watching it.`
             : `RUN refused: ${e.message}`)
          : `RUN failed: ${e.message}`;
      paint();
      if (e.body && e.body.task_id) pollUntilSettled(name, e.body.task_id);
    }
  }

  async function pause(name, paused) {
    try {
      await api("/api/autonomy/agent", {
        method: "POST",
        body: JSON.stringify({ agent: name, enabled: !paused }),
      });
      flash = { text: paused ? `${name} paused in the autonomy engine.`
                             : `${name} resumed.`, kind: "ok" };
      lastError = null;
    } catch (e) {
      lastError = `pause failed: ${e.message}`;
      flash = null;
    }
    await load(); paint();
  }

  // poll GET /api/rio/agents until this agent leaves RUNNING (or timeout)
  function pollUntilSettled(name, taskId) {
    if (pollTimer) clearTimeout(pollTimer);
    const t0 = Date.now();
    const tick = async () => {
      await load();
      const a = (data && data.agents || []).find(x => x.registry === name);
      const running = a && a.status === "RUNNING" &&
        (!a.current_task || !taskId || a.current_task.task_id === taskId);
      paint();
      if (running && Date.now() - t0 < 90000) {
        pollTimer = setTimeout(tick, 400);
      } else {
        pollTimer = null;
        if (a && a.last_execution && a.last_execution.task_id === taskId)
          flash = { text: `run ${a.last_execution.status}: ${
            (a.last_execution.detail || "").slice(0, 140)}`,
            kind: a.last_execution.status === "failed" ? "warn" : "ok" };
        paint();
      }
    };
    pollTimer = setTimeout(tick, 250);
  }

  function teardown() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  return { build, teardown, _state: () => ({ data, selected, lastError }) };
})();
window.RIOAgents = RIOAgents;
