/* RIO face.js — a cute robot companion for the command center.
 *
 * Pure SVG + CSS (no canvas, no images, no 3D library):
 *
 *   • a rounded ceramic shell head with a dark face screen, antenna,
 *     side ear-pods and a little body with a glowing chest core
 *   • big glossy screen eyes with a coloured iris, pupil and twin
 *     catchlights; they dilate with the state, blink on a natural
 *     schedule, glance around while idle and track the pointer
 *   • LED brows and a small glowing mouth that smiles, frowns, thinks
 *     in a little “o”, flattens when muted and opens into a real
 *     animated cavity while RIO is speaking
 *   • state colours drive the eyes, antenna, ear rings, blush, mouth,
 *     chest core and rim — the shell itself stays a friendly off-white
 *   • a hologram HUD (orbit, sonar ring, scanline, glass sheen) and a
 *     live speech spectrum driven by the same viseme levels as the mouth
 *   • thinking dots, natural gaze saccades, a soft head bob and an
 *     antenna pulse add life
 *
 * Speech is real: the mouth opens from the assistant's output-audio
 * viseme levels that main.py broadcasts over the dashboard WebSocket
 * (the same frames the HUD avatar uses), eased at ~60 fps only while
 * speaking; the spectrum bars share those levels. The mouth always
 * closes when speech ends.
 *
 * State cues (colour overridable by the user's RGB controls):
 *   listening   → blue, eyes wide, brows up, quicker glances
 *   thinking    → orange, gaze up, brows tilted, dots appear
 *   working     → orange, focused squint, brows angled in
 *   speaking    → yellow, animated mouth + spectrum
 *   idle        → violet, relaxed smile, curious saccades
 *   muted       → grey, eyes half closed, flat mouth, antenna dim
 *   error       → red, narrowed eyes, downturned mouth
 *
 * Everything is event-driven: chat turn lifecycle, health/home payloads
 * and the transient WS frames. Reduced motion stops bob/blink/pulse/
 * parallax/dots/scanline/spectrum; colours and states remain.
 */
"use strict";

const RIOFace = (() => {

  const STATES = {
    listening:  { label: "Listening…",        hint: "Microphone live — say something" },
    thinking:   { label: "Processing…",       hint: "Working through your request" },
    speaking:   { label: "Speaking",          hint: "Answering out loud" },
    working:    { label: "RIO is working",    hint: "Background task running" },
    muted:      { label: "Microphone muted",  hint: "Click the Mic button to unmute" },
    idle:       { label: "Standing by",       hint: "Say 'Hey Jarvis' — or type below" },
    error:      { label: "Connection issue",  hint: "Backend unreachable" },
  };
  const PRIORITY = ["error", "speaking", "thinking", "working", "muted",
                    "listening", "idle"];

  // ── geometry helper (generated, not hand-written) ───────────────────
  function tickMarks(cx, cy, r1, r2, n) {
    let d = "";
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2;
      const long = i % 4 === 0;
      const rb = long ? r2 : r2 - 3;
      d += `M${(cx + r1 * Math.cos(a)).toFixed(1)} ${(cy + r1 * Math.sin(a)).toFixed(1)}` +
           `L${(cx + rb * Math.cos(a)).toFixed(1)} ${(cy + rb * Math.sin(a)).toFixed(1)}`;
    }
    return d;
  }

  const SVG = `
<svg class="rio-face face-st-idle" viewBox="0 0 320 360" role="img"
     aria-label="RIO assistant face — a small robot whose eyes, mouth and glow reflect the assistant state">
  <defs>
    <linearGradient id="shellGrad" x1="0.2" y1="0" x2="0.8" y2="1">
      <stop offset="0%"   stop-color="#f6f9ff"/>
      <stop offset="42%"  stop-color="#dde6f4"/>
      <stop offset="100%" stop-color="#b0bed4"/>
    </linearGradient>
    <linearGradient id="screenGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"   stop-color="#0d1729"/>
      <stop offset="100%" stop-color="#050a14"/>
    </linearGradient>
    <linearGradient id="bodyGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"   stop-color="#344662"/>
      <stop offset="100%" stop-color="#152238"/>
    </linearGradient>
    <linearGradient id="metalGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%"   stop-color="#677792"/>
      <stop offset="50%"  stop-color="#aab8cd"/>
      <stop offset="100%" stop-color="#5b6982"/>
    </linearGradient>
    <radialGradient id="bgGlow" cx="50%" cy="42%" r="55%">
      <stop offset="0%"   stop-color="var(--fc-a, #a78bfa)" stop-opacity=".28"/>
      <stop offset="55%"  stop-color="var(--fc-b, #6366f1)" stop-opacity=".1"/>
      <stop offset="100%" stop-color="var(--fc-b, #6366f1)" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="rimGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="48%"  stop-color="var(--fc-a, #a78bfa)" stop-opacity="0"/>
      <stop offset="100%" stop-color="var(--fc-a, #a78bfa)" stop-opacity=".75"/>
    </linearGradient>

    <filter id="tipGlow" x="-220%" y="-220%" width="540%" height="540%">
      <feGaussianBlur stdDeviation="4" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="mouthGlow" x="-200%" y="-200%" width="500%" height="500%">
      <feGaussianBlur stdDeviation="2" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="soft4" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="4"/>
    </filter>

    <clipPath id="eyeLClip"><ellipse cx="128" cy="146" rx="17" ry="20"/></clipPath>
    <clipPath id="eyeRClip"><ellipse cx="192" cy="146" rx="17" ry="20"/></clipPath>
    <clipPath id="screenClip">
      <rect x="100" y="78" width="120" height="150" rx="44"/>
    </clipPath>

    <linearGradient id="scanGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"   stop-color="var(--fc-a, #a78bfa)" stop-opacity="0"/>
      <stop offset="55%"  stop-color="var(--fc-a, #a78bfa)" stop-opacity=".42"/>
      <stop offset="100%" stop-color="var(--fc-a, #a78bfa)" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="screenAura" cx="50%" cy="35%" r="68%">
      <stop offset="0%"   stop-color="var(--fc-a, #a78bfa)" stop-opacity=".32"/>
      <stop offset="62%"  stop-color="var(--fc-b, #6366f1)" stop-opacity=".10"/>
      <stop offset="100%" stop-color="var(--fc-b, #6366f1)" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <!-- ── holographic ambience ─────────────────────────────────────────── -->
  <g class="face-hud">
    <circle class="hud-ring hud-r1" cx="160" cy="150" r="152" fill="none"
            stroke="var(--fc-a, #a78bfa)" stroke-opacity=".16" stroke-width="1"
            stroke-dasharray="2 12"/>
    <circle class="hud-ring hud-r2" cx="160" cy="150" r="139" fill="none"
            stroke="var(--fc-b, #6366f1)" stroke-opacity=".16" stroke-width="1.2"
            stroke-dasharray="48 26"/>
    <circle class="hud-ring hud-r3" cx="160" cy="150" r="146" fill="none"
            stroke="var(--fc-a, #a78bfa)" stroke-opacity=".12" stroke-width=".9"/>
    <g class="hud-orbit">
      <circle cx="160" cy="16" r="2.2" fill="var(--fc-a, #a78bfa)"/>
      <circle cx="160" cy="16" r="5" fill="none" stroke="var(--fc-a, #a78bfa)"
              stroke-opacity=".4" stroke-width="1"/>
    </g>
    <path class="hud-ticks" d="${tickMarks(160, 150, 122, 130, 44)}"
          stroke="var(--fc-a, #a78bfa)" stroke-opacity=".18" stroke-width="1"/>
  </g>
  <circle class="face-glow" cx="160" cy="150" r="140" fill="url(#bgGlow)"/>

  <!-- ── body ─────────────────────────────────────────────────────────── -->
  <ellipse cx="160" cy="256" rx="34" ry="9" fill="#000" opacity=".28"/>
  <path d="M118 266 C102 280 94 302 92 338 L92 360 L228 360 L228 338
           C226 302 218 280 202 266 Z" fill="url(#bodyGrad)"/>
  <path d="M118 268 C102 282 94 302 92 338" fill="none" stroke="#8fa2c2"
        stroke-opacity=".3" stroke-width="1.6"/>
  <path d="M202 268 C218 282 226 302 228 338" fill="none" stroke="#8fa2c2"
        stroke-opacity=".18" stroke-width="1.6"/>
  <path d="M92 340 C122 352 198 352 228 340" fill="none" stroke="#3a4c6b"
        stroke-opacity=".5" stroke-width="1.4"/>
  <rect x="146" y="244" width="28" height="30" rx="9" fill="url(#metalGrad)"/>
  <path d="M148 255 L172 255 M148 262 L172 262" stroke="#2c3a52"
        stroke-width="1.4" opacity=".7"/>
  <ellipse cx="160" cy="272" rx="25" ry="7" fill="#0a1120"
           stroke="#2e3c58" stroke-width="1.4"/>
  <circle class="core-halo" cx="160" cy="312" r="16" fill="var(--fc-a, #a78bfa)"
          opacity=".08" filter="url(#soft4)"/>
  <circle class="chest-core" cx="160" cy="312" r="7" fill="var(--fc-a, #a78bfa)"
          filter="url(#mouthGlow)"/>
  <circle class="core-ring" cx="160" cy="312" r="12" fill="none"
          stroke="var(--fc-a, #a78bfa)" stroke-opacity=".34" stroke-width="1.2"/>
  <rect class="body-vent" x="138" y="330" width="44" height="3" rx="1.5"
        fill="var(--fc-a, #a78bfa)" opacity=".22"/>

  <!-- ── head ─────────────────────────────────────────────────────────── -->
  <g class="face-parallax">
  <g class="face-head">

    <!-- antenna -->
    <path d="M160 52 C160 42 160 34 160 28" fill="none" stroke="#8d9bb3"
          stroke-width="4" stroke-linecap="round"/>
    <circle cx="160" cy="52" r="7" fill="#aebbd0" stroke="#7c8aa3" stroke-width="1"/>
    <g class="antenna">
      <circle class="antenna-tip" cx="160" cy="22" r="7"
              fill="var(--fc-a, #a78bfa)" filter="url(#tipGlow)"/>
      <circle cx="160" cy="22" r="11" class="antenna-halo" fill="none"
              stroke="var(--fc-a, #a78bfa)" stroke-opacity=".35" stroke-width="1.2"/>
      <circle cx="157.5" cy="19.5" r="2.2" fill="#fff" opacity=".8"/>
    </g>

    <!-- ear pods -->
    <rect x="62" y="122" width="24" height="56" rx="11"
          fill="url(#shellGrad)" stroke="#8290a9" stroke-width="1.4"/>
    <rect class="ear-led ear-l" x="66" y="130" width="4" height="40" rx="2"
          fill="var(--fc-a, #a78bfa)" opacity=".7" filter="url(#mouthGlow)"/>
    <rect x="234" y="122" width="24" height="56" rx="11"
          fill="url(#shellGrad)" stroke="#8290a9" stroke-width="1.4"/>
    <rect class="ear-led ear-r" x="250" y="130" width="4" height="40" rx="2"
          fill="var(--fc-a, #a78bfa)" opacity=".7" filter="url(#mouthGlow)"/>

    <!-- shell -->
    <rect x="82" y="50" width="156" height="200" rx="66"
          fill="url(#shellGrad)" stroke="#7f8da6" stroke-width="1.6"/>
    <ellipse cx="132" cy="72" rx="38" ry="11" fill="#fff" opacity=".45"
             filter="url(#soft4)"/>
    <path d="M92 224 C112 244 208 244 228 224" fill="none" stroke="#8e9cb4"
          stroke-opacity=".4" stroke-width="1.4"/>
    <g fill="#8492aa" opacity=".55">
      <circle cx="94" cy="64" r="2.4"/><circle cx="226" cy="64" r="2.4"/>
      <circle cx="94" cy="236" r="2.4"/><circle cx="226" cy="236" r="2.4"/>
    </g>

    <!-- face screen -->
    <rect class="face-screen" x="100" y="78" width="120" height="150" rx="44"
          fill="url(#screenGrad)" stroke="#2c3c5e" stroke-width="2"/>
    <rect class="screen-rim" x="100" y="78" width="120" height="150" rx="44"
          fill="none" stroke="var(--fc-a, #a78bfa)" stroke-opacity=".22"
          stroke-width="1"/>
    <rect x="103" y="81" width="114" height="144" rx="41" fill="none"
          stroke="#000" stroke-opacity=".45" stroke-width="1.5"/>
    <g stroke="#8fb6ff" stroke-opacity=".06" stroke-width="1">
      <path d="M106 104 H214"/><path d="M106 196 H214"/><path d="M106 220 H214"/>
    </g>

    <!-- screen depth: state aura, drifting scanline, glass sheen and the
         speech spectrum (bars animate from the real viseme levels) -->
    <g clip-path="url(#screenClip)" pointer-events="none">
      <rect class="screen-aura" x="100" y="78" width="120" height="150"
            fill="url(#screenAura)"/>
      <ellipse class="face-sheen" cx="122" cy="96" rx="26" ry="5"
               fill="#fff" opacity=".10" filter="url(#soft4)"/>
      <rect class="face-scan" x="100" y="72" width="120" height="28"
            fill="url(#scanGrad)"/>
      <g class="face-eq">
        <rect x="143" y="212" width="3.2" height="10" rx="1.6"/>
        <rect x="149" y="208" width="3.2" height="14" rx="1.6"/>
        <rect x="155" y="204" width="3.2" height="18" rx="1.6"/>
        <rect x="161" y="204" width="3.2" height="18" rx="1.6"/>
        <rect x="167" y="208" width="3.2" height="14" rx="1.6"/>
        <rect x="173" y="212" width="3.2" height="10" rx="1.6"/>
      </g>
    </g>

    <!-- LED brows -->
    <g class="brows">
      <rect class="brow-l" x="112" y="110" width="32" height="4.5" rx="2.25"
            fill="var(--fc-a, #a78bfa)" opacity=".85"/>
      <rect class="brow-r" x="176" y="110" width="32" height="4.5" rx="2.25"
            fill="var(--fc-a, #a78bfa)" opacity=".85"/>
    </g>

    <!-- eyes -->
    <g class="eye-unit eye-l">
      <g clip-path="url(#eyeLClip)">
        <ellipse cx="128" cy="146" rx="17" ry="20" fill="#f7fbff"/>
        <ellipse cx="128" cy="139" rx="17" ry="10" fill="#7f9dc4" opacity=".18"/>
        <g class="eye-ball">
          <circle cx="128" cy="147" r="12" fill="var(--fc-a, #a78bfa)"
                  opacity=".95"/>
          <circle cx="128" cy="147" r="12" fill="none" stroke="#0a1424"
                  stroke-opacity=".4" stroke-width="1.6"/>
          <circle cx="128" cy="147" r="8.6" fill="none" stroke="#fff"
                  stroke-opacity=".18" stroke-width="1"/>
          <circle class="pupil" cx="128" cy="147" r="5.6" fill="#08101c"/>
          <circle cx="123.6" cy="141.8" r="4.1" fill="#fff" opacity=".95"/>
          <circle cx="132.6" cy="153" r="1.7" fill="#fff" opacity=".65"/>
        </g>
        <g class="lid lid-l">
          <rect x="109" y="124" width="38" height="46" fill="#081020"/>
          <path d="M113 166 Q128 171 143 166" fill="none"
                stroke="var(--fc-a, #a78bfa)" stroke-width="2.6"
                stroke-linecap="round"/>
        </g>
      </g>
      <ellipse cx="128" cy="146" rx="17" ry="20" fill="none"
               stroke="#0a1424" stroke-opacity=".35" stroke-width="1.4"/>
    </g>

    <g class="eye-unit eye-r">
      <g clip-path="url(#eyeRClip)">
        <ellipse cx="192" cy="146" rx="17" ry="20" fill="#f7fbff"/>
        <ellipse cx="192" cy="139" rx="17" ry="10" fill="#7f9dc4" opacity=".18"/>
        <g class="eye-ball">
          <circle cx="192" cy="147" r="12" fill="var(--fc-a, #a78bfa)"
                  opacity=".95"/>
          <circle cx="192" cy="147" r="12" fill="none" stroke="#0a1424"
                  stroke-opacity=".4" stroke-width="1.6"/>
          <circle cx="192" cy="147" r="8.6" fill="none" stroke="#fff"
                  stroke-opacity=".18" stroke-width="1"/>
          <circle class="pupil" cx="192" cy="147" r="5.6" fill="#08101c"/>
          <circle cx="187.6" cy="141.8" r="4.1" fill="#fff" opacity=".95"/>
          <circle cx="196.6" cy="153" r="1.7" fill="#fff" opacity=".65"/>
        </g>
        <g class="lid lid-r">
          <rect x="173" y="124" width="38" height="46" fill="#081020"/>
          <path d="M177 166 Q192 171 207 166" fill="none"
                stroke="var(--fc-a, #a78bfa)" stroke-width="2.6"
                stroke-linecap="round"/>
        </g>
      </g>
      <ellipse cx="192" cy="146" rx="17" ry="20" fill="none"
               stroke="#0a1424" stroke-opacity=".35" stroke-width="1.4"/>
    </g>

    <!-- blush -->
    <ellipse class="blush blush-l" cx="114" cy="174" rx="10" ry="6"
             fill="var(--fc-a, #a78bfa)" opacity=".16" filter="url(#soft4)"/>
    <ellipse class="blush blush-r" cx="206" cy="174" rx="10" ry="6"
             fill="var(--fc-a, #a78bfa)" opacity=".16" filter="url(#soft4)"/>

    <!-- mouth: resting LED smile sits UNDER the open cavity, so the
         cavity simply covers it while RIO is talking -->
    <g class="face-mouth">
      <path class="mouth-line" d="M145 198 Q160 206 175 198" fill="none"
            stroke="var(--fc-a, #a78bfa)" stroke-width="3.2"
            stroke-linecap="round" filter="url(#mouthGlow)"/>
      <path class="mouth-flat" d="M147 200 H173" fill="none"
            stroke="var(--fc-a, #a78bfa)" stroke-width="3" stroke-linecap="round"
            filter="url(#mouthGlow)" opacity="0"/>
      <ellipse class="mouth-o" cx="160" cy="199" rx="4.6" ry="4" fill="none"
               stroke="var(--fc-a, #a78bfa)" stroke-width="2.2" opacity="0"
               filter="url(#mouthGlow)"/>
      <g class="face-mouth-open" opacity="0">
        <ellipse cx="160" cy="198" rx="14" ry="10" fill="#0a1220"
                 stroke="var(--fc-a, #a78bfa)" stroke-width="1.6"/>
        <ellipse cx="160" cy="202" rx="6.5" ry="2.5" fill="var(--fc-a, #a78bfa)"
                 opacity=".3"/>
      </g>
      <path class="mouth-frown" d="M145 205 Q160 197 175 205" fill="none"
            stroke="var(--fc-a, #a78bfa)" stroke-width="3.2"
            stroke-linecap="round" filter="url(#mouthGlow)" opacity="0"/>
      <g class="lip-upper"></g>
      <g class="lip-lower"></g>
    </g>

    <!-- rim light -->
    <rect x="82" y="50" width="156" height="200" rx="66" fill="none"
          stroke="url(#rimGrad)" stroke-width="2.4" opacity=".8"
          filter="url(#soft4)"/>
  </g>
  </g>

  <!-- thinking dots -->
  <g class="think-dots" opacity="0">
    <circle cx="240" cy="96" r="3.2" fill="var(--fc-a, #a78bfa)"/>
    <circle cx="253" cy="86" r="2.6" fill="var(--fc-a, #a78bfa)"/>
    <circle cx="264" cy="74" r="2" fill="var(--fc-a, #a78bfa)"/>
  </g>

  <!-- particles -->
  <g class="face-sparks">
    <circle class="face-spark sp1" cx="74"  cy="86"  r="1.6" fill="var(--fc-a, #a78bfa)"/>
    <circle class="face-spark sp2" cx="254" cy="132" r="1.3" fill="var(--fc-a, #a78bfa)"/>
    <circle class="face-spark sp3" cx="58"  cy="210" r="1.5" fill="var(--fc-b, #6366f1)"/>
    <circle class="face-spark sp4" cx="266" cy="236" r="1.6" fill="var(--fc-a, #a78bfa)"/>
    <circle class="face-spark sp5" cx="106" cy="40"  r="1.2" fill="var(--fc-b, #6366f1)"/>
  </g>
</svg>`;

  // ── RGB customization (localStorage-backed, :root-level CSS vars) ────
  const RGB_KEY = "rio_face_rgb";
  const RGB_DEFAULTS = { listening: "", thinking: "", speaking: "" };

  function rgbLoad() {
    try { return Object.assign({}, RGB_DEFAULTS,
      JSON.parse(localStorage.getItem(RGB_KEY) || "{}")); }
    catch (_) { return Object.assign({}, RGB_DEFAULTS); }
  }
  function rgbSave(map) {
    try { localStorage.setItem(RGB_KEY, JSON.stringify(map)); } catch (_) {}
  }
  function rgbApply(map) {
    const s = document.documentElement.style;
    s.setProperty("--face-listening", map.listening || "");
    s.setProperty("--face-thinking", map.thinking || "");
    s.setProperty("--face-speaking", map.speaking || "");
  }

  // ── state machine (honest, event-driven) ────────────────────────────
  let respondingTimer = null;

  function create(opts = {}) {
    const root = document.createElement("div");
    root.className = "face-wrap";
    root.innerHTML = SVG;
    const svg = root.querySelector("svg");
    const mouthOpenEl = root.querySelector(".face-mouth-open");
    const lipUpperEl = root.querySelector(".lip-upper");
    const lipLowerEl = root.querySelector(".lip-lower");
    const mouthLineEl = root.querySelector(".mouth-line");
    const eqBars = Array.from(root.querySelectorAll(".face-eq rect"));
    const state = { current: "idle", label: "", detail: "" };
    const clamp01 = (v) => Math.max(0, Math.min(1, v));
    const reduce = (() => {
      try { return matchMedia("(prefers-reduced-motion: reduce)").matches; }
      catch (_) { return false; }
    })();

    // ── mouth easing (rAF only while speaking) ────────────────────────
    let mouthTarget = 0, mouthNow = 0, rafId = null, lastMouthAt = 0;

    function renderMouth(mo) {
      if (mouthOpenEl) {
        mouthOpenEl.style.opacity = mo < 0.04 ? "0" : "1";
        mouthOpenEl.setAttribute(
          "transform",
          `translate(160 198) scale(${(0.7 + 0.3 * mo).toFixed(3)} ` +
          `${(0.06 + 0.94 * mo).toFixed(3)}) translate(-160 -198)`);
      }
      lipUpperEl?.setAttribute("transform",
        `translate(0 ${(-2.2 * mo).toFixed(2)})`);
      lipLowerEl?.setAttribute("transform",
        `translate(0 ${(4.6 * mo).toFixed(2)})`);
      mouthLineEl?.setAttribute("transform",
        `translate(0 ${(1.2 * mo).toFixed(2)})`);
      // Spectrum bars are driven by the same real viseme level as the mouth,
      // with a tiny per-bar phase offset so they ripple instead of pumping.
      const t = performance.now() / 95;
      for (let i = 0; i < eqBars.length; i++) {
        const wave = 0.55 + 0.45 * Math.abs(Math.sin(t + i * 1.15));
        const h = 0.16 + mo * 1.35 * wave;
        eqBars[i].style.transform = `scaleY(${h.toFixed(3)})`;
      }
    }

    function mouthFrame() {
      if (state.current !== "speaking") { rafId = null; return; }
      if (performance.now() - lastMouthAt > 600) mouthTarget = 0;
      mouthNow += (mouthTarget - mouthNow) * 0.35;
      renderMouth(mouthNow);
      rafId = requestAnimationFrame(mouthFrame);
    }
    function startMouth() {
      if (rafId == null) rafId = requestAnimationFrame(mouthFrame);
    }

    function setState(next, { label, detail } = {}) {
      if (!STATES[next]) next = "idle";
      const prev = state.current;
      state.current = next;
      state.label = label != null ? label : STATES[next].label;
      state.detail = detail != null ? detail : STATES[next].hint;
      for (const s of PRIORITY) svg.classList.remove("face-st-" + s);
      svg.classList.add("face-st-" + next);
      if (prev !== next) {
        // A new expression starts from centre stage; idle saccades resume
        // on their own schedule once the state settles.
        root.style.setProperty("--gaze-x", "0px");
        root.style.setProperty("--gaze-y", "0px");
      }
      if (next === "speaking") {
        if (prev !== "speaking") startMouth();
      } else if (prev === "speaking") {
        // close cleanly when speech ends — never leave the mouth stuck open
        mouthTarget = 0; mouthNow = 0;
        renderMouth(0);
        if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
      }
      root.dispatchEvent(new CustomEvent("facestate", {
        detail: { state: next, label: state.label, detail: state.detail },
      }));
    }

    // ── idle life: natural (non-lockstep) blinks + gaze saccades ──────
    // CSS could blink on a fixed clock; scheduling in JS lets the face blink
    // every few seconds, sometimes twice, and glance around while it waits.
    let blinkTimer = null, saccadeTimer = null;
    const rand = (lo, hi) => lo + Math.random() * (hi - lo);

    function blinkOnce() {
      if (!root.isConnected) return;
      svg.classList.add("face-blink");
      setTimeout(() => svg.classList.remove("face-blink"), 120);
    }
    function scheduleBlink() {
      if (reduce) return;
      clearTimeout(blinkTimer);
      blinkTimer = setTimeout(() => {
        if (!root.isConnected) return;   // detached faces stop living
        blinkOnce();
        if (Math.random() < 0.28) setTimeout(blinkOnce, 230);
        scheduleBlink();
      }, rand(2800, 6800));
    }
    function scheduleSaccade() {
      if (reduce) return;
      clearTimeout(saccadeTimer);
      saccadeTimer = setTimeout(() => {
        if (!root.isConnected) return;
        if (state.current === "idle" || state.current === "listening") {
          root.style.setProperty("--gaze-x", rand(-2.4, 2.4).toFixed(2) + "px");
          root.style.setProperty("--gaze-y", rand(-1.8, 1.8).toFixed(2) + "px");
        } else {
          root.style.setProperty("--gaze-x", "0px");
          root.style.setProperty("--gaze-y", "0px");
        }
        scheduleSaccade();
      }, state.current === "listening" ? rand(1500, 3600)
                                        : rand(2400, 6200));
    }

    // ── pointer parallax + pupil tracking ────────────────────────────
    if (!reduce) {
      let raf = null, lastEvt = null;
      const apply = () => {
        raf = null;
        const e = lastEvt;
        if (!e || !root.isConnected) return;
        const r = root.getBoundingClientRect();
        if (!r.width || !r.height) return;
        const nx = Math.max(-1, Math.min(1, (e.clientX - r.left) / r.width * 2 - 1));
        const ny = Math.max(-1, Math.min(1, (e.clientY - r.top) / r.height * 2 - 1));
        root.style.setProperty("--look-x", (nx * 4).toFixed(2) + "px");
        root.style.setProperty("--look-y", (ny * 3).toFixed(2) + "px");
        root.style.setProperty("--look-r", (nx * 0.8).toFixed(2) + "deg");
      };
      const onMove = (e) => { lastEvt = e; if (raf == null) raf = requestAnimationFrame(apply); };
      requestAnimationFrame(() => {
        const host = root.closest(".cmd-hero") || root.parentElement;
        if (host) host.addEventListener("pointermove", onMove, { passive: true });
        else window.addEventListener("pointermove", onMove, { passive: true });
      });
    }

    // ── wire REAL events ──────────────────────────────────────────────
    let lastHealth = null, lastHome = null, lastMicMuted = false;
    let wsAssistState = null;      // from the transient WS frames

    RIOBus.on("chat:busy",  () => { clearTimeout(respondingTimer); setState("thinking"); });
    RIOBus.on("chat:reply", () => {
      clearTimeout(respondingTimer);
      setState("speaking");
      holdSpeech(6000);
    });
    RIOBus.on("chat:fail",  () => { clearTimeout(respondingTimer); respondingTimer = null; resolveBase(); });
    // Polls keep arriving while RIO talks — they must not cut the reply short.
    RIOBus.on("micmute",    (m) => { lastMicMuted = !!m; if (!respondingTimer) resolveBase(); });
    RIOBus.on("data:health", (h) => { lastHealth = h; if (!respondingTimer) resolveBase(); });
    RIOBus.on("data:home",   (d) => { lastHome = d;   if (!respondingTimer) resolveBase(); });

    function holdSpeech(ms) {
      clearTimeout(respondingTimer);
      respondingTimer = setTimeout(() => {
        respondingTimer = null;
        resolveBase();
      }, ms);
    }

    function resolveBase() {
      if (lastHealth === null) { setState("idle"); return; }
      const backendDown = !lastHealth || !lastHealth.ok ||
        (lastHealth.checks && lastHealth.checks.backend &&
         lastHealth.checks.backend.state === "down");
      if (backendDown) { setState("error"); return; }
      if (wsAssistState === "SPEAKING") { setState("speaking"); return; }
      if (wsAssistState === "THINKING") { setState("thinking"); return; }
      if (lastMicMuted) { setState("muted"); return; }
      const doing = lastHome && lastHome.doing;
      if (doing && doing.state === "busy") {
        setState("working", { detail: doing.detail || "" });
        return;
      }
      if (wsAssistState === "SLEEPING") { setState("idle"); return; }
      setState("listening");
    }

    // ── transient WS: mouth levels + live assist states ───────────────
    let ws = null, wsTries = 0;
    function wsOnmsg(ev) {
      let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m && m.type === "mouth" && typeof m.lvl === "number") {
        mouthTarget = clamp01(m.lvl);
        lastMouthAt = performance.now();
        if (state.current === "speaking") {
          startMouth();
          // keep the speaking state alive while real viseme frames flow,
          // then fall back to the live status 2.5s after the last frame
          holdSpeech(2500);
        }
      } else if (m && m.type === "assist_state" && typeof m.state === "string") {
        wsAssistState = m.state;
        resolveBase();
      }
    }
    function ensureWs() {
      if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
      const tok = (() => {
        try { return sessionStorage.getItem("jarvis_token") || ""; }
        catch (_) { return ""; }
      })();
      if (!tok) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      try {
        ws = new WebSocket(
          `${proto}://${location.host}/ws?token=${encodeURIComponent(tok)}`);
      } catch (_) { return; }
      ws.onmessage = wsOnmsg;
      ws.onclose = () => {
        ws = null;
        if (wsTries < 3) {
          wsTries += 1;
          setTimeout(ensureWs, 4000 * wsTries);
        }
      };
      ws.onopen = () => { wsTries = 0; };
    }

    rgbApply(rgbLoad());
    ensureWs();
    setState(opts.state || "idle");
    scheduleBlink();
    scheduleSaccade();
    return {
      el: root,
      setState,
      get state() { return state.current; },
      get label() { return state.label; },
      get detail() { return state.detail; },
    };
  }

  return { create, STATES, rgbLoad, rgbSave, rgbApply, RGB_DEFAULTS };
})();

window.RIOFace = RIOFace;
