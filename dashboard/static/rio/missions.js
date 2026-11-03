/* RIO Mission Center (Phase 9) — REAL backend only.
 *
 * A mission = one scheduled autonomy task (multiple agent steps).
 * Statuses queued/running/paused/completed/failed come straight from
 * GET /api/rio/missions (derived from the live TaskStore + engine).
 * Start/Pause/Resume are shown ONLY when the backend advertises them in
 * `capabilities`; the engine has NO per-mission stop and NO mid-run pause —
 * the Stop button is therefore DISABLED and explains that the only stop
 * the backend supports is the GLOBAL emergency stop (POST /api/autonomy/stop),
 * which this page can trigger. Nothing is claimed to work that does not.
 * Polling: none — explicit Refresh + after each action.
 */
"use strict";

const RIOMissions = (() => {

  let data = null;      // GET /api/rio/missions
  let busy = false;
  let flash = null;

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>\"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function say(text, kind) { flash = text ? { text, kind: kind || "ok" } : null; }

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
      new Error((j && (j.error || j.detail)) || `HTTP ${r.status}`),
      { code: r.status, body: j });
    return j;
  }

  const post = (path, body) =>
    api(path, { method: "POST", body: JSON.stringify(body || {}) });

  async function refresh() {
    busy = true; paint();
    try { data = await api("/api/rio/missions"); say(null); }
    catch (e) { data = null; say(`missions: ${e.message}`, "err"); }
    busy = false; paint();
  }

  const CAP = () => (data && data.capabilities) || {};

  async function action(kind, name) {
    try {
      await post(`/api/rio/missions/${kind}`, { task: name });
      say(`${kind}: ${name}`, kind === "stop" ? "warn" : "ok");
    } catch (e) { say(`${kind} failed: ${e.message}`, "err"); }
    await refresh();
  }

  async function globalStop() {
    const ok = window.confirm(
      "GLOBAL EMERGENCY STOP?\n\nThe backend has no per-mission stop — this " +
      "trips the autonomy engine's emergency stop (POST /api/autonomy/stop), " +
      "halting every mission between steps until you resume. Continue?");
    if (!ok) return;
    try {
      await post("/api/autonomy/stop",
                 { reason: "manual emergency stop from Mission Center" });
      say("global emergency stop engaged — resume via /api/autonomy/resume", "warn");
    } catch (e) { say(`emergency stop failed: ${e.message}`, "err"); }
    await refresh();
  }

  async function resumeGlobal() {
    try {
      await post("/api/autonomy/resume", { by: "mission-center" });
      say("autonomy resumed", "ok");
    } catch (e) { say(`resume failed: ${e.message}`, "err"); }
    await refresh();
  }

  /* ── shell ─────────────────────────────────────────────────────────── */
  function build() {
    const root = document.createElement("div");
    root.className = "mi-wrap";
    root.innerHTML = `
      <h1>Mission Center</h1>
      <p class="sub">A mission is one autonomy task made of multiple agent
        steps. Statuses and controls below are driven entirely by the real
        backend: <b>Start / Pause / Resume</b> exist because the scheduler
        supports them; <b>Stop</b> is disabled because the engine has no
        per-mission cancel — only the global emergency stop, which this page
        exposes as its own button.</p>

      <div class="sy-bar">
        <button class="btn" id="mi-refresh">↻ Refresh</button>
        <span class="tr-pill" id="mi-estop">…</span>
        <span class="hm-checked" id="mi-running"></span>
      </div>
      <div class="mem-flash" id="mi-flash" hidden></div>
      <div class="tr-banner" id="mi-caps" hidden></div>

      <h2>Missions</h2>
      <div class="mi-list" id="mi-list"></div>

      <h2>Run history</h2>
      <div class="mi-history" id="mi-history"></div>`;

    const frag = document.createDocumentFragment();
    frag.append(root);
    queueMicrotask(() => {
      document.getElementById("mi-refresh")?.addEventListener("click", refresh);
      refresh();
    });
    return frag;
  }

  const STATUS_CLS = { queued: "", running: "ok", paused: "warn",
                       completed: "ok", failed: "warn" };

  function paint() {
    if (!document.querySelector(".mi-wrap")) return;
    const fb = document.getElementById("mi-flash");
    if (fb) {
      fb.hidden = !flash;
      if (flash) { fb.textContent = flash.text; fb.className = "mem-flash fb-" + flash.kind; }
    }
    paintStatus(); paintCaps(); paintList(); paintHistory();
  }

  function paintStatus() {
    const e = document.getElementById("mi-estop");
    const r = document.getElementById("mi-running");
    if (e) {
      if (!data) { e.textContent = "…"; e.className = "tr-pill"; }
      else if (data.emergency_stop && data.emergency_stop.stopped) {
        e.textContent = "⛔ EMERGENCY STOP";
        e.className = "tr-pill tr-pill-stop";
      } else { e.textContent = "● autonomy live"; e.className = "tr-pill tr-pill-run"; }
    }
    if (r) {
      const run = (data && data.running) || [];
      r.textContent = run.length
        ? `running: ${run.join(", ")}` : (data ? "nothing running" : "");
    }
  }

  function paintCaps() {
    const b = document.getElementById("mi-caps");
    if (!b) return;
    const notes = (CAP().notes || []);
    const estop = data && data.emergency_stop && data.emergency_stop.stopped;
    b.hidden = !notes.length && !estop;
    b.className = "tr-banner " + (estop ? "banner-warn" : "banner-info");
    b.innerHTML = [
      estop ? `<b>⛔ Emergency stop active:</b> ${esc(data.emergency_stop.reason || "stopped")}
               — resume with the button below.` : "",
      CAP().stop === false
        ? `<b>Per-mission Stop unavailable:</b> the engine has no cancel for a
           single mission, so the Stop control is disabled (never faked).
           Use <b>GLOBAL EMERGENCY STOP</b> instead.` : "",
      ...notes.map(n => `<div>${esc(n)}</div>`),
    ].filter(Boolean).join("<br>");
  }

  function paintList() {
    const box = document.getElementById("mi-list");
    if (!box) return;
    if (busy && !data) { box.innerHTML = `<div class="empty">Loading…</div>`; return; }
    if (!data) {
      box.innerHTML = `<div class="empty warn">Mission backend unavailable —
        ${esc(flash ? flash.text : "autonomy engine not reachable")}. Nothing
        is simulated here.</div>`;
      return;
    }
    const rows = data.missions || [];
    if (!rows.length) {
      box.innerHTML = `<div class="empty">No missions scheduled yet — tasks
        added by the scheduler appear here with live statuses.</div>`;
      return;
    }
    const canStart = CAP().start !== false;
    const canPause = CAP().pause !== false;
    const canResume = CAP().resume !== false;
    const stopped = data.emergency_stop && data.emergency_stop.stopped;
    const btn = (id, label, name, disabled, cls) =>
      `<button class="btn ${cls || ""}" data-act="${id}" data-name="${esc(name)}"${disabled ? " disabled" : ""}>${label}</button>`;
    box.innerHTML = `<table class="sy-table mi-table"><thead><tr>
        <th>Mission</th><th>Status</th><th>Steps</th><th>Level</th>
        <th>Last run</th><th>Last result</th><th>Controls</th>
      </tr></thead><tbody>` +
      rows.map(m => {
        const st = m.status || "queued";
        const running = st === "running";
        const paused = st === "paused";
        return `<tr>
          <td><b>${esc(m.name)}</b><br>
            <span class="hm-checked">${esc((m.kind || "task") +
              (m.agents && m.agents.length ? " · " + m.agents.join(", ") : ""))}</span></td>
          <td><span class="tr-mini ${STATUS_CLS[st] || ""}">${esc(st)}</span></td>
          <td>${m.step_count ?? (m.steps || []).length}</td>
          <td>${esc(m.level || "—")}</td>
          <td>${esc((m.last_run || "—").replace("T", " ").slice(0, 16))}</td>
          <td>${esc(m.last_status
            ? m.last_status + (m.failures ? ` (×${m.failures})` : "")
            : "never run")}</td>
          <td class="mi-ctrl">
            ${btn("start", "▶ Start", m.name, !canStart || running || stopped)}
            ${paused
              ? btn("resume", "⏵ Resume", m.name, !canResume || stopped)
              : btn("pause", "⏸ Pause", m.name, !canPause)}
            <button class="btn" data-act="stop" data-name="${esc(m.name)}" disabled
              title="backend has no per-mission stop">■ Stop</button>
          </td>
        </tr>`;
      }).join("") + `</tbody></table>
      <div class="mi-btns">
        <button class="btn btn-danger" id="mi-emg">⛔ GLOBAL EMERGENCY STOP</button>
        <button class="btn" id="mi-resume"${stopped ? "" : " disabled"}>⏶ Resume autonomy</button>
      </div>`;

    box.querySelectorAll("button[data-act]").forEach(el => {
      const act = el.dataset.act, name = el.dataset.name;
      if (act === "stop") return;               // stays disabled — honesty
      el.addEventListener("click", () => action(act, name));
    });
    box.querySelector("#mi-emg")?.addEventListener("click", globalStop);
    box.querySelector("#mi-resume")?.addEventListener("click", resumeGlobal);
  }

  function paintHistory() {
    const box = document.getElementById("mi-history");
    if (!box) return;
    const rows = (data && data.history) || [];
    if (!rows.length) {
      box.innerHTML = `<div class="empty">No runs recorded yet.</div>`;
      return;
    }
    box.innerHTML = `<table class="sy-table"><thead><tr>
      <th>When</th><th>Mission</th><th>Result</th><th>Detail</th>
      </tr></thead><tbody>` +
      rows.map(h => `<tr>
        <td>${esc(String(h.ts || h.at || "").replace("T", " ").slice(0, 16))}</td>
        <td>${esc(h.task || h.name || "—")}</td>
        <td><span class="tr-mini ${h.status === "ok" ? "ok" : "warn"}">
          ${esc(h.status || "—")}</span></td>
        <td>${esc(String(h.note || "—")).slice(0, 140)}</td>
      </tr>`).join("") + `</tbody></table>`;
  }

  return { build };
})();

window.RIOMissions = RIOMissions;
