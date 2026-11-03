/* RIO Live Ops — EVERY agent working, EVERY task measured.
 *
 * Data: GET /api/rio/ops (fresh on every call — no cache, no simulation).
 *   • agent registry status + current task (orchestrator registry)
 *   • plan.* / task.* events from the orchestrator journal, with real
 *     start/end timestamps → durations, step counts, retries
 *   • autonomy TaskStore missions + run history
 *   • in-flight agent runs in this process
 *
 * Layout:
 *   KPI strip  → agent/task counts, success rate, avg + p95 duration
 *   Theatre    → radial map of every agent; live agents pulse + beam to core
 *   Inspector  → the selected agent: status, current task, last executions
 *   Tasks      → measured runs with duration bars
 *   Missions   → scheduled autonomy tasks + run history
 *   Feed       → the journal, newest first
 *
 * Polling stops on teardown and while the tab is hidden. A run without a
 * terminal journal event is shown as running/stalled — never as success.
 */
"use strict";

const RIOOps = (() => {

  const POLL_MS = 3000;
  const STATUS_CLS = {
    "READY": "ready", "RUNNING": "running", "PAUSED": "paused",
    "ERROR": "error", "OFFLINE": "offline", "NOT IMPLEMENTED": "ni",
    "COMPLETED": "ready", "IDLE": "idle",
  };

  let data = null;
  let rootEl = null;
  let selected = null;
  let lastErr = null;
  let pollTimer = null;
  let tickTimer = null;
  let loading = false;

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path) {
    const r = await fetch(path, {
      headers: { "Authorization": `Bearer ${token()}`,
                 "Content-Type": "application/json" },
      cache: "no-store",
    });
    let j = null;
    try { j = await r.json(); } catch (_) { /* empty body */ }
    if (r.status === 401) throw Object.assign(new Error("pairing required"),
                                             { code: 401 });
    if (!r.ok) throw Object.assign(
      new Error((j && (j.detail || j.error)) || ("HTTP " + r.status)),
      { code: r.status, body: j });
    return j;
  }

  // ── formatting ───────────────────────────────────────────────────────
  function fmtDur(s) {
    if (s == null || !isFinite(s)) return "—";
    if (s < 1) return `${Math.round(s * 1000)}ms`;
    if (s < 90) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60), sec = Math.round(s % 60);
    if (m < 60) return `${m}m ${sec}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  function fmtAge(s) {
    if (s == null) return "—";
    if (s < 90) return `${Math.round(s)}s ago`;
    if (s < 5400) return `${Math.round(s / 60)}m ago`;
    if (s < 172800) return `${(s / 3600).toFixed(1)}h ago`;
    return `${(s / 86400).toFixed(1)}d ago`;
  }

  function fmtClock(ts) {
    return String(ts || "").replace("T", " ").replace("Z", "").slice(5, 19);
  }

  function statusCls(s) { return STATUS_CLS[s] || "offline"; }

  function shortId(id) {
    const s = String(id || "");
    return s.length > 30 ? s.slice(0, 14) + "…" + s.slice(-8) : s;
  }

  function ageOfRun(r) {
    if (r.started_epoch == null) return null;
    return Math.max(0, Date.now() / 1000 - r.started_epoch);
  }

  // ── shell ────────────────────────────────────────────────────────────
  function build() {
    const wrap = document.createElement("div");
    wrap.className = "ops-wrap";
    wrap.innerHTML = `
      <header class="ops-head">
        <div>
          <h1>Live Ops</h1>
          <p class="sub">Every agent, every task — measured from the live
             registry, the orchestrator journal and the autonomy engine.
             Nothing on this page is simulated.</p>
        </div>
        <div class="ops-head-right">
          <span class="ops-live" id="ops-live"><i class="dot dot-unknown"></i>
            <span id="ops-updated">connecting…</span></span>
          <button class="btn btn-mini" id="ops-refresh">${RIOIcons.get("refresh")} Refresh</button>
        </div>
      </header>

      <div class="ops-kpis" id="ops-kpis">
        <div class="ops-kpi"><span class="skel">loading…</span></div>
      </div>

      <div class="ops-grid">
        <section class="card ops-theatre-card">
          <div class="ops-card-head">
            <h2>${RIOIcons.get("activity")} Agent theatre</h2>
            <span class="ops-hint">click any agent to inspect it</span>
          </div>
          <div class="ops-stage" id="ops-stage">
            <svg class="ops-links" id="ops-links" viewBox="0 0 100 100"
                 preserveAspectRatio="none" aria-hidden="true"></svg>
            <div class="ops-core" id="ops-core">
              <div class="ops-core-ring"></div>
              <div class="ops-core-txt"><b>RIO</b><span id="ops-core-sub">core</span></div>
            </div>
            <div class="ops-nodes" id="ops-nodes"></div>
          </div>
        </section>

        <aside class="ops-side">
          <section class="card" id="ops-inspector">
            <h2>${RIOIcons.get("bot")} Agent inspector</h2>
            <div class="ops-inspector-body" id="ops-inspector-body">
              <div class="empty">Select an agent in the theatre.</div>
            </div>
          </section>

          <section class="card">
            <div class="ops-card-head">
              <h2>${RIOIcons.get("clock")} Task performance</h2>
              <span class="ops-hint" id="ops-runs-count"></span>
            </div>
            <div class="ops-runs" id="ops-runs">
              <div class="empty">no runs recorded yet</div>
            </div>
          </section>
        </aside>
      </div>

      <section class="card ops-desk-card">
        <div class="ops-card-head">
          <h2>${RIOIcons.get("trending-up")} Trading desk pipeline</h2>
          <span class="ops-hint" id="ops-desk-note"></span>
        </div>
        <div class="ops-desk-flow" id="ops-desk"></div>
      </section>

      <div class="ops-grid ops-grid-2">
        <section class="card">
          <div class="ops-card-head">
            <h2>${RIOIcons.get("target")} Missions</h2>
            <span class="ops-hint" id="ops-missions-count"></span>
          </div>
          <div class="ops-missions" id="ops-missions"></div>
          <h3 class="ops-h3">Scheduled run history</h3>
          <div class="ops-mhistory" id="ops-mhistory"></div>
        </section>

        <section class="card">
          <div class="ops-card-head">
            <h2>${RIOIcons.get("zap")} Live activity</h2>
            <span class="ops-hint">orchestrator journal · newest first</span>
          </div>
          <div class="ops-feed" id="ops-feed">
            <div class="empty">no journal events yet</div>
          </div>
        </section>
      </div>`;

    rootEl = wrap;
    queueMicrotask(() => {
      wrap.querySelector("#ops-refresh")?.addEventListener("click", () => load());
      load();
      startPolling();
    });
    return wrap;
  }

  // ── data ─────────────────────────────────────────────────────────────
  async function load() {
    if (loading) return;
    loading = true;
    try {
      data = await api("/api/rio/ops");
      lastErr = null;
    } catch (e) {
      lastErr = e.code === 401
        ? "Pairing required — open /login in this browser first."
        : `backend unavailable: ${e.message}`;
    }
    loading = false;
    paint();
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
      if (!rootEl || !rootEl.isConnected) { stopPolling(); return; }
      if (document.hidden) return;         // never poll a hidden tab
      load();
    }, POLL_MS);
    // one-second ticker only repaints elapsed times (no fetch)
    tickTimer = setInterval(() => {
      if (!rootEl || !rootEl.isConnected) return;
      paintElapsed();
    }, 1000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  // ── paint ────────────────────────────────────────────────────────────
  function paint() {
    if (!rootEl || !rootEl.isConnected) return;
    const live = rootEl.querySelector("#ops-live");
    const upd = rootEl.querySelector("#ops-updated");
    if (live && upd) {
      const dot = live.querySelector(".dot");
      if (lastErr) {
        dot.className = "dot dot-down";
        upd.textContent = lastErr;
        live.title = lastErr;
      } else {
        dot.className = "dot dot-up";
        const t = new Date();
        upd.textContent = `live · ${t.toLocaleTimeString()}`;
        live.title = data && data.journal ? `journal: ${data.journal}` : "";
      }
    }
    paintKpis();
    paintTheatre();
    paintInspector();
    paintRuns();
    paintDesk();
    paintMissions();
    paintFeed();
  }

  function paintKpis() {
    const el = rootEl.querySelector("#ops-kpis");
    if (!el) return;
    if (!data) {
      el.innerHTML = `<div class="ops-kpi ops-kpi-wide"><span class="dim">${
        esc(lastErr || "loading live state…")}</span></div>`;
      return;
    }
    const k = data.kpis || {};
    const rate = k.success_rate;
    const rateCls = rate == null ? "" : rate >= 90 ? "ok" : rate >= 70 ? "warn" : "err";
    const cells = [
      { label: "Agents live", value: k.agents_running || 0,
        sub: `${k.agents_ready || 0} ready · ${k.agents_total || 0} total`,
        cls: (k.agents_running ? "ok" : ""), icon: "bot" },
      { label: "In flight", value: k.in_flight || 0,
        sub: "agent runs now", cls: k.in_flight ? "ok" : "", icon: "activity" },
      { label: "Success rate", value: rate == null ? "—" : `${rate}%`,
        sub: `${k.runs_completed || 0} ok · ${k.runs_failed || 0} failed`,
        cls: rateCls, icon: "check" },
      { label: "Avg duration", value: fmtDur(k.avg_duration_s),
        sub: `p95 ${fmtDur(k.p95_duration_s)}`, cls: "", icon: "clock" },
      { label: "Missions", value: k.missions_total || 0,
        sub: k.missions_stopped ? "EMERGENCY STOP" :
             `${k.missions_enabled || 0} enabled`,
        cls: k.missions_stopped ? "err" : "", icon: "target" },
    ];
    el.innerHTML = cells.map(c => `
      <div class="ops-kpi ${c.cls ? "k-" + c.cls : ""}">
        <div class="ops-kpi-top"><span class="ops-kpi-ico">${RIOIcons.get(c.icon)}</span>
          <span class="ops-kpi-label">${esc(c.label)}</span></div>
        <div class="ops-kpi-val">${esc(String(c.value))}</div>
        <div class="ops-kpi-sub">${esc(c.sub)}</div>
      </div>`).join("");
  }

  // ── theatre ──────────────────────────────────────────────────────────
  function paintTheatre() {
    const nodesEl = rootEl.querySelector("#ops-nodes");
    const linksEl = rootEl.querySelector("#ops-links");
    const coreSub = rootEl.querySelector("#ops-core-sub");
    const core = rootEl.querySelector("#ops-core");
    if (!nodesEl || !linksEl) return;
    if (!data) { nodesEl.innerHTML = ""; linksEl.innerHTML = ""; return; }

    const agents = data.agents || [];
    const n = agents.length;
    const running = agents.filter(a => a.status === "RUNNING" ||
      (a.current_task && a.status !== "OFFLINE")).length;
    if (coreSub) coreSub.textContent = running ? `${running} active` : "standing by";
    core?.classList.toggle("is-busy", running > 0);

    const positions = [];
    for (let i = 0; i < n; i++) {
      const angle = (i / Math.max(1, n)) * Math.PI * 2 - Math.PI / 2;
      positions.push({
        left: 50 + 41 * Math.cos(angle),
        top: 50 + 39 * Math.sin(angle),
      });
    }

    linksEl.innerHTML = agents.map((a, i) => {
      const p = positions[i];
      const live = a.status === "RUNNING" || (a.current_task && a.status !== "OFFLINE");
      return `<line x1="50" y1="50" x2="${p.left.toFixed(2)}" y2="${p.top.toFixed(2)}"
        class="ops-link${live ? " on" : ""}"/>`;
    }).join("");

    nodesEl.innerHTML = "";
    agents.forEach((a, i) => {
      const p = positions[i];
      const live = a.status === "RUNNING" ||
        (a.current_task && a.status !== "OFFLINE");
      const b = document.createElement("button");
      b.className = `ops-node st-${statusCls(a.status)}` +
        (selected === a.key ? " sel" : "") + (live ? " live" : "");
      b.style.left = `${p.left.toFixed(2)}%`;
      b.style.top = `${p.top.toFixed(2)}%`;
      b.title = `${a.name} — ${a.status}`;
      b.innerHTML = `
        <span class="ops-node-dot"></span>
        <span class="ops-node-name">${esc(a.name)}</span>
        <span class="ops-node-meta">${
          live && a.current_task
            ? `<i data-elapsed-for="${esc(a.registry || a.key)}">0s</i>`
            : esc(a.status)}</span>`;
      b.addEventListener("click", () => {
        selected = selected === a.key ? null : a.key;
        paintTheatre();
        paintInspector();
        rootEl.querySelector("#ops-inspector")
          ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
      nodesEl.append(b);
    });
    paintElapsed();
  }

  function paintElapsed() {
    if (!data || !rootEl) return;
    rootEl.querySelectorAll("[data-elapsed-for]").forEach(el => {
      const key = el.getAttribute("data-elapsed-for");
      const a = (data.agents || []).find(x => (x.registry || x.key) === key);
      const cur = a && a.current_task;
      if (!cur || !cur.started) { el.textContent = "…"; return; }
      el.textContent = fmtDur(Math.max(0, Date.now() / 1000 - cur.started));
    });
    const live = rootEl.querySelector("#ops-insp-elapsed");
    if (live && selected) {
      const a = (data.agents || []).find(x => x.key === selected);
      const cur = a && a.current_task;
      if (cur && cur.started) {
        live.textContent = fmtDur(Math.max(0, Date.now() / 1000 - cur.started));
      }
    }
  }

  // ── inspector ────────────────────────────────────────────────────────
  function paintInspector() {
    const el = rootEl.querySelector("#ops-inspector-body");
    if (!el) return;
    const a = selected && (data && (data.agents || []).find(x => x.key === selected));
    if (!a) {
      el.innerHTML = `<div class="empty">Select an agent in the theatre to see
        its state, the task it is running and its last executions.</div>`;
      return;
    }
    const cur = a.current_task;
    const stats = a.stats || { ok: 0, fail: 0, other: 0 };
    const hist = (a.history || []).length
      ? a.history.map(h => `
          <li>
            <span class="ops-hist-ts">${esc(fmtClock(h.ts))}</span>
            <span class="ops-chip sc-${h.status === "completed" ? "ok"
              : h.status === "failed" ? "err" : "idle"}">${esc(h.status)}</span>
            <span class="ops-hist-txt">${esc((h.detail || h.task_id || "").slice(0, 110))}</span>
          </li>`).join("")
      : `<li class="dim">no executions recorded yet</li>`;

    el.innerHTML = `
      <div class="ops-insp-head">
        <div>
          <div class="ops-insp-name">${esc(a.name)}</div>
          <div class="ops-insp-reg">${esc(a.registry || "—")}</div>
        </div>
        <span class="ops-chip sc-${statusCls(a.status)}">${esc(a.status)}</span>
      </div>
      <p class="ops-insp-desc">${esc(a.description || "—")}</p>
      <div class="ops-insp-facts">
        <div><span>Model</span><b>${esc(a.model || "—")}</b></div>
        <div><span>Implementation</span><b>${esc(a.impl || "—")}</b></div>
        <div><span>Current task</span><b>${cur ? esc(shortId(cur.task_id)) : "idle"}</b></div>
        <div><span>Elapsed</span><b id="ops-insp-elapsed">—</b></div>
        <div><span>Success / failure</span><b><i class="ok-txt">✓ ${stats.ok}</i>
          · <i class="err-txt">✗ ${stats.fail}</i></b></div>
      </div>
      ${a.status_detail ? `<div class="ops-insp-note">${esc(a.status_detail)}</div>` : ""}
      <div class="ops-insp-links">
        <a class="wf-link" href="/agents/${encodeURIComponent(a.key)}">pipeline →</a>
        <a class="wf-link" href="/agents">agent center →</a>
      </div>
      <h3 class="ops-h3">Last executions</h3>
      <ul class="ops-hist">${hist}</ul>`;
    el.querySelectorAll("a[href^='/agents']").forEach(link => {
      link.addEventListener("click", (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        if (window.navigate) window.navigate(link.getAttribute("href"));
      });
    });
    paintElapsed();
  }

  // ── task performance ─────────────────────────────────────────────────
  function paintRuns() {
    const el = rootEl.querySelector("#ops-runs");
    const count = rootEl.querySelector("#ops-runs-count");
    if (!el) return;
    if (!data) { el.innerHTML = `<div class="empty">loading…</div>`; return; }
    const runs = (data.runs || []).slice(0, 14);
    if (count) count.textContent = `${(data.runs || []).length} in journal window`;
    if (!runs.length) {
      el.innerHTML = `<div class="empty">No plan or task runs in the journal
        yet — start a mission or run an agent, and its measured timing appears
        here.</div>`;
      return;
    }
    const max = Math.max(...runs.map(r => r.duration_s || 0), 1);
    el.innerHTML = runs.map(r => {
      const live = r.status === "running";
      const dur = fmtDur(r.duration_s);
      const pct = Math.max(2, Math.round(((r.duration_s || 0) / max) * 100));
      const cls = r.status === "completed" ? "ok"
        : r.status === "failed" ? "err"
        : r.status === "running" ? "run" : "idle";
      const meta = r.kind === "plan"
        ? `${r.steps_done || 0}/${r.step_count || 0} steps` +
          (r.retries ? ` · ${r.retries} retr${r.retries === 1 ? "y" : "ies"}` : "") +
          (r.approval_pending ? " · waiting approval" : "")
        : `${r.agent || r.task_type || "routed task"}`;
      return `<div class="ops-run${live ? " live" : ""}">
        <div class="ops-run-line">
          <span class="ops-tag t-${esc(r.kind)}">${r.kind === "plan" ? "PLAN" : "TASK"}</span>
          <b class="ops-run-id" title="${esc(r.task_id)}">${esc(shortId(r.task_id))}</b>
          <span class="ops-chip sc-${cls}">${esc(r.status)}</span>
          <span class="ops-run-dur">${dur}</span>
        </div>
        <div class="ops-bar"><i class="${cls}" style="width:${pct}%"></i></div>
        <div class="ops-run-meta">
          <span>${esc(meta)}</span>
          <span>${esc(fmtAge(ageOfRun(r)))}</span>
        </div>
      </div>`;
    }).join("");
  }

  // ── desk pipeline ────────────────────────────────────────────────────
  function paintDesk() {
    const el = rootEl.querySelector("#ops-desk");
    const note = rootEl.querySelector("#ops-desk-note");
    if (!el) return;
    if (!data) { el.innerHTML = `<span class="dim">loading…</span>`; return; }
    const desk = data.desk || {};
    const agents = desk.agents || [];
    if (note) {
      note.textContent = desk.note ||
        (desk.status && desk.status !== "idle" ? `desk ${desk.status}` : "desk idle");
    }
    if (!agents.length) {
      el.innerHTML = `<div class="empty">Desk state unavailable.</div>`;
      return;
    }
    el.innerHTML = agents.map((a, i) => `
      ${i ? `<span class="ops-desk-arrow" aria-hidden="true">→</span>` : ""}
      <div class="ops-desk-node st-${statusCls(a.status)}">
        <span class="ops-desk-dot"></span><b>${esc(a.name)}</b>
        <small>${esc(a.role || "")}</small>
        <div class="ops-desk-stages">${Object.entries(a.stages || {}).map(([k, v]) => {
          const s = String(v || "").toLowerCase().replace(/[^a-z]/g, "");
          return `<span class="ops-desk-stage s-${s}">${esc(k.replace(/_/g, " "))}</span>`;
        }).join("")}</div>
      </div>`).join("");
  }

  // ── missions ─────────────────────────────────────────────────────────
  function paintMissions() {
    const box = rootEl.querySelector("#ops-missions");
    const histBox = rootEl.querySelector("#ops-mhistory");
    const count = rootEl.querySelector("#ops-missions-count");
    if (!box || !histBox) return;
    if (!data) {
      box.innerHTML = `<div class="empty">loading…</div>`;
      histBox.innerHTML = "";
      return;
    }
    const missions = data.missions || [];
    if (count) count.textContent = missions.length
      ? `${missions.length} scheduled` : "none scheduled";
    if (!missions.length) {
      box.innerHTML = `<div class="empty">No scheduled missions. Create one
        from Home → Start Mission, or with the autonomy API.</div>`;
    } else {
      box.innerHTML = `<table class="ops-table"><thead><tr>
          <th>Mission</th><th>State</th><th>Steps</th><th>Last run</th>
          <th>Result</th></tr></thead><tbody>` +
        missions.map(m => {
          const last = m.last_run
            ? fmtAge(Math.max(0, Date.now() / 1000 - m.last_run)) : "never";
          const ok = m.last_status === "ok";
          return `<tr>
            <td><b>${esc(m.name)}</b>
              <div class="ops-table-sub">${esc(m.kind || "task")}${
                m.agents && m.agents.length ? " · " + esc(m.agents.join(", ")) : ""}</div></td>
            <td><span class="ops-chip sc-${m.enabled ? "ok" : "idle"}">${
              m.enabled ? "enabled" : "paused"}</span></td>
            <td>${m.steps || 0}</td>
            <td>${esc(last)}</td>
            <td>${m.last_status
              ? `<span class="ops-chip sc-${ok ? "ok" : "err"}">${esc(m.last_status)}</span>` +
                (m.failures ? ` <span class="err-txt">×${m.failures}</span>` : "")
              : `<span class="dim">never run</span>`}</td>
          </tr>`;
        }).join("") + `</tbody></table>`;
    }

    const hist = (data.mission_history || []).slice(-10).reverse();
    if (!hist.length) {
      histBox.innerHTML = `<div class="empty">No scheduled runs recorded yet.</div>`;
    } else {
      histBox.innerHTML = hist.map(h => `
        <div class="ops-mh-row">
          <span class="ops-mh-ts">${esc(fmtClock(h.ts))}</span>
          <span class="ops-chip sc-${h.status === "ok" ? "ok" : "err"}">${esc(h.status || "—")}</span>
          <b>${esc(h.task || h.name || "—")}</b>
          <span class="ops-mh-note">${esc(String(h.note || "").slice(0, 90))}</span>
        </div>`).join("");
    }
  }

  // ── feed ─────────────────────────────────────────────────────────────
  function paintFeed() {
    const el = rootEl.querySelector("#ops-feed");
    if (!el) return;
    if (!data) { el.innerHTML = `<div class="empty">loading…</div>`; return; }
    const feed = (data.feed || []).slice(0, 32);
    if (!feed.length) {
      el.innerHTML = `<div class="empty">No journal events yet — the
        orchestrator writes one for every routed task and plan step.</div>`;
      return;
    }
    el.innerHTML = feed.map(e => {
      const bad = /failed|error|fallback/.test(e.event) ||
        /failed|error/.test(e.status || "");
      const good = /completed|done|approved/.test(e.event) ||
        /completed/.test(e.status || "");
      const label = e.label ? `<b>${esc(e.label)}</b> · ` : "";
      return `<div class="ops-feed-row${bad ? " bad" : good ? " good" : ""}">
        <span class="ops-feed-age">${esc(fmtAge(e.age_s))}</span>
        <span class="ops-feed-ev">${esc(e.event)}</span>
        <span class="ops-feed-txt">${label}${esc((e.detail || "").slice(0, 150))}</span>
      </div>`;
    }).join("");
  }

  function teardown() {
    stopPolling();
    rootEl = null;
  }

  return { build, teardown, _state: () => ({ data, selected, lastErr }) };
})();

window.RIOOps = RIOOps;
