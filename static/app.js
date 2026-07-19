/* TikTok Live Leaderboard — Browser client
   Connects to the WebSocket server. The WS host/port is derived from the
   page's own URL so it works whether the browser source loads via
   localhost or a public IP. The page is served on port 8766 (HTTP) and the
   live data WebSocket runs on the SAME port (8766) for same-origin compatibility. */

// WebSocket endpoint resolution:
//   1. If a <meta name="ws-url"> tag is present, use it (lets a page hosted
//      on one domain — e.g. Cloudflare Pages — talk to a backend on another).
//   2. Otherwise derive from the page's own URL (same-origin). Works for
//      localhost and when the page + WS share a host (single tunnel/ALB).
// Auto-upgrade to wss:// when the resolved URL's protocol is https.
const WS_META = document.querySelector('meta[name="ws-url"]');
let WS_URL;
if (WS_META && WS_META.content) {
  // meta may be http(s)://host/ or ws://host/ — normalize to ws:// or wss://
  const metaUrl = new URL(WS_META.content);
  const wsProto = (metaUrl.protocol === "https:" || metaUrl.protocol === "wss:") ? "wss" : "ws";
  WS_URL = `${wsProto}://${metaUrl.host}/`;
}

let previousScores = {};   // user -> points (to detect changes for bump animation)
let lastState = null;      // most recent snapshot from the server
let lastStateTime = 0;     // performance.now() when lastState arrived
let prevHypeLevel = 0;     // to detect the transition INTO max hype (for the howl)

// --- Wolf howl (Web Audio, no external file) ---
// Synthesizes a short rising-then-falling "howl" so the overlay can cheer
// when the Hype Meter maxes out. Lazily created on first user gesture/audio.
let _audioCtx = null;
function playWolfHowl() {
  // sound disabled per user request
}

function connect() {
  const ws = new WebSocket(WS_URL);

  ws.onopen = () => console.log("[WS] Connected to leaderboard server");
  ws.onclose = () => {
    console.log("[WS] Disconnected, retrying in 3s...");
    pollState();  // don't wait — grab live state via HTTP immediately
    setTimeout(connect, 3000);
  };
  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    if (data.type === "state") {
      lastState = data;
      lastStateTime = performance.now();
      render(data);
    }
  };
  ws.onerror = (e) => { console.error("[WS] Error", e); };
}

// Fallback refresh: re-render the last snapshot every 0.5s so the hype
// countdown ticks smoothly even between WebSocket pushes.
setInterval(() => {
  if (lastState) render(lastState);
}, 500);

// HTTP polling fallback: if the WebSocket is blocked by the OBS / TikTok Live
// Studio browser sandbox, poll /state every 2s so likes/gifts ALWAYS update.
// This makes the overlay work even when WS connections fail in the sandbox.
let _httpPolling = false;
async function pollState() {
  try {
    const res = await fetch("/state", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    dbgRaw("HTTP", data);
    lastState = data;
    lastStateTime = performance.now();
    render(data);
    _httpPolling = true;
  } catch (e) {
    console.error("[pollState] failed:", e);
  }
}
setInterval(pollState, 2000);
// Kick an immediate poll so the overlay shows live data even before WS connects.
pollState();


function render(data) {
 try {
  // LIKE TOTAL FIRST — update before anything that could throw, so the likes
  // display is bulletproof regardless of other render errors.
  try {
    const likeTotal = (typeof data.like_total === "number") ? data.like_total : 0;
    const followCount = (typeof data.follow_count === "number") ? data.follow_count : 0;
    const ltEl = document.getElementById("like-total");
    const fcEl = document.getElementById("follow-count");
    if (ltEl) ltEl.textContent = likeTotal.toLocaleString() + " likes";
    if (fcEl) fcEl.textContent = followCount + " follows";
  } catch (e) {
    console.error("[render] like-total update failed:", e);
  }
  // Status (guarded: elements/null/undefined safe)
  try {
    const dot = document.getElementById("live-dot");
    const isLive = (data && data.connected === true);
    if (dot) dot.className = "dot " + (isLive ? "on" : "off");
    const st = document.getElementById("status-text");
    if (st) st.textContent = isLive ? "LIVE" : "OFFLINE";
    const su = document.getElementById("stream-user");
    if (su) su.textContent = (data && data.stream_user) ? String(data.stream_user).replace("@", "") : "";
  } catch (e) {
    console.error("[render] status update failed:", e);
  }

  // Hype Meter
  const maxLevel = 20; // 20 steps of 0.1 = x3.0 (full main bar + rainbow)
  // Main bar fills 0->100% as multiplier climbs x1.0 -> x3.0, then caps full.
  const pct = Math.min(data.hype_level, maxLevel) / maxLevel * 100;
  const hypeFill = document.getElementById("hype-fill");
  hypeFill.style.width = pct + "%";
  // Rainbow spectrum when the meter is MAXED (full hype).
  hypeFill.classList.toggle("rainbow", data.hype_level >= maxLevel);

  // Overdrive bar — sits ON TOP of a full main bar when hype exceeds x3.0.
  // Fills x3.0 (step 20) -> x5.0 (step 40), then stays full. Hidden below x3.0.
  const odRow = document.getElementById("hype-overdrive-row");
  const hypeOd = document.getElementById("hype-overdrive");
  const odActive = data.hype_level > maxLevel;
  if (odRow && hypeOd) {
    if (odRow.classList.contains("active") !== odActive) {
      odRow.classList.toggle("active", odActive);
    }
    if (odActive) {
      const odSteps = Math.min(data.hype_level - maxLevel, maxLevel);
      hypeOd.style.width = (odSteps / maxLevel * 100) + "%";
    } else {
      hypeOd.style.width = "0%";
    }
  }

  // Full-screen edge glow + wolf howl when hype is MAXED.
  // Only FIRE the howl on the transition INTO max (prev < max, now == max),
  // so it plays once per max-up, not on every 500ms render tick.
  // Glow is attached to the .board element (not the screen) so it hugs the panel.
  const boardEl = document.querySelector(".board");
  const isMax = data.hype_level >= maxLevel;
  if (boardEl) {
    if (boardEl.classList.contains("maxed") !== isMax) {
      boardEl.classList.toggle("maxed", isMax);
    }
  }
  if (isMax && prevHypeLevel < maxLevel) {
    playWolfHowl();
  }
  prevHypeLevel = data.hype_level;
  const hypeMult = (typeof data.hype_multiplier === "number") ? data.hype_multiplier : 1.0;
  document.getElementById("hype-mult").textContent = "x" + hypeMult.toFixed(2).replace(/\.?0+$/, "");


  // Hype Countdown — two-phase:
  //   Grace (calm): hype > 0, no warning shown, waiting 3s after last activity.
  //   Countdown (active): 15s ticking down; any like/gift resets to grace.
  // NOTE: only write className/textContent when they actually change, otherwise
  // re-assigning the class every render (500ms tick + WS pushes) RESTARTS the
  // CSS animations and makes the text appear to "flash".
  const cd = document.getElementById("hype-countdown");
  // Server sends the live remaining seconds (hype_countdown); use it directly
  // so the ticker matches the server's 15s countdown (no client double-count).
  const liveCountdown = (data.hype_countdown_active ? Math.max(0, data.hype_countdown || 0) : 0);
  let cdClass, cdText;
  if (data.hype_level > 0 && data.hype_countdown_active && liveCountdown > 0) {
    cdClass = "hype-countdown warning";
    cdText = (liveCountdown <= 5)
      ? `⚠️ HYPE DROPS IN ${liveCountdown}s — KEEP LIKING!`
      : `⏳ Hype dropping in ${liveCountdown}s...`;
  } else if (data.hype_level > 0) {
    // Grace period — calm, steady (no flashing).
    cdClass = "hype-countdown";
    cdText = "⚡ Hype high — keep liking or it drops!";
  } else {
    // Hype at 0 - invite the audience to activate it.
    cdClass = "hype-countdown invite";
    cdText = "GIFTS and TAPS activate the HYPE METER!";
  }
  if (cd.className !== cdClass) cd.className = cdClass;
  if (cd.textContent !== cdText) cd.textContent = cdText;

  // Recent Likers
  const rl = document.getElementById("recent-likers");
  if (!data.recent_likers || !data.recent_likers.length) {
    rl.innerHTML = '<span class="empty">No likes yet</span>';
  } else {
    rl.innerHTML = data.recent_likers
      .map((u) => `<span class="liker">${escapeHtml(u)}</span>`)
      .join("");
  }

  // Leaderboard
  // Leaderboard — ALWAYS render exactly 10 fixed rows so the panel height
  // never changes (no more layout jump as viewers join/leave). Real users
  // fill the top ranks; empty ranks show dimmed placeholder rows.
  const lb = document.getElementById("leaderboard");
  const MAX_ROWS = 10;
  const rows = (data.leaderboard || []).slice(0, MAX_ROWS);
  let html = "";
  rows.forEach((row) => {
    const bump = (previousScores[row.user] !== undefined && row.points > previousScores[row.user]) ? " bump" : "";
    html += `<li class="${bump.trim()}">`
      + `<div class="lb-user">${escapeHtml(row.user)}<br><span class="gifts">${row.gifts} gift${row.gifts !== 1 ? "s" : ""}</span></div>`
      + `<div class="lb-points">${row.points.toLocaleString()} pts</div>`
      + `</li>`;
  });
  // Pad with placeholders up to MAX_ROWS so the list is always 10 tall.
  for (let i = rows.length; i < MAX_ROWS; i++) {
    html += `<li class="placeholder">`
      + `<div class="lb-user">—<br><span class="gifts">0 gifts</span></div>`
      + `<div class="lb-points">0 pts</div>`
      + `</li>`;
  }
  lb.innerHTML = html;

  // Update previous scores (for bump detection next render)
  previousScores = {};
  data.leaderboard.forEach((r) => (previousScores[r.user] = r.points));
} catch (err) {
  // render error swallowed — display is best-effort
}
}

function escapeHtml(str) {
  return str.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

connect();
