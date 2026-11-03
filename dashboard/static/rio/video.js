/* RIO Video Studio — Video Creation Agent (Phase 9).
 *
 * Data:   GET  /api/video/overview          jobs (real store phases) + caps
 *         POST /api/video/create            full tool surface (auth'd)
 *         POST /api/video/cancel {job_id?}  cancel latest or specific
 *         POST /api/video/retry  {job_id?}  re-queue a failed job
 *         POST /api/video/split  {count}    documentary → Shorts (narration
 *                                           reused, no re-synthesis)
 *         GET  /api/video/library           finished files (real ffprobe)
 *         GET  /api/video/thumb?path=       cached ffmpeg thumbnail
 *         GET  /api/video/file?path=        whitelisted preview/download
 *         GET  /api/video/capabilities      honest capability matrix
 *         GET  /api/video/encoder           real VAAPI/CPU + NVIDIA probe
 *
 * Honest: progress is the job store's real phase/progress — no fake bars.
 * AI cinematic shows its measured billing-blocked status; the documentary
 * alternative is offered, never substituted silently.
 */
"use strict";

const RIOVideo = (() => {

  let overview = null;
  let library = null;
  let busy = false;
  let pollTimer = null;
  let previewPath = null;

  const $ = (sel, el) => (el || document).querySelector(sel);

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; }
    catch (_) { return ""; }
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Authorization": `Bearer ${token()}`,
                 "Content-Type": "application/json" },
    }, opts));
    let body = {};
    try { body = await r.json(); } catch (_) {}
    return { status: r.status, body };
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "\u0026amp;").replace(/</g, "\u0026lt;")
      .replace(/>/g, "\u0026gt;").replace(/"/g, "\u0026quot;");
  }

  function fmtBytes(b) {
    if (!b && b !== 0) return "—";
    if (b > 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + " MB";
    return Math.max(1, Math.round(b / 1024)) + " KB";
  }

  function fmtDur(s) {
    if (!s) return "—";
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return (m ? m + "m " : "") + r + "s";
  }

  function fmtDate(t) {
    if (!t) return "—";
    const d = new Date(t * 1000);
    return d.toLocaleDateString() + " " + d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }

  // ── sections ──────────────────────────────────────────────────────────

  function newVideoSection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>New Video</h3>
      <div class="video-form">
        <label>Topic <input id="v-topic" placeholder="what the video is about"></label>
        <label>Type
          <select id="v-mode">
            <option value="DOCUMENTARY">Documentary (free)</option>
            <option value="SHORTS">Short / Reel 9:16 (free)</option>
            <option value="AI_CINEMATIC">AI Cinematic (paid)</option>
          </select>
        </label>
        <label>Duration (s) <input id="v-duration" type="number" min="10" max="900" placeholder="auto by type"></label>
        <label>Language <input id="v-language" placeholder="simple English"></label>
        <label>Voice <input id="v-voice" placeholder="calm narrator (auto)"></label>
        <label>Style <input id="v-style" placeholder="auto by type"></label>
        <label>Aspect <input id="v-aspect" placeholder="auto by type"></label>
        <label>Quality <input id="v-resolution" placeholder="1080p"></label>
        <label>Encoder
          <select id="v-encoder">
            <option value="auto">Auto (VAAPI → CPU)</option>
            <option value="vaapi">Intel VAAPI H.264</option>
            <option value="libx264">CPU libx264</option>
          </select>
        </label>
        <label>Fade transitions
          <input id="v-transitions" type="checkbox">
        </label>
        <label class="v-wide">Cinematic script (optional — skips the AI story stage)
          <textarea id="v-script" rows="2" placeholder="paste your script for script2video"></textarea></label>
        <label class="v-wide">Cinematic image (optional — image-to-video)
          <input id="v-image" placeholder="/path/to/image.png"></label>
        <label class="v-wide">Cinematic confirm (paid API)
          <input id="v-confirm" type="checkbox"></label>
        <label class="v-wide">Documentary full render (default is a 30s sample first)
          <input id="v-full" type="checkbox"></label>
      </div>
      <div class="v-actions">
        <button id="v-generate" class="btn small">Generate</button>
        <span id="v-create-msg" class="v-msg"></span>
      </div>
      <div id="v-approval" class="v-approval" style="display:none">
        <h4>Method approval</h4>
        <div class="v-muted">Nothing starts without your choice — the
        token is single-use and bound to this video.</div>
        <div class="video-form">
          <label>Method
            <select id="v-method">
              <option value="CANVAS">Canvas (stills + motion, free)</option>
              <option value="BLENDER_3D">Blender 3D (free)</option>
              <option value="VIMAX">ViMax (paid providers)</option>
              <option value="STOCK_FOOTAGE">Stock footage (licensed)</option>
              <option value="WAN_2_1">Wan 2.1 (remote GPU)</option>
              <option value="LTX_VIDEO">LTX-Video (remote GPU)</option>
              <option value="HYBRID">Hybrid (per-scene engines)</option>
            </select>
          </label>
          <label class="v-wide" id="v-sceneplan-row" style="display:none">
            Scene→engine table <input id="v-sceneplan"
            placeholder="1:CANVAS,2:CANVAS,3:BLENDER_3D"></label>
        </div>
        <div class="v-actions">
          <button id="v-approve" class="btn small">Approve & render</button>
          <span id="v-approve-token" class="v-muted"></span>
        </div>
      </div>`;
    $("#v-generate", box).addEventListener("click", () => createVideo(box));
    wireApproval(box);
    return box;
  }

  async function createVideo(box) {
    if (busy) return;
    busy = true;
    const msg = $("#v-create-msg", box);
    msg.textContent = "queuing…";
    const params = {
      action: "create",
      topic: $("#v-topic", box).value.trim(),
      mode: $("#v-mode", box).value,
      duration_s: $("#v-duration", box).value || undefined,
      language: $("#v-language", box).value || undefined,
      voice: $("#v-voice", box).value || undefined,
      style: $("#v-style", box).value || undefined,
      aspect_ratio: $("#v-aspect", box).value || undefined,
      resolution: $("#v-resolution", box).value || undefined,
      encoder: $("#v-encoder", box).value || "auto",
      transitions: $("#v-transitions", box).checked || undefined,
      full: $("#v-full", box).checked || undefined,
    };
    if (params.mode === "AI_CINEMATIC") {
      params.script = $("#v-script", box).value.trim() || undefined;
      params.image = $("#v-image", box).value.trim() || undefined;
      params.confirm = $("#v-confirm", box).checked;
    }
    const res = await api("/api/video/create", {method: "POST",
                                                body: JSON.stringify(params)});
    const body = res.body || {};
    msg.textContent = body.message ||
      (res.status === 401 ? "unauthorized" : "failed");
    if (body.needs_method && body.approval_token) {
      // Phase 6/9: nothing runs until the user picks a method — reveal the
      // approval sub-form pre-filled with the single-use token
      const approval = $("#v-approval", box);
      approval.style.display = "";
      $("#v-approve-token", box).textContent =
        `token ${body.approval_token}`;
      approval.dataset.token = body.approval_token;
      const methodSel = $("#v-method", box);
      methodSel.value = params.mode === "BLENDER_3D" ? "BLENDER_3D"
        : (params.mode === "AI_CINEMATIC" ? "VIMAX" : "CANVAS");
    }
    busy = false;
    await refresh();
  }

  function wireApproval(box) {
    const methodSel = $("#v-method", box);
    methodSel.addEventListener("change", () => {
      $("#v-sceneplan-row", box).style.display =
        methodSel.value === "HYBRID" ? "" : "none";
    });
    $("#v-approve", box).addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      const approval = $("#v-approval", box);
      const msg = $("#v-create-msg", box);
      msg.textContent = "starting…";
      const params = {
        action: "create",
        topic: $("#v-topic", box).value.trim(),
        mode: $("#v-mode", box).value,
        duration_s: $("#v-duration", box).value || undefined,
        language: $("#v-language", box).value || undefined,
        resolution: $("#v-resolution", box).value || undefined,
        approve_token: approval.dataset.token || "",
        method: methodSel.value,
      };
      if (methodSel.value === "HYBRID") {
        params.scene_plan = $("#v-sceneplan", box).value.trim() || undefined;
      }
      const res = await api("/api/video/create", {method: "POST",
                                                  body: JSON.stringify(params)});
      const body = res.body || {};
      msg.textContent = body.message || "failed";
      if (!(body.needs_method && body.approval_token)) {
        approval.style.display = "none";
        approval.dataset.token = "";
      }
      busy = false;
      await refresh();
    });
  }

  function encoderSection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>Encoding Status</h3>
      <div class="v-muted">Measured live — Intel VAAPI H.264 is used only when
      its real probe passes; CPU libx264 is the automatic fallback. NVIDIA
      effects appear only if an accelerated ffmpeg path actually exists.</div>
      <pre id="v-enc" class="v-caps">loading…</pre>`;
    return box;
  }

  // ── Phase 9: Engine Selection / Hardware Status / Tool Installer ──────

  function enginesSection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>Engine Selection <button id="v-engines-refresh" class="btn small">Refresh</button></h3>
      <div class="v-muted">Measured status — READY only after a real probe.
      Jarvis never starts a video without your approval.</div>
      <div id="v-engines" class="v-engines"><div class="v-muted">loading…</div></div>`;
    $("#v-engines-refresh", box).addEventListener("click", async () => {
      const el = $("#v-engines", box);
      el.textContent = "re-measuring hardware + engines…";
      const res = await api("/api/video/engines?refresh=1");
      renderEngines(el, res.body || {});
    });
    api("/api/video/engines").then(res =>
      renderEngines($("#v-engines", box), res.body || {}));
    return box;
  }

  function renderEngines(el, body) {
    if (!el) return;
    const rows = body.engines || [];
    if (!rows.length) {
      el.textContent = "no engines registered";
      return;
    }
    el.replaceChildren(...rows.map(row => {
      const d = row.descriptor || {}, st = row.status || {};
      const card = document.createElement("div");
      card.className = "v-engine";
      const pill = String(st.status || "").toLowerCase();
      card.innerHTML = `
        <div class="v-job-head">
          <strong>${esc(d.name || d.id)}</strong>
          <span class="v-pill ${pill}">${esc(st.status || "?")}</span>
          <span class="v-muted">${esc((d.execution || []).join("/"))} ·
            ${esc(d.estimated_cost || "")}</span>
        </div>
        <div class="v-muted">${esc(st.detail || "")}</div>`;
      return card;
    }));
  }

  function hardwareSection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>Hardware Status <button id="v-hw-refresh" class="btn small">Refresh</button></h3>
      <div class="v-muted">One measured report: CPU, RAM, disk, GPUs
      (VAAPI verified by a real encode; CUDA only when the driver actually
      works — a GPU label never implies CUDA), ffmpeg encoders, remote
      workers.</div>
      <pre id="v-hw" class="v-caps">loading…</pre>`;
    $("#v-hw-refresh", box).addEventListener("click", async () => {
      const el = $("#v-hw", box);
      el.textContent = "re-measuring…";
      const res = await api("/api/video/hardware?refresh=1");
      if (el) el.textContent = (res.body && res.body.text) || "unavailable";
    });
    api("/api/video/hardware").then(res => {
      const el = $("#v-hw", box);
      if (el) el.textContent = (res.body && res.body.text) || "unavailable";
    });
    return box;
  }

  function installerSection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>Tool Installer</h3>
      <div class="v-muted">Detects what is installed, shows a plan (size,
      disk, official source) and installs ONLY after your approval —
      detached and cancellable. Never touches drivers, CUDA or system
      packages.</div>
      <div class="video-form">
        <label>Engine
          <select id="v-inst-engine">
            <option value="BLENDER_3D">Blender 3D</option>
            <option value="TTS_KOKORO">Kokoro TTS (local voice)</option>
            <option value="VIMAX">ViMax (via repo agent)</option>
            <option value="WAN_2_1">Wan 2.1 (weights)</option>
            <option value="LTX_VIDEO">LTX-Video (weights)</option>
            <option value="STOCK_FOOTAGE">Stock footage (provider)</option>
          </select>
        </label>
      </div>
      <div class="v-actions">
        <button id="v-inst-plan" class="btn small">Show plan</button>
        <button id="v-inst-install" class="btn small" style="display:none">Install (approved)</button>
        <span id="v-inst-msg" class="v-msg"></span>
      </div>
      <pre id="v-inst-plan-text" class="v-caps" style="display:none"></pre>`;
    $("#v-inst-plan", box).addEventListener("click", async () => {
      const engine = $("#v-inst-engine", box).value;
      const res = await api("/api/video/installer", {method: "POST",
          body: JSON.stringify({action: "plan", engine_id: engine})});
      const body = res.body || {};
      const msg = $("#v-inst-msg", box);
      const pre = $("#v-inst-plan-text", box);
      pre.style.display = "";
      pre.textContent = body.plan ? JSON.stringify(body.plan, null, 1)
        : (body.message || body.error || "nothing to install");
      if (body.approval_token) {
        pre.dataset.token = body.approval_token;
        $("#v-inst-install", box).style.display = "";
        msg.textContent = "plan ready — approving runs a detached, "
          + "cancellable install";
      } else {
        $("#v-inst-install", box).style.display = "none";
      }
    });
    $("#v-inst-install", box).addEventListener("click", async () => {
      const engine = $("#v-inst-engine", box).value;
      const token = $("#v-inst-plan-text", box).dataset.token || "";
      const res = await api("/api/video/installer", {method: "POST",
          body: JSON.stringify({action: "install", engine_id: engine,
                                approve_token: token})});
      const body = res.body || {};
      $("#v-inst-msg", box).textContent = body.message || body.error || "";
      await refresh();
    });
    api("/api/video/installer").then(res => {
      // read-only survey as the start state of the plan pane
      const pre = $("#v-inst-plan-text", box);
      if (pre && !pre.textContent) {
        pre.style.display = "";
        pre.textContent = JSON.stringify(res.body && res.body.survey || {},
                                         null, 1);
      }
    });
    return box;
  }

  function renderEncoder(enc, gpu) {
    const el = $("#v-enc");
    if (!el || !enc) return;
    const v = enc.vaapi || {};
    const checks = v.checks || {};
    const mem = enc.memory || {};
    const out = enc.output || {};
    el.textContent = [
      `Selected: ${enc.selected}`
        + (enc.hardware_accelerated ? " (Intel VAAPI hardware H.264)"
                                    : " (CPU libx264)"),
      `Reason: ${enc.reason || "—"}`,
      `VAAPI device: ${v.device || "—"}`
        + (v.device_resolved ? ` → ${v.device_resolved}` : ""),
      `Driver: ${v.driver || "—"}`
        + ` (${checks.i965_driver_present ? "present" : "missing"})`,
      `Probe: ${v.available ? "PASS" : "FAIL"} — ${v.detail || ""}`,
      `Output: ${out.video_codec || "h264"} + ${out.audio_codec || "aac"}, `
        + `${out.fps || 30} fps, ${out.vertical || "1080x1920"}`,
      `RAM: ${Math.round(mem.available_mb || 0)} MB available `
        + `(floor ${Math.round(mem.minimum_mb || 0)} MB)`,
      `NVIDIA effects: ${gpu ? gpu.reason : "not probed"}`,
      `CUDA/NVENC: not used`,
    ].join("\n");
  }

  function jobsSection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>Active Jobs <button id="v-split-btn" class="btn small">Shorts from latest documentary</button></h3>
      <div id="v-jobs" class="v-jobs"><div class="v-muted">loading…</div></div>
      <div class="v-actions"><span id="v-jobs-msg" class="v-msg"></span></div>`;
    $("#v-split-btn", box).addEventListener("click", async () => {
      const res = await api("/api/video/split", {method: "POST",
                                                 body: JSON.stringify({count: 3})});
      $("#v-jobs-msg", box).textContent = (res.body && res.body.message) || "";
      await refresh();
    });
    return box;
  }

  function jobCard(job) {
    const pct = Math.round((job.progress || 0) * 100);
    const active = ["QUEUED", "INSTALLING", "RUNNING"]
      .includes(job.status);
    const state = (job.status === "RUNNING" && job.phase)
      ? job.phase.toUpperCase() : job.status;
    const card = document.createElement("div");
    card.className = "v-job";
    card.innerHTML = `
      <div class="v-job-head">
        <strong>${esc(job.job_id)}</strong>
        <span class="v-pill ${job.status.toLowerCase()}">${esc(state)} ${pct}%</span>
        <span class="v-muted">${esc(job.pipeline || job.ref || "")} · ${esc(job.topic || "")}</span>
      </div>
      <div class="v-bar"><div class="v-bar-fill" style="width:${pct}%"></div></div>
      ${job.message ? `<div class="v-muted v-msgline">${esc(job.message)}</div>` : ""}
      ${job.error ? `<div class="v-err">error: ${esc(String(job.error).slice(-200))}</div>` : ""}
      <div class="v-job-actions">
        ${active ? `<button data-act="cancel" data-id="${esc(job.job_id)}" class="btn small">Cancel</button>` : ""}
        ${["FAILED", "CANCELLED"].includes(job.status)
          ? `<button data-act="retry" data-id="${esc(job.job_id)}" class="btn small">Retry</button>` : ""}
        ${job.status === "COMPLETED" && job.pipeline === "documentary"
          ? `<button data-act="split" data-id="${esc(job.job_id)}" class="btn small">Make Shorts</button>` : ""}
      </div>`;
    card.querySelectorAll("button[data-act]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const act = btn.dataset.act;
        const res = await api(`/api/video/${act}`, {method: "POST",
            body: JSON.stringify({job_id: btn.dataset.id, count: 3})});
        const line = $(".v-msgline", card) ||
          card.appendChild(document.createElement("div"));
        line.className = "v-muted v-msgline";
        line.textContent = (res.body && res.body.message) || "";
        await refresh();
      });
    });
    return card;
  }

  function librarySection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>Video Library</h3>
      <div id="v-library" class="v-library"><div class="v-muted">loading…</div></div>
      <div id="v-preview" class="v-preview"></div>`;
    return box;
  }

  function libraryCard(item) {
    const card = document.createElement("div");
    card.className = "v-lib";
    const q = encodeURIComponent(item.path);
    card.innerHTML = `
      <img class="v-thumb" loading="lazy" src="/api/video/thumb?path=${q}"
           onerror="this.style.visibility='hidden'">
      <div class="v-lib-info">
        <strong>${esc(item.title)}</strong>
        <div class="v-muted">${fmtDur(item.duration_s)} · ${fmtBytes(item.size_bytes)} · ${fmtDate(item.created_at)}</div>
        <div class="v-muted v-path">${esc(item.path)}</div>
        <div class="v-lib-actions">
          <button data-act="preview" class="btn small">Preview</button>
          <a class="btn small" href="/api/video/file?path=${q}" download>Download</a>
        </div>
      </div>`;
    card.querySelector("button[data-act=preview]").addEventListener(
      "click", () => {
        previewPath = item.path;
        const pv = $("#v-preview");
        pv.innerHTML = `<video controls autoplay src="/api/video/file?path=${q}" style="max-width:100%"></video>`;
        pv.scrollIntoView({behavior: "smooth"});
      });
    return card;
  }

  function capabilitiesSection() {
    const box = document.createElement("section");
    box.className = "card";
    box.innerHTML = `
      <h3>Provider Settings</h3>
      <div class="v-muted">Provider keys live in config/api_keys.json (never
      shown here). Cinematic providers are measured billing-blocked; the
      documentary and Shorts pipelines are the zero-cost paths.</div>
      <pre id="v-caps" class="v-caps">loading…</pre>`;
    api("/api/video/capabilities").then(res => {
      const pre = $("#v-caps", box);
      const body = res.body || {};
      if (pre) pre.textContent = [body.matrix || "", "",
        body.encoder || "", "", body.gpu_effects || ""].join("\n");
    });
    return box;
  }

  // ── data ──────────────────────────────────────────────────────────────

  async function refresh() {
    const [ov, lib] = await Promise.all([
      api("/api/video/overview"), api("/api/video/library")]);
    overview = ov.body || {};
    library = lib.body || {};
    renderEncoder(overview.encoder, overview.gpu_effects);
    const jobsEl = $("#v-jobs");
    if (jobsEl && Array.isArray(overview.jobs)) {
      const rows = overview.jobs.filter(j =>
        ["video", "run_skill"].includes(j.kind)).slice(0, 12);
      jobsEl.replaceChildren(...(rows.length
        ? rows.map(jobCard)
        : [Object.assign(document.createElement("div"),
                         {className: "v-muted",
                          textContent: "No video jobs yet."})]));
      const anyActive = rows.some(j =>
        ["QUEUED", "INSTALLING", "RUNNING"].includes(j.status));
      schedulePoll(anyActive);
    }
    const libEl = $("#v-library");
    if (libEl && Array.isArray(library.items)) {
      libEl.replaceChildren(...(library.items.length
        ? library.items.map(libraryCard)
        : [Object.assign(document.createElement("div"),
                         {className: "v-muted",
                          textContent: "No finished videos yet."})]));
    }
  }

  function schedulePoll(anyActive) {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (anyActive) pollTimer = setTimeout(refresh, 3000);
  }

  // ── RIO module contract ───────────────────────────────────────────────

  function build() {
    const page = document.createElement("div");
    page.className = "page video-page";
    const head = document.createElement("div");
    head.className = "v-head";
    head.innerHTML = `<h1>Video Studio</h1>
      <p class="v-blurb">Documentary and Shorts render free (research,
      narration, ffmpeg) with Intel VAAPI H.264 when verified and CPU
      libx264 as the automatic fallback. AI Cinematic runs ViMax on paid
      providers and shows its measured status. Progress is always the real
      job state.</p>`;
    page.append(head, newVideoSection(), enginesSection(),
                hardwareSection(), encoderSection(), installerSection(),
                jobsSection(), librarySection(), capabilitiesSection());
    refresh();
    return page;
  }

  function destroy() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  return { build, destroy, refresh };
})();

window.RIOVideo = RIOVideo;
