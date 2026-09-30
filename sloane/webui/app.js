// Sloane's control room. No framework and nothing loaded from outside: the
// page is served with CSP 'self'. Everything shown that came from outside
// (assignment titles, calendar text, her replies) goes in as text, or through
// `markdown()`, which escapes first and only then adds a few safe tags.
"use strict";

const $ = (sel) => document.querySelector(sel);
const HEAD = { "X-Sloane": "1", "Content-Type": "application/json" };
const wide = window.matchMedia("(min-width: 1024px)");
const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
let clockState = { tz: undefined, minutes: 0, fetchedAt: Date.now() };
let latest = null;           // the last /api/overview
let shop = null;             // the last /api/workshop
let tomorrowShown = false;   // the timeline's Today / Tomorrow toggle
let forgetting = "";         // a learned fact whose "Forget?" is open

// -- small helpers ------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") {
      // CSSOM, not a style attribute: the page's CSP has no 'unsafe-inline'.
      for (const decl of value.split(";")) {
        const at = decl.indexOf(":");
        if (at > 0) node.style.setProperty(decl.slice(0, at).trim(), decl.slice(at + 1).trim());
      }
    }
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) if (child != null && child !== false) node.append(child);
  return node;
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Her detail is Markdown. Escaped first; then bold, code, links (http/https
// only), headings and bullets. Nothing she writes can become a tag.
function markdown(text) {
  const blocks = escapeHtml(text).split(/\n{2,}/);
  const inline = (s) => s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const item = /^\s*([-*•]|\d+\.)\s+/;
  return blocks.map((block) => {
    const lines = block.split("\n");
    if (/^\|.*\|$/.test(lines[0].trim())) {
      const rows = lines.filter((l) => !/^\|?\s*-{3,}/.test(l.trim()));
      return "<ul>" + rows.map((r) => "<li>" + inline(r.split("|").map((c) => c.trim()).filter(Boolean).join(" · ")) + "</li>").join("") + "</ul>";
    }
    // Runs of list lines become a list; the lines around them stay text.
    let out = "", text = [], list = [], ordered = false;
    const flushText = () => { if (text.length) out += "<p>" + text.join("<br>") + "</p>"; text = []; };
    const flushList = () => {
      if (list.length) out += (ordered ? "<ol>" : "<ul>") + list.map((l) => "<li>" + l + "</li>").join("") + (ordered ? "</ol>" : "</ul>");
      list = [];
    };
    for (const raw of lines) {
      const hit = raw.match(item);
      if (hit) {
        if (!list.length) { flushText(); ordered = /\d/.test(hit[1]); }
        list.push(inline(raw.replace(item, "")));
      } else {
        flushList();
        const heading = /^#{1,4}\s+/.test(raw);
        text.push(heading ? "<strong>" + inline(raw.replace(/^#{1,4}\s+/, "")) + "</strong>" : inline(raw));
      }
    }
    flushList();
    flushText();
    return out;
  }).join("");
}

function toast(message, bad = false) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.toggle("bad", bad);
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 4200);
}

function remember(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }
function recall(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } }

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (response.status === 401) {
    location.href = "/app/login";
    throw new Error("signed out");
  }
  return response;
}

async function post(path, body) {
  const response = await api(path, { method: "POST", headers: HEAD, body: JSON.stringify(body || {}) });
  let data = {};
  try { data = await response.json(); } catch { /* an empty body is fine */ }
  toast(data.message || (response.ok ? "Done." : "That didn't work."), !response.ok);
  refresh();
  return { ok: response.ok, ...data };
}

function button(label, onclick, cls = "btn", extra = {}) {
  return el("button", { type: "button", class: cls, text: label, onclick, ...extra });
}

// -- time, said plainly ----------------------------------------------------------------

function nowMinutes() {
  return clockState.minutes + Math.floor((Date.now() - clockState.fetchedAt) / 60000);
}

// 945 → "3:45", 1440 → "12:00": the timeline's times (the order says morning or night).
function short(minutes) {
  const m = Math.min(minutes, 1439 + 1) % 1440;
  const h = Math.floor(m / 60);
  return `${h % 12 || 12}:${String(m % 60).padStart(2, "0")}`;
}

// A span the way the header says it: "25m", "2h 08m", "3d 4h".
function hm(minutes) {
  minutes = Math.max(0, Math.round(minutes));
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
  return `${Math.floor(minutes / 1440)}d ${Math.floor((minutes % 1440) / 60)}h`;
}

function fromNow(iso) {
  if (!iso) return "";
  const minutes = (new Date(iso).getTime() - Date.now()) / 60000;
  if (Number.isNaN(minutes)) return "";
  if (minutes < -1) return `${hm(-minutes)} ago`;
  if (minutes < 1) return "now";
  return `in ${hm(minutes)}`;
}

// The local wall-clock minute of an ISO time, in her timezone.
function minuteOf(iso) {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", hourCycle: "h23", timeZone: clockState.tz })
      .formatToParts(new Date(iso));
    const get = (type) => Number(parts.find((p) => p.type === type)?.value || 0);
    return get("hour") * 60 + get("minute");
  } catch { return 0; }
}

function hourLabel(iso, previous) {
  const minute = minuteOf(iso);
  const h = Math.floor(minute / 60);
  const pm = h >= 12;
  const label = String(h % 12 || 12);
  return previous === undefined || previous !== pm ? [label + (pm ? "p" : "a"), pm] : [label, pm];
}

// -- the orb ---------------------------------------------------------------------------
// One function builds every orb, so each has its own gradient id: the gradient's
// stops use currentColor, and a shared id would paint every orb one colour.

let orbs = 0;
const RING = 2 * Math.PI * 35;  // the progress arc's circumference
const BARS = Array.from({ length: 36 }, (_, i) => {
  const a = i * 10 * Math.PI / 180, r1 = 25, r2 = 25 + (i % 3 === 0 ? 5 : i % 2 ? 3 : 4);
  return `<line x1="${(50 + r1 * Math.sin(a)).toFixed(2)}" y1="${(50 - r1 * Math.cos(a)).toFixed(2)}" x2="${(50 + r2 * Math.sin(a)).toFixed(2)}" y2="${(50 - r2 * Math.cos(a)).toFixed(2)}"/>`;
}).join("");

function orb(host) {
  const n = orbs++;
  host.innerHTML = `<span class="glow"></span><svg viewBox="0 0 100 100" aria-hidden="true" focusable="false">
    <defs><radialGradient id="g${n}" cx="42%" cy="38%" r="65%"><stop offset="0" stop-color="#fff" stop-opacity=".95"/><stop offset=".35" stop-color="currentColor" stop-opacity=".95"/><stop offset="1" stop-color="currentColor" stop-opacity=".15"/></radialGradient></defs>
    <g class="spin r1"><circle cx="50" cy="50" r="46" fill="none" stroke="currentColor" stroke-width=".8" stroke-dasharray=".7 3.3" opacity=".55"/></g>
    <g class="spin r2"><circle cx="50" cy="50" r="40" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="54 72" stroke-linecap="round" opacity=".8"/></g>
    <circle class="prog" cx="50" cy="50" r="35" fill="none" stroke="currentColor" stroke-width="2.4" stroke-dasharray="0 220" transform="rotate(-90 50 50)" stroke-linecap="round"/>
    <circle cx="50" cy="50" r="31" fill="none" stroke="currentColor" stroke-width=".6" opacity=".35"/>
    <g class="spin r3"><circle cx="50" cy="50" r="31" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="14 181" stroke-linecap="round"/></g>
    <g class="bars">${BARS}</g>
    <circle class="core" cx="50" cy="50" r="17" fill="url(#g${n})"/>
    <circle cx="50" cy="50" r="21" fill="none" stroke="currentColor" stroke-width=".5" opacity=".5"/>
  </svg>`;
  // Staggered, through CSSOM (a style attribute would need 'unsafe-inline').
  host.querySelectorAll(".bars line").forEach((line, i) => { line.style.animationDelay = `${(i * 0.07).toFixed(2)}s`; });
  host.dataset.state = "idle";
}

const ORB_STATES = ["idle", "listening", "thinking", "speaking", "building", "needs"];
// ?orb=<state> forces one, for review. What each says when forced and she isn't.
const PREVIEW = {
  idle: ["Idle", "Next check: Canvas at 2:00 PM"],
  listening: ["Listening", "Recording your voice note"],
  thinking: ["Thinking", "Working out tonight's plan"],
  speaking: ["Speaking", "Reading her reply aloud"],
  building: ["Building", "Workshop: “/ opens /app”, testing", 3],
  needs: ["Needs you", "A Workshop change is ready to accept", 5],
};
const forced = (() => {
  const asked = new URLSearchParams(location.search).get("orb");
  return ORB_STATES.includes(asked) ? asked : "";
})();
const doing = { server: { state: "idle", label: "Connecting", detail: "", step: null, ready: 0, waiting: 0 },
  listening: false, speaking: false, asking: false };

// His own states first (listening, speaking, a message in flight), then hers.
function currentOrb() {
  if (forced) {
    const s = doing.server;
    if (s.state === forced) return { ...s };
    const [label, detail, step] = PREVIEW[forced];
    return { state: forced, label, detail, step: step ?? null };
  }
  if (doing.listening) {
    return { state: "listening", label: "Listening", detail: call.on ? "On a call. Just talk" : "Recording your voice note" };
  }
  if (doing.speaking) return { state: "speaking", label: "Speaking", detail: call.on ? "Tap the orb to cut in" : "Saying her reply" };
  if (doing.asking) return { state: "thinking", label: "Thinking", detail: "Working on your message" };
  return doing.server;
}

function paintOrb() {
  const now = currentOrb();
  const arc = now.state === "building" && now.step ? (RING * now.step) / 5 : 0;
  document.body.dataset.orb = now.state;
  document.querySelectorAll(".js-orb").forEach((o) => {
    o.dataset.state = now.state;
    o.querySelector(".prog")?.setAttribute("stroke-dasharray", `${arc.toFixed(1)} 220`);
  });
  $("#status-label").textContent = now.label;
  $("#status-detail").textContent = now.detail || "";
  $("#status-short").textContent = now.label;
  $("#workshop-dot").hidden = !doing.server.ready;
}

let activityTimer = 0;
let lastActivity = "";
async function pollActivity() {
  clearTimeout(activityTimer);
  try {
    const response = await api("/api/activity");
    if (response.ok) {
      doing.server = await response.json();
      paintOrb();
      // The workshop moved: its panel and lanes follow.
      const key = `${doing.server.state}|${doing.server.step}|${doing.server.ready}|${doing.server.detail}`;
      if (key !== lastActivity) { lastActivity = key; loadWorkshop(); }
    }
  } catch { /* the next poll tries again */ }
  // Every 5 s while he's looking, every minute when the page is hidden.
  activityTimer = setTimeout(pollActivity, document.hidden ? 60000 : 5000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollActivity(); });

// -- views -----------------------------------------------------------------------------

const VIEWS = ["today", "memory", "workshop", "engine"];

function show(view) {
  if (view === "more") view = recall("sloane.more", "memory");
  if (wide.matches && view === "talk") {
    // On a desk the chat is a drawer beside every view: Talk opens it, and the view stays.
    openChat(true);
    view = document.body.dataset.view === "talk" ? "today" : document.body.dataset.view || "today";
  }
  document.body.dataset.view = view;
  for (const id of VIEWS) $("#" + id).hidden = id !== view;
  document.querySelectorAll(".nav-item").forEach((item) => {
    const mine = item.dataset.view === view || (item.dataset.view === "more" && (view === "memory" || view === "engine"));
    if (mine) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
  });
  document.querySelectorAll(".subnav .seg-b").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.go === view)));
  if (view === "memory" || view === "engine") remember("sloane.more", view);
  if (view === "workshop") loadWorkshop();
  if (view === "talk") scrollDown();
  if (view === "today" && latest) renderPanels();
  remember("sloane.view", view);
}

document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => show(item.dataset.view)));
document.querySelectorAll(".subnav .seg-b").forEach((b) => b.addEventListener("click", () => show(b.dataset.go)));
wide.addEventListener("change", () => {
  const view = document.body.dataset.view;
  show(wide.matches && view === "talk" ? "today" : view);
  if (latest) renderPanels();
});

// -- the chat drawer: open, closed, or in its own window ------------------------------------
// On a desk the chat sits on the right. He can close it (the widgets take the room), or pop it
// out into its own window and put it back. The two windows keep in step over a BroadcastChannel:
// the popped-out one says when it closes, and a widget's question goes to whichever is showing.

const SOLO = new URLSearchParams(location.search).get("chat") === "pop";
const talkLine = "BroadcastChannel" in window ? new BroadcastChannel("sloane-chat") : null;
let chatMode = "open";
let popup = null;

function setChat(mode, focus = false) {
  if (SOLO) return;
  const was = chatMode;
  chatMode = mode;
  remember("sloane.chat", mode);
  document.body.dataset.chat = mode;
  const toggle = $("#chat-toggle");
  toggle.setAttribute("aria-expanded", String(mode === "open"));
  $("#chat-toggle-label").textContent = mode === "popped" ? "Chat window" : "Chat";
  toggle.title = mode === "popped" ? "The chat is in its own window" : mode === "open" ? "Close the chat" : "Open the chat";
  if (mode === "open") {
    if (was === "popped") loadHistory();
    scrollDown();
    if (focus) input.focus();
  }
  requestAnimationFrame(layoutPanels);
}

function openChat(focus = false) {
  if (!wide.matches) { show("talk"); if (focus) input.focus(); return; }
  if (chatMode === "popped" && popup && !popup.closed) { popup.focus(); return; }
  setChat("open", focus);
}

function toggleChat() {
  if (chatMode === "open") setChat("closed");
  else openChat(true);
}

function popOut() {
  const w = 440;
  const h = Math.min(860, (screen.availHeight || 900) - 80);
  const left = Math.max(0, (screen.availWidth || 1440) - w - 32);
  popup = window.open("/app?chat=pop", "sloane-chat", `popup=yes,width=${w},height=${h},left=${left},top=48`);
  if (!popup) { toast("The browser blocked the window. Allow pop-ups for this page, then try again.", true); return; }
  if (call.on) endCall();
  hush();
  setChat("popped");
  watchPopup();
}

// The popped-out window closed (or was put back): the chat comes home.
function watchPopup() {
  clearInterval(watchPopup.timer);
  watchPopup.timer = setInterval(() => {
    if (popup && !popup.closed) return;
    clearInterval(watchPopup.timer);
    popup = null;
    if (chatMode === "popped") setChat("open");
  }, 800);
}

// A question from a widget: into whichever chat he can see.
function askHer(text) {
  if (!SOLO && wide.matches && chatMode === "popped" && talkLine) {
    talkLine.postMessage({ t: "say", text });
    if (popup && !popup.closed) popup.focus();
    return;
  }
  openChat();
  say(text);
}

if (talkLine) {
  talkLine.addEventListener("message", (event) => {
    const m = event.data || {};
    if (SOLO) {
      if (m.t === "say" && typeof m.text === "string") { window.focus(); say(m.text); }
      if (m.t === "ping") talkLine.postMessage({ t: "here" });
      if (m.t === "close") window.close();
      return;
    }
    if (m.t === "dock" || m.t === "closed") { popup = null; if (chatMode === "popped") setChat("open", m.t === "dock"); }
    if (m.t === "here" && chatMode === "popped") setChat.confirmed = true;
    if (m.t === "opened" && chatMode !== "popped") { if (call.on) endCall(); hush(); setChat("popped"); }
  });
}

// Called once everything below exists (the thread, the message box).
function initChat() {
  if (SOLO) {
    document.body.classList.add("solo");
    document.title = "Sloane · Chat";
    $("#chat-dock").hidden = false;
    $("#chat-dock").addEventListener("click", () => { talkLine?.postMessage({ t: "dock" }); window.close(); });
    window.addEventListener("pagehide", () => talkLine?.postMessage({ t: "closed" }));
    talkLine?.postMessage({ t: "opened" });
  } else {
    const kept = recall("sloane.chat", "open");
    if (kept === "popped") {
      // Reloaded while the chat was popped out: is that window still there?
      setChat("popped");
      setChat.confirmed = false;
      talkLine?.postMessage({ t: "ping" });
      setTimeout(() => { if (!setChat.confirmed && chatMode === "popped") setChat("open"); }, 700);
    } else {
      setChat(kept === "closed" ? "closed" : "open");
    }
  }
}

$("#chat-toggle").addEventListener("click", toggleChat);
$("#chat-close").addEventListener("click", () => setChat("closed"));
$("#chat-pop").addEventListener("click", popOut);

// -- the conversation ------------------------------------------------------------------

const thread = $("#thread");
const input = $("#composer-input");
const sendButton = $("#send");
const readAloud = $("#read-aloud");

function scrollDown() { thread.scrollTop = thread.scrollHeight; }

function hisMessage(text, { when = "", forwarded = false, spoken = false, pending = false } = {}) {
  const item = el("li", { class: `msg him${pending ? " pending" : ""}` },
    when ? el("span", { class: "when", text: when }) : null,
    el("span", { class: "body", text }),
    spoken ? el("span", { class: "tag", text: "voice" }) : null,
    forwarded ? el("span", { class: "tag", text: "forwarded" }) : null);
  thread.append(item);
  scrollDown();
  return item;
}

// What she did for him rides at the end of her detail, a line each ("→ …", "✗ …"):
// shown apart, in mono, as receipts.
function receipts(detail) {
  const lines = String(detail || "").replace(/\s+$/, "").split("\n");
  const done = [];
  while (lines.length && /^\s*(→|✗)\s/.test(lines[lines.length - 1])) done.unshift(lines.pop().trim());
  return { detail: lines.join("\n").trim(), done };
}

function herMessage({ speech = "", detail = "", when = "", outside = false, cls = "" } = {}) {
  const item = el("li", { class: "msg her " + cls });
  if (when) item.append(el("span", { class: "when", text: when }));
  const said = el("p", { class: "speech", text: speech });
  if (outside) said.append(el("span", { class: "tag", text: "from outside text" }));
  item.append(said);
  const parts = receipts(detail);
  if (parts.detail) {
    const more = el("div", { class: "detail" });
    more.innerHTML = markdown(parts.detail);
    item.append(more);
  }
  for (const line of parts.done) {
    const bad = line.startsWith("✗");
    item.append(el("div", { class: `act${bad ? " bad" : ""}`, text: line.replace(/^(→|✗)\s*/, bad ? "not done · " : "") }));
  }
  thread.append(item);
  scrollDown();
  return item;
}

function splitLogged(text) {
  // The log keeps speech, a blank line, then detail (when it adds something).
  const at = text.indexOf("\n\n");
  return at < 0 ? { speech: text, detail: "" } : { speech: text.slice(0, at), detail: text.slice(at + 2) };
}

async function loadHistory() {
  let response;
  try { response = await api("/api/history"); } catch { return; }
  if (!response.ok) return;
  const rows = await response.json();
  thread.replaceChildren();
  for (const row of rows) {
    if (row.from === "him") hisMessage(row.text, { when: row.when, forwarded: row.forwarded, spoken: row.voice });
    else herMessage({ ...splitLogged(row.text), when: row.when, outside: row.outside });
  }
  if (!rows.length) herMessage({ speech: "Ask me anything, or tell me what needs doing.", cls: "hello" });
}

function grow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
}
input.addEventListener("input", grow);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});

// -- her voice: her own (Piper on the box), sentence by sentence as she writes -------------------
// Each finished sentence is sent to /api/speak while the next is still being written,
// so she starts talking a moment after she starts answering. When the server has no
// voice, the browser's own reads it instead (it never leaves the device).

const SERVER_RETRY = 5 * 60 * 1000;
const voice = {
  el: null,          // the one <audio> she speaks through (Safari lets it play later only if a tap started it)
  context: null,     // for the orb's bars to follow her voice
  levels: null,
  queue: [],         // sentences waiting: { text, audio: Promise<Blob|null> }
  chain: Promise.resolve(),
  run: 0,            // bumped by hush(): anything from an older run stops
  running: false,
  stop: null,        // ends the sentence playing now
  said: "",          // how much of the current reply's speech is queued
  serverDown: 0,     // when /api/speak last failed
};

function voiceOn() { return call.on || readAloud.getAttribute("aria-pressed") === "true"; }

// A tenth of a second of silence, as a WAV: what the first tap plays to unlock her voice.
function silence() {
  const rate = 8000, samples = 800, bytes = new DataView(new ArrayBuffer(44 + samples * 2));
  const text = (at, s) => [...s].forEach((c, i) => bytes.setUint8(at + i, c.charCodeAt(0)));
  text(0, "RIFF"); bytes.setUint32(4, 36 + samples * 2, true); text(8, "WAVEfmt ");
  bytes.setUint32(16, 16, true); bytes.setUint16(20, 1, true); bytes.setUint16(22, 1, true);
  bytes.setUint32(24, rate, true); bytes.setUint32(28, rate * 2, true); bytes.setUint16(32, 2, true);
  bytes.setUint16(34, 16, true); text(36, "data"); bytes.setUint32(40, samples * 2, true);
  return new Blob([bytes], { type: "audio/wav" });
}

// Called on his taps and keys: browsers only let a page make sound after one.
function primeAudio() {
  if (!voice.el) {
    voice.el = new Audio();
    voice.el.src = URL.createObjectURL(silence());
    voice.el.play().catch(() => {});
  }
  if (voice.context) {
    if (voice.context.state === "suspended") voice.context.resume().catch(() => {});
    return;
  }
  if (calm.matches) return;
  try {
    const Context = window.AudioContext || window.webkitAudioContext;
    const context = new Context();
    // Only a running context may carry her voice: a suspended one would silence it.
    if (context.state !== "running") { context.close().catch(() => {}); return; }
    const levels = context.createAnalyser();
    levels.fftSize = 128;
    context.createMediaElementSource(voice.el).connect(levels);
    levels.connect(context.destination);
    Object.assign(voice, { context, levels });
  } catch { /* the CSS pulse instead */ }
}
document.addEventListener("pointerdown", primeAudio, true);
document.addEventListener("keydown", primeAudio, true);

function hush() {
  voice.run += 1;
  voice.queue = [];
  voice.chain = Promise.resolve();
  voice.said = "";
  if (voice.stop) voice.stop();
  if (voice.el) voice.el.pause();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  voice.running = false;
  doing.speaking = false;
  unfollow(null);
  paintOrb();
}

// The end of the last whole sentence in `text`, once there's enough to be worth a breath.
function sentenceEnd(text) {
  let at = 0;
  const ends = /[.!?…]+["')\]]*\s+/g;
  for (let m; (m = ends.exec(text));) at = m.index + m[0].length;
  return text.slice(0, at).trim().length >= 12 ? at : 0;
}

// Her speech so far (a partial) or all of it (the reply): queue what's new, whole sentences only.
function speakAlong(speech, final = false) {
  if (!voiceOn() || !speech) return;
  if (!speech.startsWith(voice.said)) {
    // The reply isn't what she was writing (it changed at the end): say it only if nothing was said yet.
    if (voice.said) return;
  }
  const rest = speech.slice(voice.said.length);
  const cut = final ? rest.length : sentenceEnd(rest);
  if (!cut) return;
  voice.said += rest.slice(0, cut);
  // Long sentences in pieces the server takes (600 characters), at a space.
  let chunk = rest.slice(0, cut).trim();
  while (chunk) {
    let piece = chunk;
    if (piece.length > 560) piece = piece.slice(0, piece.lastIndexOf(" ", 560) > 200 ? piece.lastIndexOf(" ", 560) : 560);
    enqueue(piece);
    chunk = chunk.slice(piece.length).trim();
  }
}

function enqueue(text) {
  const run = voice.run;
  // One at a time, in order: the next is made while this one plays.
  const audio = voice.chain.then(() => (run === voice.run ? fetchVoice(text) : null));
  voice.chain = audio.catch(() => null);
  voice.queue.push({ text, audio });
  if (!voice.running) playQueue();
}

async function fetchVoice(text) {
  if (Date.now() - voice.serverDown < SERVER_RETRY) return null;
  try {
    const response = await api("/api/speak", { method: "POST", headers: HEAD, body: JSON.stringify({ text }) });
    if (!response.ok) throw new Error(String(response.status));
    return await response.blob();
  } catch (error) {
    if (error.message === "signed out") throw error;
    voice.serverDown = Date.now();
    return null;
  }
}

async function playQueue() {
  const run = voice.run;
  voice.running = true;
  while (voice.queue.length && run === voice.run) {
    let blob = null;
    try { blob = await voice.queue[0].audio; } catch { /* signed out */ }
    if (run !== voice.run) return;
    const { text } = voice.queue.shift();
    doing.speaking = true;
    paintOrb();
    if (blob) await playBlob(blob, run);
    else await browserSay(text);
  }
  if (run !== voice.run) return;
  voice.running = false;
  doing.speaking = false;
  unfollow(null);
  paintOrb();
  quieted();
}

function playBlob(blob, run) {
  return new Promise((resolve) => {
    const el = voice.el || (voice.el = new Audio());
    const url = URL.createObjectURL(blob);
    let hear = null;
    // Never stuck on a sentence: a call waits for her to finish before it listens again.
    const guard = setTimeout(() => { el.pause(); done(); }, 60000);
    const done = () => {
      clearTimeout(guard);
      el.removeEventListener("ended", done);
      el.removeEventListener("error", done);
      if (hear) cancelAnimationFrame(hear.frame);
      URL.revokeObjectURL(url);
      if (voice.stop === done) voice.stop = null;
      resolve();
    };
    voice.stop = done;
    el.addEventListener("ended", done);
    el.addEventListener("error", done);
    el.src = url;
    el.play().then(() => {
      if (run === voice.run && voice.levels) hear = barsFrom(voice.levels);
    }).catch(() => { done(); });
  });
}

function browserSay(text) {
  return new Promise((resolve) => {
    if (!("speechSynthesis" in window)) { resolve(); return; }
    const words = new SpeechSynthesisUtterance(text);
    words.lang = latest?.talk?.lang || "en-US";
    const chosen = speechSynthesis.getVoices().find((v) => v.lang.replace("_", "-") === words.lang);
    if (chosen) words.voice = chosen;
    // Some browsers never say "end" (no voices installed): give up after about as long as it takes to say.
    const guard = setTimeout(() => { speechSynthesis.cancel(); done(); }, 2500 + text.length * 90);
    const done = () => { clearTimeout(guard); if (voice.stop === done) voice.stop = null; resolve(); };
    voice.stop = done;
    for (const over of ["end", "error"]) words.addEventListener(over, done);
    speechSynthesis.speak(words);
  });
}

// The orb's bars following an analyser: his voice while he talks, hers while she does.
function barsFrom(analyser) {
  const bins = new Uint8Array(analyser.frequencyBinCount);
  const live = document.querySelectorAll(".js-orb");
  live.forEach((o) => o.classList.add("live"));
  const hear = { frame: 0 };
  const frame = () => {
    analyser.getByteFrequencyData(bins);
    live.forEach((o) => o.querySelectorAll(".bars line").forEach((line, i) => {
      // Symmetric around the top: low voices near 12 o'clock, higher ones down the sides.
      const bin = 1 + Math.floor((Math.abs(18 - i) / 18) * 20);
      line.style.transform = `scale(${(0.9 + (bins[bin] / 255) * 0.32).toFixed(3)})`;
    }));
    hear.frame = requestAnimationFrame(frame);
  };
  frame();
  return hear;
}

readAloud.setAttribute("aria-pressed", recall("sloane.readAloud", "false"));
readAloud.addEventListener("click", () => {
  const on = readAloud.getAttribute("aria-pressed") !== "true";
  readAloud.setAttribute("aria-pressed", String(on));
  remember("sloane.readAloud", String(on));
  if (!on && !call.on) hush();
  toast(on ? "She'll say her replies out loud." : "Reading aloud is off.");
});

// One exchange: his message (typed, or a voice note), her reply streamed in.
async function exchange(request, mine) {
  if (sendButton.disabled) return;
  sendButton.disabled = true;
  hush();
  doing.asking = true;
  paintOrb();
  let draft = null;
  const place = () => draft || (draft = herMessage({ speech: "Thinking…", cls: "thinking" }));
  try {
    const response = await request();
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      if (mine.classList.contains("pending")) mine.remove();
      // On a call, nothing heard (a cough, the room) isn't worth a message: she just keeps listening.
      if (call.on && response.status === 422) { if (draft) draft.remove(); draft = null; return; }
      place().replaceWith(herMessage({ speech: data.error || "That didn't go through.", cls: "error" }));
      draft = null;
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.t === "heard") {
          mine.classList.remove("pending");
          mine.querySelector(".body").textContent = event.text;
          place();
        } else if (event.t === "typing") {
          place();
        } else if (event.t === "partial") {
          const node = place();
          node.className = "msg her draft";
          node.querySelector(".speech").textContent = event.text;
          scrollDown();
          // Speech, then a blank line, then detail: once the detail starts, the speech is whole.
          const cut = event.text.indexOf("\n\n");
          speakAlong(cut < 0 ? event.text : event.text.slice(0, cut), cut >= 0);
        } else if (event.t === "reply") {
          const finished = herMessage({ speech: event.speech, detail: event.detail, outside: event.outside });
          if (draft) { draft.replaceWith(finished); draft = null; }
          speakAlong(event.speech, true);
        } else if (event.t === "error") {
          const failed = herMessage({ speech: event.text, cls: "error" });
          if (draft) { draft.replaceWith(failed); draft = null; }
          if (call.on) speakAlong(event.text, true);
        }
      }
    }
    if (draft) draft.remove();
  } catch (error) {
    if (draft) draft.remove();
    if (error.message !== "signed out") herMessage({ speech: "Lost the connection. Try that again.", cls: "error" });
  } finally {
    sendButton.disabled = false;
    doing.asking = false;
    paintOrb();
    refresh();
  }
}

async function say(text) {
  text = text.trim();
  if (!text || sendButton.disabled) return;
  input.value = "";
  grow();
  const mine = hisMessage(text);
  await exchange(() => api("/api/chat", { method: "POST", headers: HEAD, body: JSON.stringify({ text }) }), mine);
  if (wide.matches || document.body.dataset.view === "talk") input.focus();
}

$("#composer").addEventListener("submit", (event) => { event.preventDefault(); say(input.value); });

// -- the microphone: tap to talk, tap to send ----------------------------------------------

const mic = $("#mic");
const MAX_RECORDING = 110 * 1000;
let recording = null;

function canRecord() {
  return window.isSecureContext && !!navigator.mediaDevices?.getUserMedia && "MediaRecorder" in window;
}

function recordingType() {
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

function recordingState(on) {
  mic.setAttribute("aria-pressed", String(on));
  mic.setAttribute("aria-label", on ? "Stop and send the voice message" : "Record a voice message");
  $("#composer").classList.toggle("recording", on);
  input.placeholder = on ? "Listening… tap the mic to send, Esc to cancel" : "Message Sloane";
  input.disabled = on;
  doing.listening = on;
  paintOrb();
}

// While he talks, the orb's bars follow his voice. Without an analyser, the CSS pulse.
function follow(stream) {
  if (calm.matches) return null;
  try {
    const Context = window.AudioContext || window.webkitAudioContext;
    const context = new Context();
    const analyser = context.createAnalyser();
    analyser.fftSize = 128;
    context.createMediaStreamSource(stream).connect(analyser);
    const hear = barsFrom(analyser);
    hear.context = context;
    return hear;
  } catch {
    return null;
  }
}

function unfollow(hear) {
  document.querySelectorAll(".js-orb").forEach((o) => {
    o.classList.remove("live");
    o.querySelectorAll(".bars line").forEach((line) => { line.style.transform = ""; });
  });
  if (!hear) return;
  cancelAnimationFrame(hear.frame);
  hear.context?.close().catch(() => {});
}

async function startRecording() {
  if (call.on) endCall();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast("The microphone is blocked. Allow it for this page in the browser's settings.");
    return;
  }
  hush();
  const type = recordingType();
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : {});
  const chunks = [];
  recording = { recorder, stream, cancelled: false, hear: follow(stream), timer: setTimeout(() => stopRecording(), MAX_RECORDING) };
  recorder.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
  recorder.addEventListener("stop", () => {
    const done = recording;
    recording = null;
    clearTimeout(done.timer);
    unfollow(done.hear);
    stream.getTracks().forEach((track) => track.stop());
    recordingState(false);
    if (done.cancelled) { toast("Voice message cancelled."); return; }
    const kind = (recorder.mimeType || type || "audio/webm").split(";")[0];
    const blob = new Blob(chunks, { type: kind });
    if (blob.size < 800) { toast("That was too short to hear."); return; }
    const mine = hisMessage("Listening back…", { spoken: true, pending: true });
    exchange(() => api("/api/voice", { method: "POST", headers: { "X-Sloane": "1", "Content-Type": kind }, body: blob }), mine);
  });
  recorder.start();
  recordingState(true);
}

function stopRecording(cancel = false) {
  if (!recording) return;
  recording.cancelled = cancel;
  recording.recorder.stop();
}

mic.addEventListener("click", () => (recording ? stopRecording() : startRecording()));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && recording) { event.preventDefault(); stopRecording(true); }
});

// -- a call: hands-free, back and forth ------------------------------------------------------------
// He talks; when he stops (a short silence), what he said goes to her like a voice note,
// and she answers out loud, sentence by sentence. Then she listens again. The microphone
// is off while she thinks and talks (so she never hears herself); tap the orb to cut
// her off, Esc or End to hang up.

const callButton = $("#call");
const callbar = $("#callbar");
const FRAME_MS = 40;         // how often the level is read
const START_MS = 160;        // this long above the line: he's talking
const END_MS = 850;          // this long below it: he's finished
const MIN_TALK_MS = 300;     // less than this is a cough, not a sentence
const MAX_TURN_MS = 45000;   // one turn at most
const RECYCLE_MS = 8000;     // this long without a word: start the recording over (it stays small)
const IDLE_HANGUP_MS = 4 * 60 * 1000;
const call = { on: false, phase: "off", stream: null, context: null, analyser: null, samples: null, timer: 0,
  recorder: null, chunks: [], since: 0, floor: 0.006, voiced: 0, quiet: 0, talked: 0, heardAt: 0,
  pending: false, calmSince: 0, lastWords: 0, hear: null };

function callPhase(phase) {
  call.phase = phase;
  doing.listening = call.on && (phase === "listening" || phase === "hearing");
  const shown = phase === "waiting" ? (doing.speaking ? "speaking" : "thinking") : phase;
  if (callbar.dataset.phase === shown) return;
  callbar.dataset.phase = shown;
  const [words, hint] = {
    listening: ["Listening", "just talk"], hearing: ["Hearing you", "pause when you're done"],
    thinking: ["Thinking", ""], speaking: ["Speaking", "tap the orb to cut in"], off: ["", ""],
  }[shown];
  $("#call-state").textContent = words;
  $("#call-hint").textContent = hint;
  paintOrb();
}

function record() {
  if (call.recorder && call.recorder.state !== "inactive") {
    call.recorder.ondataavailable = null;
    call.recorder.onstop = null;
    call.recorder.stop();
  }
  const type = recordingType();
  const recorder = new MediaRecorder(call.stream, type ? { mimeType: type } : {});
  call.chunks = [];
  recorder.ondataavailable = (event) => { if (event.data.size) call.chunks.push(event.data); };
  recorder.start();
  Object.assign(call, { recorder, since: Date.now(), voiced: 0, quiet: 0, talked: 0 });
}

function listen() {
  if (!call.on) return;
  record();
  if (call.hear) cancelAnimationFrame(call.hear.frame);
  call.hear = calm.matches ? null : barsFrom(call.analyser);
  callPhase("listening");
}

function stopListening() {
  if (call.hear) { cancelAnimationFrame(call.hear.frame); call.hear = null; unfollow(null); }
}

// He's finished a sentence: send what was recorded, the same way a voice note goes.
function sendTurn() {
  const recorder = call.recorder;
  call.recorder = null;
  call.pending = true;
  stopListening();
  callPhase("waiting");
  const talked = call.talked;
  recorder.onstop = () => {
    const kind = (recorder.mimeType || recordingType() || "audio/webm").split(";")[0];
    const blob = new Blob(call.chunks, { type: kind });
    if (!call.on || talked < MIN_TALK_MS || blob.size < 800) { call.pending = false; return; }
    call.lastWords = Date.now();
    const mine = hisMessage("Listening back…", { spoken: true, pending: true });
    exchange(() => api("/api/voice", { method: "POST", headers: { "X-Sloane": "1", "Content-Type": kind }, body: blob }), mine)
      .finally(() => { call.pending = false; });
  };
  recorder.stop();
}

function level() {
  call.analyser.getFloatTimeDomainData(call.samples);
  let sum = 0;
  for (const x of call.samples) sum += x * x;
  return Math.sqrt(sum / call.samples.length);
}

function callFrame() {
  if (!call.on) return;
  const busy = call.pending || doing.asking || voice.running || voice.queue.length > 0;
  if (busy) {
    call.calmSince = 0;
    if (call.phase === "listening" || call.phase === "hearing") {
      // Something else started (a typed message, a reminder read out): stop listening meanwhile.
      if (call.recorder) { call.recorder.onstop = null; call.recorder.stop(); call.recorder = null; }
      stopListening();
    }
    callPhase("waiting");
    return;
  }
  if (call.phase === "waiting") {
    // A breath after she stops, so the tail of her voice isn't taken for his.
    call.calmSince ||= Date.now();
    if (Date.now() - call.calmSince >= 300) listen();
    return;
  }
  const now = Date.now();
  const heard = level();
  const line = Math.max(0.012, call.floor * 3);
  if (call.phase === "listening") {
    if (heard > line) {
      call.voiced += FRAME_MS;
      if (call.voiced >= START_MS) { call.heardAt = now; call.quiet = 0; call.talked = call.voiced; callPhase("hearing"); }
    } else {
      call.voiced = 0;
      // The room's own noise, followed slowly: a fan is not a voice.
      call.floor = heard < call.floor ? call.floor * 0.9 + heard * 0.1 : call.floor * 0.98 + heard * 0.02;
      if (now - call.since > RECYCLE_MS) record();
      if (now - Math.max(call.lastWords, call.startedAt) > IDLE_HANGUP_MS) { endCall(); toast("Hung up after a few quiet minutes."); }
    }
  } else if (call.phase === "hearing") {
    if (heard > line * 0.7) { call.quiet = 0; call.talked += FRAME_MS; } else call.quiet += FRAME_MS;
    if (call.quiet >= END_MS || now - call.heardAt > MAX_TURN_MS) sendTurn();
  }
}

function quieted() {
  if (call.on) callFrame();
}

async function startCall() {
  if (call.on) return;
  if (recording) stopRecording(true);
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch {
    toast("The microphone is blocked. Allow it for this page in the browser's settings.");
    return;
  }
  try {
    const Context = window.AudioContext || window.webkitAudioContext;
    const context = new Context();
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    context.createMediaStreamSource(stream).connect(analyser);
    Object.assign(call, { on: true, stream, context, analyser, samples: new Float32Array(analyser.fftSize),
      floor: 0.006, startedAt: Date.now(), lastWords: 0, pending: false });
  } catch {
    stream.getTracks().forEach((track) => track.stop());
    toast("This browser can't listen hands-free. Use the mic button instead.");
    return;
  }
  hush();
  if (!wide.matches) show("talk");
  else if (!SOLO && chatMode !== "open") setChat("open");
  callButton.setAttribute("aria-pressed", "true");
  callButton.querySelector("span").textContent = "End";
  document.body.classList.add("calling");
  callbar.hidden = false;
  input.placeholder = "On a call. Or type";
  call.timer = setInterval(callFrame, FRAME_MS);
  listen();
}

function endCall() {
  if (!call.on) return;
  call.on = false;
  clearInterval(call.timer);
  if (call.recorder) { call.recorder.onstop = null; call.recorder.stop(); call.recorder = null; }
  stopListening();
  call.stream.getTracks().forEach((track) => track.stop());
  call.context.close().catch(() => {});
  if (readAloud.getAttribute("aria-pressed") !== "true") hush();
  callButton.setAttribute("aria-pressed", "false");
  callButton.querySelector("span").textContent = "Talk";
  document.body.classList.remove("calling");
  callbar.hidden = true;
  input.placeholder = "Message Sloane";
  callPhase("off");
}

// Tap her while she's talking: she stops, and listens.
function cutIn() {
  if (!call.on) return;
  if (voice.running || voice.queue.length) { hush(); callFrame(); }
}

callButton.addEventListener("click", () => (call.on ? endCall() : startCall()));
$("#call-end").addEventListener("click", endCall);
$(".chat-head .js-orb").addEventListener("click", cutIn);
$("#orb-talk").addEventListener("click", () => {
  if (call.on) { cutIn(); return; }
  // The chat is in its own window: a call happens in this one, so it comes home first.
  if (chatMode === "popped") { popup?.close(); popup = null; talkLine?.postMessage({ t: "close" }); setChat("open"); }
  startCall();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && call.on && !recording) { event.preventDefault(); endCall(); }
});

// "/" goes to the message box from anywhere that isn't already a field.
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || document.activeElement?.isContentEditable) return;
  event.preventDefault();
  openChat(true);
});

// What to ask, from what's actually going on: his day, his inbox, his body, the world.
function renderStarters(data) {
  const now = nowMinutes();
  const items = data.agenda?.today?.items || [];
  const skills = new Set(data.system?.skills || []);
  const asks = [];
  if (items.some((i) => i.kind === "shift" && i.end > now)) asks.push("Plan my evening around work");
  else asks.push(now < 12 * 60 ? "What's my day look like?" : "Help me plan tonight");
  if (data.inbox?.waiting) asks.push("Anything in my inbox I need to reply to?");
  if (skills.has("whoop")) asks.push("How did I sleep?");
  if (skills.has("markets")) asks.push("How's the market?");
  if (skills.has("news")) asks.push("What's in the news?");
  asks.push("/reminders", "What do you remember about me?");
  const box = $("#starters");
  const key = asks.join("|");
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren(...asks.map((text) => el("button", { type: "button", class: "chip", text, onclick: () => say(text) })));
}

// -- the header: hello, her line on the day, the clock, what's waiting -------------------------

function tick() {
  let text;
  try {
    const parts = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: clockState.tz }).formatToParts(new Date());
    const part = (type) => parts.find((p) => p.type === type)?.value || "";
    text = `${part("hour")}:${part("minute")} ${part("dayPeriod")}`.trim();
  } catch {
    const d = new Date();
    text = `${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, "0")} ${d.getHours() < 12 ? "AM" : "PM"}`;
  }
  const clock = $("#clock");
  if (clock.textContent !== text) clock.textContent = text;
}

function panelOf(skill) {
  return (latest?.panels || []).find((p) => p.skill === skill) || null;
}

function jobLabel(item) {
  return item.kind === "shift" ? "Work" : item.title.replace(/^Focus: /, "");
}

// A title cut to fit a line: "What was your biggest struggle…".
function clip(text, most = 28) {
  text = String(text || "");
  return text.length > most ? text.slice(0, most - 1).trimEnd() + "…" : text;
}

// The countdown in her line: work first (on it, or the next shift today), else what's next, else what's on.
// Real things only: shifts, events, a focus session he started. Her plan's suggested stretches are on
// the agenda, not here (the countdown once read "What was your biggest struggle… in 4m").
function nextUp(data) {
  const now = nowMinutes();
  const blocks = (data.agenda?.today?.items || []).filter((i) => !i.all_day
    && (i.kind === "shift" || i.kind === "event" || (i.kind === "focus" && i.sub === "running now")));
  const on = (i) => i.start <= now && now < i.end;
  const shiftNow = blocks.find((i) => i.kind === "shift" && on(i));
  const shiftNext = blocks.find((i) => i.kind === "shift" && i.start > now);
  const next = blocks.find((i) => i.start > now);
  const current = blocks.find(on);
  if (shiftNow) return { text: `At work until ${short(shiftNow.end)}`, work: true };
  if (shiftNext) return { text: `Work in ${hm(shiftNext.start - now)}`, work: true };
  if (next) return { text: `${clip(jobLabel(next))} in ${hm(next.start - now)}`, work: false };
  if (current) return { text: `${clip(jobLabel(current))} until ${short(current.end)}`, work: false };
  return null;
}

function greeting(minutes) {
  if (minutes < 4 * 60) return "Up late";
  if (minutes < 12 * 60) return "Good morning";
  if (minutes < 17 * 60) return "Good afternoon";
  return "Good evening";
}

// Her line on the day, from what's actually going on: what's next, who's waiting on a reply, his body.
function renderHeader(data) {
  $("#today-title").textContent = `${greeting(nowMinutes())}, ${data.name || "Landen"}`;
  const line = $("#brief");
  const parts = [];
  const next = nextUp(data);
  if (next) parts.push(el("b", { class: next.work ? "work" : "", text: `${next.text}.` }));
  const mail = data.inbox?.waiting || 0;
  if (mail) parts.push(`${mail} email${mail === 1 ? " needs" : "s need"} a reply.`);
  const whoop = panelOf("whoop");
  if (whoop?.recovery != null) parts.push(`Recovery ${whoop.recovery}%${whoop.zone ? `, ${whoop.zone}` : ""}.`);
  const reminders = (data.agenda?.today?.items || []).filter((i) => i.kind === "reminder" && i.start > nowMinutes()).length;
  if (reminders) parts.push(`${reminders} reminder${reminders === 1 ? "" : "s"} left today.`);
  if (!parts.length) parts.push("Nothing pressing right now.");
  line.replaceChildren(...parts.flatMap((p, i) => (i ? [" ", p] : [p])));
  line.title = line.textContent;

  $("#date").textContent = data.now.date;
  const weather = panelOf("weather");
  $("#weather-short").textContent = weather && weather.temp != null ? `${Math.round(weather.temp)}° ${weather.sky || ""}`.trim() : "";
  const waiting = needsCount();
  const open = $("#waiting-open");
  open.hidden = !waiting;
  open.textContent = `${waiting} waiting on you`;
}

// -- what's waiting on him: approvals, what's broken, builds to accept; overdue work apart ---------

const JOBS = {
  entity_sync: "Canvas and calendar sync", morning_brief: "Morning brief", pre_shift: "Before your shift",
  post_shift: "After your shift", wrap: "Night wrap", reflection: "Reflection", reminders: "Reminders",
  heartbeat: "Heartbeat", inbox: "Inbox triage", learn: "Nightly learning", backup: "Backup", watchdog: "Watchdog",
  weekly_review: "Weekly review", think: "Thinking", workshop: "Workshop night shift",
};
function jobName(name) { return JOBS[name] || name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()); }

function readyBuilds() { return (shop?.items || []).filter((i) => i.status === "ready"); }
const OVERDUE_SHOWN = 12;

// What needs him to act: an approval, something broken, a failed job, a build to accept. Overdue
// work is counted on its own: with every missed Canvas item in it, this read "55 waiting on you".
function needsCount() {
  const data = latest;
  if (!data) return 0;
  return (data.proposals || []).length + (data.alerts || []).length + (data.unreadable || []).length
    + (data.jobs || []).filter((j) => j.status === "failed").length + (readyBuilds().length ? 1 : 0);
}

function renderNeeds() {
  const data = latest;
  if (!data) return;
  const list = $("#needs-list");
  list.replaceChildren();
  const row = (kind, text, sub, actions = [], cls = "") => el("li", {},
    el("span", { class: "text" }, el("span", { class: `kind ${cls}`, text: kind }), text,
      sub ? el("span", { class: "sub", text: sub }) : null),
    actions.length ? el("span", { class: "acts" }, ...actions) : null);
  const ready = readyBuilds();
  if (ready.length) {
    list.append(row("Workshop", `${ready.length} build${ready.length === 1 ? "" : "s"} ready for you`,
      ready.map((i) => i.title).slice(0, 3).join(", "), [button("Review", () => { closeWaiting(); show("workshop"); }, "btn primary")], "her"));
  }
  for (const p of data.proposals || []) {
    list.append(row(p.status === "editing" ? "Being edited" : "Needs your OK", p.preview, "", [
      button("Approve", () => post(`/api/proposals/${p.id}/approve`), "btn primary"),
      button("Deny", () => post(`/api/proposals/${p.id}/deny`), "btn danger")]));
  }
  for (const a of data.alerts || []) list.append(row("Broken", a.message, "", [], "bad"));
  for (const name of data.unreadable || []) list.append(row("Couldn't read", name, "Part of this page didn't load.", [], "bad"));
  for (const job of (data.jobs || []).filter((j) => j.status === "failed")) {
    list.append(row("Job failed", jobName(job.name), job.error,
      [button("Run again", () => post(`/api/jobs/${job.name}/run`), "btn", { "aria-label": `Run ${jobName(job.name)} again` })], "bad"));
  }
  if (!list.children.length) list.append(el("li", {}, el("span", { class: "empty", text: "Nothing needs you." })));

  // Overdue, most recent first (the ones still worth saving), a dozen at most.
  const overdue = data.overdue || [];
  const late = $("#overdue-list");
  late.replaceChildren(...overdue.slice(0, OVERDUE_SHOWN).map((a) => el("li", {},
    el("span", { class: "text" }, a.title, el("span", { class: "sub", text: [a.course, a.when].filter(Boolean).join(" · ") })))));
  if (overdue.length > OVERDUE_SHOWN) {
    late.append(el("li", {}, el("span", { class: "empty", text: `And ${overdue.length - OVERDUE_SHOWN} older. Handed one in on paper? Tell her: /done and its name.` })));
  }
  $("#overdue-count").textContent = overdue.length ? String(overdue.length) : "";
  $("#overdue-wrap").hidden = !overdue.length;

  const due = $("#due-list");
  due.replaceChildren();
  for (const a of data.due || []) {
    due.append(el("li", {},
      el("span", { class: "text" }, a.title, a.course ? el("span", { class: "sub", text: a.course }) : null),
      el("span", { class: "meta2" }, el("b", { text: fromNow(a.at) }), a.when)));
  }
  if (!due.children.length) due.append(el("li", {}, el("span", { class: "empty", text: "Nothing due in the next three days." })));
}

const waiting = $("#waiting");
function openWaiting(at) {
  renderNeeds();
  if (!waiting.open) waiting.showModal();
  ({ due: $("#due-title"), overdue: $("#overdue-title") }[at] || $("#waiting-title")).scrollIntoView({ block: "nearest" });
  $("#waiting-close").focus();
}
function closeWaiting() { if (waiting.open) waiting.close(); }
$("#waiting-open").addEventListener("click", () => openWaiting("needs"));
$("#waiting-close").addEventListener("click", closeWaiting);
waiting.addEventListener("click", (event) => { if (event.target === waiting) closeWaiting(); });

// -- today: the timeline --------------------------------------------------------------------

function subOf(item) {
  if (item.kind === "shift") return item.end > item.start ? `until ${short(item.end)}` : "";
  if (item.kind === "event") return [item.sub, item.end > item.start && !item.all_day ? `until ${short(item.end)}` : ""].filter(Boolean).join(" · ");
  if (item.kind === "due") return ["due", item.sub].filter(Boolean).join(" · ");
  if (item.kind === "reminder") return "reminder";
  if (item.kind === "focus") return [item.sub, `${item.end - item.start} min`].filter(Boolean).join(" · ");
  return item.sub || "";
}

function timelineRow(item, now, live) {
  const block = item.kind === "shift" || item.kind === "event" || item.kind === "focus";
  const past = live && !item.all_day && (block ? item.end <= now : item.start <= now);
  const sub = subOf(item);
  const cancel = item.kind === "reminder" && item.id && !past
    ? tool("x", `Cancel reminder: ${item.title}`, () => post(`/api/reminders/${item.id}/cancel`), "icon-btn sm x") : null;
  return el("li", { class: `row ${item.kind}${past ? " past" : ""}${item.all_day ? " all-day" : ""}` },
    el("span", { class: "h" }, el("span", { "aria-hidden": "true", text: item.all_day ? "all day" : short(item.start) }),
      el("span", { class: "sr", text: item.all_day ? "All day" : item.time })),
    el("div", { class: "ev" }, el("div", {}, el("span", { class: "t", text: item.title, title: item.title }),
      sub ? el("small", { text: sub }) : null), cancel));
}

function renderTimeline() {
  const data = latest;
  if (!data) return;
  const list = $("#timeline");
  const day = tomorrowShown ? data.agenda?.tomorrow : data.agenda?.today;
  const items = day?.items || [];
  const live = !tomorrowShown;
  const now = nowMinutes();
  list.replaceChildren();
  let placed = !live;
  const nowbar = () => el("li", { class: "nowbar", "aria-label": `Now, ${$("#clock").textContent}` },
    el("span", { text: "Now" }), el("i"));
  for (const item of items) {
    if (!placed && !item.all_day && item.start > now) { list.append(nowbar()); placed = true; }
    list.append(timelineRow(item, now, live));
  }
  if (!placed) list.append(nowbar());
  if (!items.length) list.append(el("li", { class: "empty", text: live ? "Nothing on the calendar today." : "A clear day tomorrow." }));
  // Opened late in the day, the morning is behind him: start with now near the top, once.
  if (live && !renderTimeline.scrolled) {
    const bar = list.querySelector(".nowbar");
    const col = list.closest(".agenda-body");
    if (bar && col && col.scrollHeight > col.clientHeight) {
      renderTimeline.scrolled = true;
      col.scrollTop = Math.max(0, bar.offsetTop - col.offsetTop - 96);
    }
  }
  for (const c of day?.clashes || []) {
    list.append(el("li", { class: "clash", text: `${c.what} ${c.hard ? "clashes with" : "is tight against"} ${c.against}.` }));
  }
  renderLater(data);
}

// After today: reminders on later days (cancel any), and promises he's made.
function renderLater(data) {
  const list = $("#later");
  list.replaceChildren();
  const today = new Set((data.agenda?.today?.items || []).filter((i) => i.kind === "reminder").map((i) => i.id));
  for (const r of data.reminders || []) {
    if (today.has(r.id)) continue;
    list.append(el("li", {},
      el("span", { class: "text" }, r.text, el("span", { class: "sub", text: [r.when, r.repeats ? `repeats ${r.repeats}` : ""].filter(Boolean).join(" · ") })),
      button(r.repeats ? "Stop" : "Cancel", () => post(`/api/reminders/${r.id}/cancel`), "btn sm ghost",
        { "aria-label": `${r.repeats ? "Stop" : "Cancel"} reminder: ${r.text}` })));
  }
  for (const c of data.promises || []) {
    list.append(el("li", {}, el("span", { class: "text" }, c.what + (c.to ? ` (to ${c.to})` : ""),
      el("span", { class: "sub", text: ["promise", c.when].filter(Boolean).join(" · ") }))));
  }
  $("#later-wrap").hidden = !list.children.length;
  renderPeek(data);
}

// With room left under today, tomorrow's first things, so the column is never half empty. As many
// as fit without making it scroll; none when today fills it.
function renderPeek(data) {
  const wrap = $("#peek-wrap");
  const list = $("#peek");
  list.replaceChildren();
  const items = tomorrowShown ? [] : (data.agenda?.tomorrow?.items || []).slice(0, 8);
  for (const i of items) {
    list.append(el("li", { class: `peek-r ${i.kind}` }, el("span", { class: "h", text: i.all_day ? "All day" : short(i.start) }),
      el("span", { class: "t", text: jobLabel(i) + (i.kind === "shift" && i.end != null ? ` until ${short(i.end)}` : "") })));
  }
  wrap.hidden = !list.children.length;
  const col = $("#agenda-body");
  while (list.children.length && col.scrollHeight > col.clientHeight + 1) list.lastElementChild.remove();
  if (!list.children.length) wrap.hidden = true;
}

function setDay(tomorrow) {
  tomorrowShown = tomorrow;
  $("#day-today").setAttribute("aria-pressed", String(!tomorrow));
  $("#day-tomorrow").setAttribute("aria-pressed", String(tomorrow));
  renderTimeline();
}
$("#day-today").addEventListener("click", () => setDay(false));
$("#day-tomorrow").addEventListener("click", () => setDay(true));

// -- Home: the widgets -------------------------------------------------------------------------
// Every widget is the same card: an icon and a name, a word on the right, at most two buttons,
// then what it shows. Each one does something: ticks, adds, starts, opens, or asks her about it.
// A button runs his own command through /api/do (the same handler as typing it); a question
// goes to her in the chat.

const PANEL_NAMES = {
  weather: "Weather", whoop: "Whoop", workouts: "Workouts", markets: "Markets", inbox: "Inbox", habits: "Habits",
  news: "News", focus: "Focus", money: "Money", portfolio: "Portfolio", countdowns: "Countdowns", lists: "Lists", birthdays: "Birthdays",
  clients: "Clients", school: "School", workshop: "Workshop", learned: "Learned today", grades: "Grades",
  week: "This week", colleges: "College", cards: "Flashcards", deca: "DECA practice", engine: "Engine",
  work: "Up next at work", plan: "Tonight's plan", memory: "Loose ends",
};
// Widgets that come from the page's own data, not a skill: shown whatever skills are loaded.
const CORE_PANELS = new Set(["inbox", "school", "learned", "grades", "week", "engine", "work"]);
// Two columns wide: the ones that are rows of words, and the ones with a chart beside their numbers.
const WIDE = new Set(["inbox", "news", "habits", "markets", "whoop"]);
// Skills whose panel other widgets draw (the bank's: Money and Portfolio), never a widget of their own.
const FEEDS = new Set(["bank"]);
function spanOf(id) {
  if (WIDE.has(id)) return 2;
  if (id === "money" && panelOf("bank")?.cash_cents !== undefined) return 2;
  return 1;
}

const SVG = "http://www.w3.org/2000/svg";
const ICONS = {
  whoop: '<path d="M3 12h4l2.2-5 4.2 10 2.2-5H21"/>',
  workouts: '<path d="M6.5 7v10M17.5 7v10M3.5 9.5v5M20.5 9.5v5M6.5 12h11"/>',
  markets: '<path d="M4 17l5-5 4 3 7-8"/><path d="M15 7h5v5"/>',
  weather: '<circle cx="12" cy="12" r="3.8"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/>',
  inbox: '<path d="M4 13.5 6.5 5.5h11l2.5 8V19H4z"/><path d="M4 13.5h4.5l1 2h5l1-2H20"/>',
  news: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 12.5h8M8 16h5"/>',
  habits: '<circle cx="12" cy="12" r="8"/><path d="M8.5 12.2l2.4 2.3 4.6-4.8"/>',
  focus: '<circle cx="12" cy="13" r="7"/><path d="M12 9.5V13l2.5 1.5M9.5 3.5h5"/>',
  money: '<rect x="3.5" y="6.5" width="17" height="11" rx="2"/><circle cx="12" cy="12" r="2.2"/>',
  portfolio: '<path d="M12 3.5a8.5 8.5 0 1 0 8.5 8.5H12z"/><path d="M14.5 2.8v6.7h6.7a6.8 6.8 0 0 0-6.7-6.7z"/>',
  lists: '<path d="M10 7h9.5M10 12h9.5M10 17h9.5"/><path d="M4.5 7l1 1 2-2M4.5 12l1 1 2-2M4.5 17l1 1 2-2"/>',
  countdowns: '<path d="M7 3.5h10M7 20.5h10M8 3.5c0 5 8 5 8 8.5s-8 3.5-8 8.5M16 3.5c0 5-8 5-8 8.5s8 3.5 8 8.5"/>',
  birthdays: '<rect x="4" y="10.5" width="16" height="9.5" rx="2"/><path d="M4 14.5c2.7 1.4 5.3 1.4 8 0s5.3-1.4 8 0M12 10.5V7.5"/><path d="M12 3.8c.9 1 .9 2 0 2.7-.9-.7-.9-1.7 0-2.7z"/>',
  clients: '<rect x="3.5" y="7.5" width="17" height="12" rx="2"/><path d="M9 7.5v-2h6v2M3.5 12.5h17"/>',
  school: '<path d="M3 9.5 12 5l9 4.5-9 4.5z"/><path d="M7 11.5v4c1.4 1.2 3 1.8 5 1.8s3.6-.6 5-1.8v-4M21 9.5v5"/>',
  workshop: '<path d="M14.5 4.5a4.5 4.5 0 0 0-4.2 6.1L4 16.9 7.1 20l6.3-6.3a4.5 4.5 0 0 0 6.1-4.2l-2.7 2.7-3-.3-.3-3z"/>',
  learned: '<path d="M6 3.5h12v17l-6-4.5-6 4.5z"/>',
  memory: '<path d="M6 3.5h12v17l-6-4.5-6 4.5z"/>',
  colleges: '<path d="M4 20h16M6 20v-9M18 20v-9M10 20v-5h4v5M3.5 10.5 12 5l8.5 5.5z"/>',
  engine: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-5"/>',
  work: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
  week: '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  grades: '<path d="M5 19v-7M10 19V6M15 19v-4M20 19V9"/>',
  cards: '<rect x="5" y="4" width="14" height="16" rx="2"/><path d="M9 9h6M9 13h4"/>',
  deca: '<path d="M4 18.5V6.5l8 4 8-4v12M12 10.5v9"/>',
  plan: '<path d="M5 5h14v14H5zM5 10h14M10 10v9"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  ask: '<path d="M4 5.5h16v10H9.5L5 19.5v-4H4z"/>',
  open: '<path d="M9.5 6l6 6-6 6"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4"/>',
  check: '<path d="M5.5 12.5l4 4 9-9"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  box: '<rect x="4" y="4" width="16" height="16" rx="3"/>',
};

// Constant SVG from the table above: nothing from outside goes in.
function icon(name, cls = "ico") {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || ICONS.box;
  return svg;
}

function tool(name, label, onclick, cls = "icon-btn sm") {
  return el("button", { type: "button", class: cls, "aria-label": label, title: label, onclick }, icon(name));
}

function asking(label, question) { return tool("ask", label, () => askHer(question)); }
function adding(label, form) { return tool("plus", label, (event) => openForm(event.currentTarget.closest(".card"), form)); }

function widget(id, { title, meta = "", metaCls = "", actions = [] } = {}, ...body) {
  return el("section", { class: "card w", "data-id": id, "aria-label": title || PANEL_NAMES[id] || id },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, icon(ICONS[id] ? id : "box"), el("span", { text: title || PANEL_NAMES[id] || id })),
      meta ? el("span", { class: `card-meta ${metaCls}`, text: meta }) : null,
      actions.length ? el("div", { class: "card-acts" }, ...actions) : null),
    el("div", { class: "card-body" }, ...body));
}

// A row: what on the left (and a second line), a value on the right, controls around it.
function item({ lead = null, one, two = "", val = null, tail = null, href = "", cls = "", label = "" } = {}) {
  const middle = el("span", { class: "grow" }, el("span", { class: "one", text: one, title: one }),
    two ? el("span", { class: "two", text: two, title: two }) : null);
  const kids = [lead, middle, val, tail];
  if (href) {
    return el("li", { class: cls }, el("a", { class: "li", href, target: "_blank", rel: "noopener noreferrer",
      "aria-label": label || null }, ...kids));
  }
  return el("li", { class: `li ${cls}`, "aria-label": label || null }, ...kids);
}

function valueOf(big, small = "") {
  return el("span", { class: "val" }, el("span", { text: big }), small ? el("small", { text: small }) : null);
}

// His command, run: the toast is her answer, and the page catches up.
async function run(command) {
  return post("/api/do", { command });
}

// -- the little forms: over the widget, the fields the command needs ---------------------------

let formOpen = null;
let redrawWaiting = false;

function closeForms() {
  if (formOpen) formOpen.remove();
  formOpen = null;
  if (redrawWaiting) { redrawWaiting = false; renderPanels(); }
}

const WORKOUT_KINDS = ["run", "lift", "ride", "walk", "swim", "yoga", "hike", "climb", "basketball", "soccer"];
const FORMS = {
  workout: { title: "Log a workout", submit: "Log", fields: [
    { name: "what", placeholder: "What: run, lift, ride…", size: "wide", list: "workout-kinds" },
    { name: "minutes", placeholder: "Minutes", inputmode: "numeric" },
    { name: "miles", placeholder: "Miles", inputmode: "decimal" }],
  command: (v) => v.what && `/workout ${v.what}${v.miles ? ` ${v.miles} mi` : ""}${v.minutes ? ` ${v.minutes} min` : ""}` },
  watch: { title: "Watch a ticker", submit: "Watch", fields: [{ name: "what", placeholder: "Ticker or name, like NVDA or bitcoin", size: "wide" }],
    command: (v) => v.what && `/watch ${v.what}` },
  follow: { title: "Follow a topic", submit: "Follow", fields: [{ name: "what", placeholder: "Like the Broncos or Formula 1", size: "wide" }],
    command: (v) => v.what && `/news follow ${v.what}` },
  habit: { title: "Track a habit", submit: "Track", fields: [{ name: "what", placeholder: "Like reading, or the gym", size: "wide" }],
    command: (v) => v.what && `/habit add ${v.what}` },
  spent: { title: "Log spending", submit: "Log", fields: [
    { name: "amount", placeholder: "Amount", inputmode: "decimal", size: "narrow" }, { name: "what", placeholder: "On what" }],
  command: (v) => v.amount && `/spent ${v.amount.replace(/^\$/, "")} ${v.what}`.trim() },
  budget: { title: "Set a weekly budget", submit: "Set", fields: [{ name: "amount", placeholder: "Dollars a week", inputmode: "decimal", size: "wide" }],
    command: (v) => v.amount && `/budget ${v.amount.replace(/^\$/, "")}` },
  countdown: { title: "Count down to a day", submit: "Add", fields: [
    { name: "what", placeholder: "What", size: "wide" }, { name: "when", placeholder: "When, like Dec 3", size: "wide" }],
  command: (v) => v.what && v.when && `/countdown ${v.what} ${v.when}` },
  birthday: { title: "Add a birthday", submit: "Add", fields: [
    { name: "who", placeholder: "Who" }, { name: "when", placeholder: "When, like Mar 3" }],
  command: (v) => v.who && v.when && `/birthday ${v.who} ${v.when}` },
  client: { title: "Add a client", submit: "Add", fields: [
    { name: "who", placeholder: "Name", size: "wide" }, { name: "amount", placeholder: "Worth ($, optional)", inputmode: "decimal", size: "wide" }],
  command: (v) => v.who && `/client add ${v.who}${v.amount ? ` $${v.amount.replace(/^\$/, "")}` : ""}` },
  remind: { title: "Add a reminder", submit: "Remind me", fields: [
    { name: "what", placeholder: "What", size: "wide" }, { name: "when", placeholder: "When: 7pm, tomorrow 9am, in 20 min", size: "wide" }],
  command: (v) => v.what && v.when && `/remind ${v.when} ${v.what}` },
  remember: { title: "Tell her something to keep", submit: "Keep it", fields: [{ name: "what", placeholder: "Like: I'm vegetarian now", size: "wide" }],
    command: (v) => v.what && `/remember ${v.what}` },
  focus: { title: "Start a focus block", submit: "Start", fields: [
    { name: "what", placeholder: "On what", size: "wide" }, { name: "minutes", placeholder: "Minutes", inputmode: "numeric", value: "25" }],
  command: (v) => `/focus ${Number(v.minutes) || 25} ${v.what}`.trim() },
  idea: { title: "Ask her to build something", submit: "Add idea", fields: [{ name: "what", placeholder: "Like: track how much water I drink", size: "wide" }],
    send: (v) => v.what && post("/api/workshop/ideas", { text: v.what }).then((data) => { loadWorkshop(); return data; }) },
};
function listForm(name) {
  return { title: `Add to ${name ? `the ${name} list` : "a list"}`, submit: "Add", fields: [
    { name: "what", placeholder: "Items, separated by commas", size: "wide" },
    { name: "list", placeholder: "Which list", value: name || "grocery", size: "wide" }],
  command: (v) => v.what && `/list add ${v.list || "grocery"}: ${v.what}` };
}

function openForm(host, spec) {
  if (!host) return;
  if (formOpen) formOpen.remove();
  const inputs = spec.fields.map((f) => el("input", { class: `field ${f.size || ""}`, name: f.name, type: "text",
    placeholder: f.placeholder, "aria-label": f.placeholder, inputmode: f.inputmode || null, list: f.list || null,
    maxlength: "200", autocomplete: "off", value: f.value ?? null }));
  const submit = el("button", { type: "submit", class: "btn primary sm", text: spec.submit || "Add" });
  const form = el("form", { class: "wform", "aria-label": spec.title },
    el("div", { class: "card-head" }, el("h2", { class: "card-title" }, el("span", { text: spec.title })),
      el("div", { class: "card-acts" }, tool("x", "Cancel", closeForms))),
    el("div", { class: "fields" }, ...inputs),
    el("div", { class: "acts" }, button("Cancel", closeForms, "btn ghost sm"), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(inputs.map((i) => [i.name, i.value.trim()]));
    const command = spec.send ? null : spec.command(values);
    if (!spec.send && !command) { (inputs.find((i) => !i.value.trim()) || inputs[0]).focus(); return; }
    if (spec.send && !values.what) { inputs[0].focus(); return; }
    submit.disabled = true;
    const data = await (spec.send ? spec.send(values) : run(command));
    submit.disabled = false;
    if (data && data.ok !== false) closeForms();
  });
  form.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeForms(); }
  });
  host.append(form);
  formOpen = form;
  inputs[0].focus();
}

// A command with a second look: the first press asks, the second does it. For the few buttons
// that hide something (a deadline marked handed in, a ticker dropped, a fact forgotten). The ask
// is kept apart from the button, so a refresh that redraws the widget in between doesn't lose it.
let asked = { key: "", until: 0 };
function careful(node, command, question) {
  const key = typeof command === "function" ? question : command;
  const pending = () => asked.key === key && Date.now() < asked.until;
  if (pending()) node.classList.add("sure");
  node.addEventListener("click", () => {
    if (pending()) {
      asked = { key: "", until: 0 };
      if (typeof command === "function") command(); else run(command);
      return;
    }
    asked = { key, until: Date.now() + 5000 };
    node.classList.add("sure");
    toast(question);
    setTimeout(() => { if (!pending()) node.classList.remove("sure"); }, 5100);
  });
  return node;
}

// -- the pieces widgets are drawn with ----------------------------------------------------------

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  return node;
}

// A ring: how far along something is. It sizes itself to the space it's given (CSS), so a
// bigger widget gets a bigger ring, and says its number in the middle.
function ring(fraction, cls, text, { label = "", sub = "" } = {}) {
  const C = 2 * Math.PI * 42;
  const svg = svgEl("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });
  svg.append(svgEl("circle", { class: "track", cx: 50, cy: 50, r: 42, fill: "none", "stroke-width": 8 }),
    svgEl("circle", { class: `done ${cls}`, cx: 50, cy: 50, r: 42, fill: "none", "stroke-width": 8, "stroke-linecap": "round",
      "stroke-dasharray": C.toFixed(1), "stroke-dashoffset": (C * (1 - Math.max(0, Math.min(1, fraction)))).toFixed(1),
      transform: "rotate(-90 50 50)" }));
  return el("div", { class: "ring" }, el("div", { class: "ring-art" }, svg, el("b", { class: text.length > 4 ? "long" : "", text })),
    label ? el("span", { class: "ring-l", text: label }) : null, sub ? el("span", { class: "ring-s", text: sub }) : null);
}

// A change, said with an arrow as well as its colour.
function change(pct, text = null) {
  if (pct == null || Number.isNaN(pct)) return el("span", { class: "chg" });
  const up = pct >= 0;
  return el("span", { class: `chg ${up ? "up" : "down"}` }, `${up ? "▲" : "▼"} ${text ?? `${Math.abs(pct).toFixed(2)}%`}`);
}

function money$(cents, { sign = false, whole = null } = {}) {
  const n = Math.abs(cents) / 100;
  const round = whole ?? (Math.abs(cents) % 100 === 0 || n >= 10000);
  const body = `$${n.toLocaleString("en-US", { minimumFractionDigits: round ? 0 : 2, maximumFractionDigits: round ? 0 : 2 })}`;
  return cents < 0 ? `−${body}` : `${sign ? "+" : ""}${body}`;
}

// A line over time, its area shaded, with a dashed line where it started (yesterday's close) and
// a crosshair that reads out the value under the pointer.
let charts = 0;
function areaChart(values, { base = null, up = true, say = (v) => String(v), when = null } = {}) {
  const wrap = el("div", { class: `chart ${up ? "up" : "down"}` });
  if (!values || values.length < 2) return wrap;
  const all = base != null ? [...values, base] : values;
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.1 || Math.abs(hi) * 0.002 || 1;
  lo -= pad; hi += pad;
  const W = 100, H = 50, id = `area-${++charts}`;
  const x = (i) => (i / (values.length - 1)) * W;
  const y = (v) => H - ((v - lo) / (hi - lo)) * H;
  const line = values.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  const grad = svgEl("linearGradient", { id, x1: 0, x2: 0, y1: 0, y2: 1 });
  grad.append(svgEl("stop", { offset: "0", "stop-color": "currentColor", "stop-opacity": "0.28" }),
    svgEl("stop", { offset: "1", "stop-color": "currentColor", "stop-opacity": "0" }));
  const defs = svgEl("defs");
  defs.append(grad);
  svg.append(defs, svgEl("path", { d: `M0,${H} L${line.replaceAll(" ", " L")} L${W},${H} Z`, fill: `url(#${id})` }));
  if (base != null) svg.append(svgEl("line", { class: "base", x1: 0, x2: W, y1: y(base).toFixed(2), y2: y(base).toFixed(2) }));
  svg.append(svgEl("polyline", { class: "stroke", points: line }));
  const last = values[values.length - 1];
  const hair = el("i", { class: "hair", hidden: true });
  const tip = el("span", { class: "tip", hidden: true });
  wrap.append(svg, el("i", { class: "dot", style: `left:100%;top:${((y(last) / H) * 100).toFixed(1)}%` }), hair, tip);
  const move = (event) => {
    const box = wrap.getBoundingClientRect();
    const i = Math.max(0, Math.min(values.length - 1, Math.round(((event.clientX - box.left) / box.width) * (values.length - 1))));
    const left = (x(i) / W) * 100;
    hair.hidden = tip.hidden = false;
    hair.style.left = `${left}%`;
    tip.textContent = when ? `${when(i)} · ${say(values[i])}` : say(values[i]);
    tip.style.left = `${Math.max(12, Math.min(88, left))}%`;
  };
  wrap.addEventListener("pointermove", move);
  wrap.addEventListener("pointerleave", () => { hair.hidden = tip.hidden = true; });
  return wrap;
}

// Magnitudes side by side: a label, a bar in one colour, the amount.
function barList(rows, cls = "") {
  const most = Math.max(1, ...rows.map((r) => r.value));
  return el("ul", { class: `bl fit ${cls}` }, ...rows.map((r) => el("li", { class: "li", title: `${r.label}: ${r.said}` },
    el("span", { class: "bl-l", text: r.label }),
    el("span", { class: "bl-t", "aria-hidden": "true" }, el("i", { style: `width:${Math.max(2, (r.value / most) * 100).toFixed(1)}%` })),
    el("span", { class: "bl-v", text: r.said }))));
}

// -- each widget -------------------------------------------------------------------------------

function weatherW() {
  const p = panelOf("weather");
  if (!p || p.temp == null) return null;
  const hours = (p.hours || []).filter((h) => h.temp != null).slice(0, 12);
  const cap = (s) => (s || "").replace(/^./, (c) => c.toUpperCase());
  const range = p.high != null && p.low != null ? `H ${Math.round(p.high)}°  L ${Math.round(p.low)}°` : "";
  const top = el("div", { class: "wx-top" }, el("div", { class: "num big", text: `${Math.round(p.temp)}°` }),
    el("div", { class: "grow" }, el("div", { class: "num-md one", text: cap(p.sky) }),
      el("div", { class: "sub", text: [p.feels != null ? `Feels ${Math.round(p.feels)}°` : "", range].filter(Boolean).join(" · ") })));
  const facts = el("div", { class: "wx-facts" },
    p.chance != null ? el("span", {}, el("b", { text: `${p.chance}%` }), "Rain") : null,
    p.humidity != null ? el("span", {}, el("b", { text: `${p.humidity}%` }), "Humidity") : null,
    p.wind != null ? el("span", {}, el("b", { text: `${p.wind}` }), p.wind_unit || "mph") : null,
    p.sunset ? el("span", {}, el("b", { text: p.sunset.replace(/ [AP]M$/, "") }), "Sunset") : null);
  const box = widget("weather", { actions: [asking("Ask about the weather", "What's the weather doing this week?")] }, top);
  const body = box.querySelector(".card-body");
  if (hours.length) {
    const temps = hours.map((h) => h.temp);
    const lo = Math.min(...temps), hi = Math.max(...temps);
    let pm;
    const cols = el("div", { class: "wx", role: "img", "aria-label": `Next hours: ${hours.map((h) => `${short(minuteOf(h.at))} ${Math.round(h.temp)}°`).join(", ")}` });
    for (const h of hours) {
      const [label, isPm] = hourLabel(h.at, pm);
      pm = isPm;
      cols.append(el("span", { title: `${short(minuteOf(h.at))}: ${Math.round(h.temp)}°, ${h.sky || ""}${h.chance ? `, ${h.chance}% rain` : ""}` },
        el("b", { text: `${Math.round(h.temp)}°` }),
        el("i", { class: h.chance >= 50 ? "wet" : "", style: `height:${hi === lo ? 55 : 22 + ((h.temp - lo) / (hi - lo)) * 78}%` }),
        el("small", { text: label })));
    }
    body.append(cols);
  }
  if (facts.children.length) body.append(facts);
  const days = (p.days || []).slice(0, 4);
  if (days.length) {
    body.append(el("div", { class: "wx-days" }, ...days.map((d) => el("span", { title: `${d.day}: ${cap(d.sky)}, ${d.chance}% rain` },
      el("small", { text: d.day }), el("b", { text: `${Math.round(d.high)}°` }), el("small", { class: "lo", text: `${Math.round(d.low)}°` }),
      d.chance >= 30 ? el("small", { class: "wet", text: `${d.chance}%` }) : null))));
  }
  return box;
}

// Whoop, as Whoop draws it: three rings (sleep, recovery, strain), then HRV and resting heart
// rate against his own average: right of the mark is above it, left below, green the good way.
function whoopW() {
  const p = panelOf("whoop");
  if (!p || (p.recovery == null && p.sleep_minutes == null && p.strain == null)) {
    if (!p?.down) return null;
    return widget("whoop", { meta: "Offline" }, el("div", { class: "w-empty" },
      el("p", { text: "Whoop isn't answering right now. She'll try again on the next refresh." })));
  }
  const zone = { green: "good", yellow: "hot", red: "bad" }[p.zone] || "";
  const rings = el("div", { class: "rings3" },
    ring(p.sleep_performance == null ? 0 : p.sleep_performance / 100, "sleep", p.sleep_performance == null ? "—" : `${p.sleep_performance}%`,
      { label: "Sleep", sub: p.sleep_minutes != null ? `${hm(p.sleep_minutes)}${p.sleep_need_minutes ? ` of ${hm(p.sleep_need_minutes)}` : ""}` : "" }),
    ring(p.recovery == null ? 0 : p.recovery / 100, p.zone || "", p.recovery == null ? "—" : `${p.recovery}%`,
      { label: "Recovery", sub: p.zone ? p.zone.replace(/^./, (c) => c.toUpperCase()) : "Not scored" }),
    ring(p.strain == null ? 0 : p.strain / (p.strain_max || 21), "strain", p.strain == null ? "—" : p.strain.toFixed(1),
      { label: "Strain", sub: `of ${p.strain_max || 21}` }));
  const vs = (name, value, avg, unit, higherIsBetter) => {
    if (value == null) return null;
    const row = el("div", { class: "vs" }, el("span", { class: "vs-l" }, el("b", { text: name }), el("span", { text: `${value} ${unit}` })));
    if (avg == null) {
      row.append(el("span", { class: "vs-t" }), el("span", { class: "vs-d muted", text: "No average yet" }));
      return row;
    }
    const pct = ((value - avg) / avg) * 100;
    const good = higherIsBetter ? pct >= 0 : pct <= 0;
    const width = Math.min(50, (Math.abs(pct) / 30) * 50);
    const bar = el("span", { class: "vs-t", role: "img", "aria-label": `${name} ${value} ${unit}, ${Math.abs(pct).toFixed(0)}% ${pct >= 0 ? "above" : "below"} your ${Math.round(avg)} average` },
      el("i", { class: good ? "good" : "hot", style: `${pct >= 0 ? "left" : "right"}:50%;width:${width.toFixed(1)}%` }), el("em"));
    row.append(bar, el("span", { class: `vs-d ${good ? "good" : "hot"}`, title: `Your average over ${p.avg_days} days: ${avg} ${unit}` },
      `${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(0)}%`, el("small", { text: ` vs ${Math.round(avg)}` })));
    return row;
  };
  const bars = el("div", { class: "vs-rows" }, vs("HRV", p.hrv, p.hrv_avg, "ms", true), vs("Resting HR", p.rhr, p.rhr_avg, "bpm", false));
  return widget("whoop", { meta: p.old ? "Earlier" : "", metaCls: zone,
    actions: [asking("Ask how you slept", "How did I sleep, and how recovered am I?")] }, rings, bars.children.length ? bars : null);
}

function workoutsW() {
  const p = panelOf("workouts");
  if (!p) return null;
  const most = Math.max(30, ...(p.days || []).map((d) => d.minutes || (d.count ? 30 : 0)));
  const bars = el("div", { class: "bars7", role: "img", "aria-label": (p.days || []).map((d) => `${d.day} ${d.count ? `${d.minutes} min` : "none"}`).join(", ") });
  for (const d of p.days || []) {
    const minutes = d.minutes || (d.count ? 30 : 0);
    bars.append(el("span", { class: d.today ? "today" : "", title: `${d.day}: ${d.count ? `${(d.kinds || []).join(", ")}${d.minutes ? `, ${d.minutes} min` : ""}` : "rest"}` },
      el("i", { class: d.count ? "y" : "", style: `height:${d.count ? Math.max(18, (minutes / most) * 100).toFixed(0) : 5}%` }), d.day[0]));
  }
  const goal = p.goal || 0;
  const hero = el("div", { class: "hero-row" },
    ring(goal ? p.count / goal : Math.min(1, p.count / 4), p.count >= goal && goal ? "good" : "", goal ? `${p.count}/${goal}` : String(p.count)),
    el("div", { class: "hero-text" }, el("div", { class: "num-md", text: `${p.count} workout${p.count === 1 ? "" : "s"}` }),
      el("div", { class: "sub", text: [p.minutes ? hm(p.minutes) : "", p.distance || "", p.strain != null ? `strain ${p.strain}` : ""].filter(Boolean).join(" · ") || "this week" })));
  const last = p.last;
  const lastLine = last ? el("div", { class: "last-w" }, last.source === "whoop" ? el("span", { class: "pill now", text: "Whoop" }) : null,
    el("span", { class: "one", text: `${last.text}, ${last.when.toLowerCase()}${last.strain != null ? ` · strain ${last.strain}` : ""}${last.heart_rate ? ` · ${last.heart_rate} bpm` : ""}` })) : null;
  return widget("workouts", { meta: p.whoop ? (p.from_whoop ? `${p.from_whoop} from Whoop` : "Whoop synced") : "",
    actions: [adding("Log a workout", FORMS.workout)] }, el("div", { class: "centered" }, hero, bars, lastLine));
}


function spark(values, up) {
  if (!values || values.length < 2) return null;
  const lo = Math.min(...values), hi = Math.max(...values);
  const points = values.map((v, i) => `${((i / (values.length - 1)) * 48).toFixed(1)},${(hi === lo ? 10 : 19 - ((v - lo) / (hi - lo)) * 18).toFixed(1)}`);
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 48 20");
  svg.setAttribute("class", `spark ${up ? "up" : "down"}`);
  svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS(SVG, "polyline");
  line.setAttribute("points", points.join(" "));
  svg.append(line);
  return svg;
}

// Markets: the market at a glance across the top, a chart of the one he picked, his watchlist.
let marketShown = recall("sloane.market", "");
const TICKER_SHORT = { "^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow", "^RUT": "Russell", "^VIX": "VIX", "^TNX": "10Y yield",
  "GC=F": "Gold", "CL=F": "Oil", "BTC-USD": "Bitcoin", "ETH-USD": "Ether" };
function marketsW() {
  const p = panelOf("markets");
  if (!p) return null;
  const quotes = p.quotes || [];
  const overview = p.overview || [];
  const every = [...quotes, ...overview];
  const pick = every.find((q) => q.symbol === marketShown) || quotes[0] || overview[0];
  const choose = (symbol) => { marketShown = symbol; remember("sloane.market", symbol); renderPanels(); };
  const strip = el("div", { class: "mk-strip", role: "list", "aria-label": "The market" }, ...overview.map((q) => {
    const up = (q.change ?? 0) >= 0;
    return el("button", { type: "button", class: "mk-chip", role: "listitem", "aria-pressed": String(pick && q.symbol === pick.symbol),
      title: `${q.name}: ${q.price}${q.change != null ? `, ${up ? "up" : "down"} ${Math.abs(q.change).toFixed(2)}%` : ""}`,
      "aria-label": `${q.name} ${q.price}${q.change != null ? `, ${up ? "up" : "down"} ${Math.abs(q.change).toFixed(2)} percent` : ""}`,
      onclick: () => choose(q.symbol) }, el("span", { class: "n", text: TICKER_SHORT[q.symbol] || q.name }), change(q.change));
  }));
  let chart = el("div", { class: "mk-chart" });
  if (pick) {
    const up = (pick.change ?? 0) >= 0;
    const fmt = (v) => (pick.symbol === "^TNX" ? `${v.toFixed(2)}%` : v >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 })
      : v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: v < 1 ? 4 : 2 }));
    chart = el("div", { class: "mk-chart" },
      el("div", { class: "mk-head" }, el("span", { class: "mk-name", text: pick.name }), el("span", { class: "mk-price", text: pick.price }), change(pick.change)),
      areaChart(pick.spark, { base: pick.previous, up, say: fmt }),
      el("div", { class: "mk-foot" }, el("span", { text: "Today" }), pick.previous != null ? el("span", { text: `Prev close ${fmt(pick.previous)}` }) : null));
  }
  const list = el("ul", { class: "fit mk" });
  for (const q of quotes) {
    const up = (q.change ?? 0) >= 0;
    const name = q.name === q.symbol ? q.symbol : q.name;
    const drop = p.mine ? careful(tool("x", `Stop watching ${name}`, null, "icon-btn sm hover"), `/unwatch ${q.symbol}`,
      `Press again to stop watching ${name}`) : null;
    const row = item({ one: TICKER_SHORT[q.symbol] || q.symbol.replace(/-USD$/, "").replace(/^\^/, ""), cls: pick && q.symbol === pick.symbol ? "on" : "",
      label: `${name}: ${q.price}${q.change != null ? `, ${up ? "up" : "down"} ${Math.abs(q.change).toFixed(2)}% today` : ""}`,
      val: el("span", { class: "val-row" }, spark(q.spark, up), el("span", { class: "val mk-val" }, el("span", { text: q.price }), change(q.change))),
      tail: drop });
    row.addEventListener("click", (event) => { if (!event.target.closest("button")) choose(q.symbol); });
    list.append(row);
  }
  if (!quotes.length && !overview.length) list.append(el("li", { class: "sub", text: (p.lines || [])[0] || "No prices right now." }));
  return widget("markets", { meta: p.old ? "Delayed" : "", actions: [
    adding("Watch a ticker", FORMS.watch), asking("Ask about the market", "How's the market doing today?")] },
  overview.length ? strip : null, el("div", { class: "mk-main" }, chart, quotes.length ? list : null));
}

function inboxW() {
  const box = latest?.inbox;
  if (!box) return null;
  if (!box.ready && !(box.items || []).length) {
    return widget("inbox", {}, el("div", { class: "w-empty" },
      el("p", { text: "She reads your Gmail every few hours and flags what needs you, with a draft reply ready." }),
      el("p", { class: "note", text: "Connect Gmail on the box: DEPLOY.md, section 7c." })));
  }
  const list = el("ul", { class: "fit ib" });
  const words = { urgent: "Urgent", reply: "Needs a reply", fyi: "For your information" };
  for (const m of box.items || []) {
    const row = [el("span", { class: "dot", title: words[m.category] || "" }), el("span", { class: "who", text: m.from }),
      el("span", { class: "subj", text: m.subject, title: m.subject }), el("span", { class: "when", text: m.when.replace(/^Today /, "") })];
    const label = `${words[m.category] || ""}: ${m.from}, ${m.subject}. ${m.thread ? "Opens in Gmail." : ""}`;
    list.append(m.thread
      ? el("li", { class: m.category }, el("a", { class: "li", href: `https://mail.google.com/mail/u/0/#all/${m.thread}`,
        target: "_blank", rel: "noopener noreferrer", "aria-label": label }, ...row))
      : el("li", { class: `li ${m.category}`, "aria-label": label }, ...row));
  }
  if (!list.children.length) list.append(el("li", { class: "sub", text: "Nothing new that needs you." }));
  return widget("inbox", { meta: box.waiting ? `${box.waiting} need${box.waiting === 1 ? "s" : ""} you` : "All clear",
    metaCls: box.waiting ? "hot" : "good", actions: [
      tool("refresh", "Check the inbox now", () => post("/api/jobs/inbox/run")),
      asking("Ask what needs a reply", "Anything in my inbox I need to reply to?")] }, list);
}

function newsW() {
  const p = panelOf("news");
  if (!p) return null;
  const list = el("ul", { class: "fit nw" });
  for (const n of (p.items || []).slice(0, 10)) {
    const src = [n.topic, n.source, n.ago].filter(Boolean).join(" · ");
    const row = [el("span", { class: "grow" }, el("span", { class: "one", text: n.title, title: n.title })), el("span", { class: "src", text: src })];
    list.append(n.url
      ? el("li", {}, el("a", { class: "li", href: n.url, target: "_blank", rel: "noopener noreferrer" }, ...row))
      : el("li", { class: "li" }, ...row));
  }
  if (!list.children.length) list.append(el("li", { class: "sub", text: (p.lines || [])[0] || "No headlines right now." }));
  return widget("news", { meta: p.old ? "Earlier" : "", actions: [
    adding("Follow a topic", FORMS.follow), asking("Ask for the news", "What's in the news?")] }, list);
}

function habitsW() {
  const habits = panelOf("habits")?.habits || [];
  if (!habits.length) return null;
  const list = el("ul", { class: "fit hb" });
  for (const h of habits) {
    const days = h.days || [];
    const today = !!days[days.length - 1];
    const tick = el("button", { type: "button", class: "tick", "aria-pressed": String(today),
      "aria-label": today ? `${h.name}: done today. Undo` : `Mark ${h.name} done today`,
      title: today ? "Done today. Press to undo" : "Mark done today",
      onclick: () => run(today ? `/habit undo ${h.name}` : `/did ${h.name}`) }, icon("check"));
    const dots = el("span", { class: "dots14", "aria-hidden": "true" });
    days.forEach((done, i) => dots.append(el("i", { class: [done ? "y" : "", i === days.length - 1 ? "today" : ""].join(" ").trim() })));
    const kept = days.filter(Boolean).length;
    list.append(el("li", { class: "li" }, tick, el("span", { class: "name", text: h.name, title: h.name }), dots,
      el("span", { class: "streak", "aria-label": `${h.streak} day streak, ${kept} of the last 14 days` },
        el("b", { text: `${h.streak}d` }))));
  }
  return widget("habits", { meta: "Last 14 days", actions: [adding("Track a habit", FORMS.habit)] }, list);
}

// Focus: what's worth a block (his work due soonest), one press to start it; while it runs, the
// time left, and her messages held till it's done.
function focusW() {
  const loaded = new Set(latest?.system?.skills || []);
  if (!loaded.has("focus")) return null;
  const p = panelOf("focus") || { today_minutes: 0, goal_minutes: 0, running: null, suggest: [], week: [] };
  const running = p.running;
  if (running) {
    const total = Math.max(1, Date.parse(running.ends_at) - Date.parse(running.started_at));
    const left = Math.max(0, Date.parse(running.ends_at) - Date.now());
    const until = new Date(running.ends_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: clockState.tz });
    return widget("focus", { meta: "Running", metaCls: "good" }, el("div", { class: "centered" },
      ring(left / total, "", `${Math.ceil(left / 60000)}m`),
      el("div", { class: "hero-text c" }, el("div", { class: "num-md one", text: running.what, title: running.what }),
        el("div", { class: "sub", text: `Until ${until} · holding her messages` })),
      el("div", { class: "acts" }, button("Stop", () => run("/focus stop"), "btn sm danger"))));
  }
  const goal = p.goal_minutes || 0;
  const hero = el("div", { class: "hero-row" }, ring(goal ? Math.min(1, p.today_minutes / goal) : 0, "", hm(p.today_minutes || 0)),
    el("div", { class: "hero-text" }, el("div", { class: "num-md", text: p.today_minutes ? "Focused today" : "No focus yet" }),
      el("div", { class: "sub", text: [goal ? `Goal ${hm(goal)}` : "", p.week_minutes ? `${hm(p.week_minutes)} this week` : ""].filter(Boolean).join(" · ") || "Start a block" })));
  const next = (p.suggest || []).map((s) => el("button", { type: "button", class: "sug",
    title: `Focus 25 minutes on ${s.what}`, onclick: () => run(`/focus 25 ${s.what}`) },
  icon("open", "ico sm"), el("span", { class: "grow" }, el("span", { class: "one", text: s.what }),
    el("span", { class: "two", text: [s.course, `due ${s.due}`, s.minutes ? `${hm(s.minutes)} in` : ""].filter(Boolean).join(" · ") }))));
  return widget("focus", { actions: [adding("Focus on something, for as long as you choose", FORMS.focus)] },
    el("div", { class: "centered" }, hero,
      next.length ? el("div", { class: "sugs fit" }, ...next) : null,
      el("div", { class: "acts" }, button("25 min", () => run("/focus 25 deep work"), "btn sm"),
        button("50 min", () => run("/focus 50 deep work"), "btn sm"))));
}

function moneyW() {
  const p = panelOf("money");
  const b = panelOf("bank");
  const bank = b && !b.waiting && b.cash_cents !== undefined ? b : null;
  if (!bank && (!p || p.spent_cents == null)) return null;
  const spent = p?.spent_cents ?? bank?.spent_week_cents ?? 0;
  const budget = p?.budget_cents || 0;
  const share = budget ? spent / budget : 0;
  const acts = [adding("Log cash spending", FORMS.spent)];
  if (bank) acts.push(tool("refresh", "Ask the bank now", () => run("/bank sync")));
  const spentBox = el("div", { class: "stat-b" }, el("small", { text: "Spent this week" }),
    el("div", { class: "num", text: money$(spent, { whole: spent >= 100000 }) },
      el("small", { text: budget ? `of ${money$(budget)}` : "" })),
    budget
      ? el("div", { class: "meter", role: "img", "aria-label": `${Math.round(share * 100)}% of the week's budget` },
        el("i", { class: share >= 1 ? "over" : share >= 0.85 ? "hot" : "", style: `width:${Math.min(100, share * 100).toFixed(0)}%` }))
      : el("button", { type: "button", class: "link", text: "Set a weekly budget", onclick: (e) => openForm(e.currentTarget.closest(".card"), FORMS.budget) }));
  const top = el("div", { class: "money-top" }, spentBox);
  if (bank && bank.cash_cents != null) {
    const over = budget && spent > budget;
    top.append(el("div", { class: "stat-b" }, el("small", { text: !budget ? "Checking & savings" : over ? "Over this week" : "Left this week" }),
      el("div", { class: `num ${over ? "bad" : ""}`, text: budget ? money$(Math.abs(budget - spent), { whole: Math.abs(budget - spent) >= 10000 }) : money$(bank.cash_cents, { whole: true }) }),
      el("div", { class: "sub", text: [budget ? `${money$(bank.cash_cents, { whole: true })} in the bank` : "",
        bank.owed_cents > 0 ? `card owes ${money$(bank.owed_cents, { whole: true })}` : ""].filter(Boolean).join(" · ") })));
  } else if (p?.earned_cents != null) {
    top.append(el("div", { class: "stat-b" }, el("small", { text: "Earned (est.)" }), el("div", { class: "num", text: money$(p.earned_cents, { whole: true }) })));
  }
  let middle = null;
  if (bank && (bank.categories || []).length) {
    middle = barList(bank.categories.slice(0, 4).map((c) => ({ label: c.name.replace(/^./, (x) => x.toUpperCase()), value: c.cents,
      said: money$(c.cents, { whole: true }) })), "cats");
  }
  const list = el("ul", { class: "fit" });
  const rows = bank ? (bank.recent || []).map((r) => ({ what: r.what, cents: -r.cents, day: r.day, pending: r.pending }))
    : (p?.recent || []).map((r) => ({ what: r.what, cents: r.cents, day: r.day }));
  for (const r of rows) {
    list.append(item({ one: r.what, two: r.pending ? "Pending" : "", val: valueOf(r.cents < 0 ? `+${money$(-r.cents)}` : money$(r.cents), r.day),
      cls: r.cents < 0 ? "in" : "" }));
  }
  if (!rows.length) list.append(el("li", { class: "sub", text: "Nothing spent yet this week." }));
  return widget("money", { meta: bank ? (bank.stale ? `Synced ${bank.synced}` : bank.error ? "Bank not answering" : "") : p?.earned_cents != null ? "" : "",
    metaCls: bank?.error ? "bad" : "", actions: acts }, top, el("div", { class: "money-main" }, middle, list));
}

// His investments, from the bank sync, priced with today's delayed quotes.
function portfolioW() {
  const b = panelOf("bank");
  const p = b?.portfolio;
  if (!p) return null;
  const up = (p.change_cents ?? 0) >= 0;
  const history = (b.invest_history || []).map((h) => h.cents);
  const head = el("div", { class: "pf-head" }, el("div", { class: "num big", text: money$(p.value_cents, { whole: p.value_cents >= 1000000 }) }),
    p.change_cents != null ? el("div", {}, change(p.change_pct, `${money$(Math.abs(p.change_cents))} (${Math.abs(p.change_pct).toFixed(2)}%)`), el("small", { class: "muted", text: " today" })) : null);
  const chart = history.length >= 2
    ? areaChart(history, { up: history[history.length - 1] >= history[0], say: (v) => money$(v, { whole: true }),
      when: (i) => new Date(`${b.invest_history[i].on}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" }) })
    : null;
  const list = el("ul", { class: "fit" });
  for (const h of p.holdings || []) {
    list.append(item({ one: h.symbol || h.name, two: h.symbol ? h.name : "", label: `${h.name}: ${money$(h.value_cents ?? 0)}`,
      val: el("span", { class: "val" }, el("span", { text: money$(h.value_cents ?? 0, { whole: (h.value_cents ?? 0) >= 100000 }) }),
        h.change_pct != null ? change(h.change_pct) : el("small", { text: `${Math.round((h.weight || 0) * 100)}%` })) }));
  }
  const gain = p.gain_cents != null ? `${p.gain_cents >= 0 ? "+" : ""}${money$(p.gain_cents, { whole: true })} all time` : "";
  return widget("portfolio", { meta: gain, metaCls: p.gain_cents != null ? (p.gain_cents >= 0 ? "good" : "bad") : "",
    actions: [asking("Ask about your portfolio", "How's my portfolio doing?")] }, head, chart, list);
}

let listShown = "";
function listsW() {
  const lists = panelOf("lists")?.lists || {};
  const names = Object.keys(lists);
  if (!names.length) return null;
  if (!names.includes(listShown)) listShown = names[0];
  const tabs = names.length > 1 ? el("div", { class: "tabs-row", role: "group", "aria-label": "Which list" },
    ...names.map((n) => el("button", { type: "button", class: "chip", "aria-pressed": String(n === listShown),
      onclick: () => { listShown = n; renderPanels(); } }, n.replace(/^./, (c) => c.toUpperCase()), el("b", { text: String(lists[n].length) })))) : null;
  const list = el("ul", { class: "fit" });
  lists[listShown].forEach((text, i) => {
    list.append(item({ one: text, lead: el("button", { type: "button", class: "tick square", "aria-pressed": "false",
      "aria-label": `Check off ${text}`, title: "Check off", onclick: () => run(`/list done ${listShown}: ${i + 1}`) }, icon("check")) }));
  });
  return widget("lists", { meta: names.length === 1 ? `${listShown.replace(/^./, (c) => c.toUpperCase())} · ${lists[listShown].length}` : "",
    actions: [tool("plus", `Add to the ${listShown} list`, (e) => openForm(e.currentTarget.closest(".card"), listForm(listShown)))] },
  tabs, list);
}

function daysBetween(fromIso, toIso) {
  return Math.round((Date.parse(toIso) - Date.parse(fromIso)) / 86400000);
}

function countdownsW() {
  const items = panelOf("countdowns")?.items || [];
  if (!items.length) return null;
  const list = el("ul", { class: "fit cd" });
  for (const c of items) {
    const total = c.created_on ? Math.max(1, daysBetween(c.created_on, c.date)) : 90;
    const gone = Math.max(0, Math.min(1, 1 - c.days / total));
    const on = new Date(`${c.date}T12:00:00`);
    list.append(el("li", { class: "li", "aria-label": `${c.name}: ${c.days === 0 ? "today" : `${c.days} days`}` },
      el("span", { class: "days" }, c.days === 0 ? "Today" : String(c.days), c.days === 0 ? null : el("small", { text: "d" })),
      el("span", { class: "grow" }, el("span", { class: "one", text: c.name, title: c.name })),
      el("span", { class: "val" }, el("small", { text: on.toLocaleDateString("en-US", { month: "short", day: "numeric" }) })),
      el("span", { class: "meter", "aria-hidden": "true" }, el("i", { style: `width:${(gone * 100).toFixed(0)}%` }))));
  }
  return widget("countdowns", { actions: [adding("Add a countdown", FORMS.countdown)] }, list);
}

function birthdaysW() {
  const items = panelOf("birthdays")?.items || [];
  if (!items.length) return null;
  const next = items[0];
  const hero = items.length <= 3 ? el("div", { class: "centered feature" },
    el("div", {}, el("div", { class: "num big", text: next.days === 0 ? "Today" : String(next.days) }),
      el("div", { class: "sub", text: next.days === 0 ? "" : `day${next.days === 1 ? "" : "s"} until` })),
    el("div", {}, el("div", { class: "num-md one", text: `${next.name}'s birthday`, title: `${next.name}'s birthday` }),
      el("div", { class: "sub", text: next.date }))) : null;
  const list = el("ul", { class: "fit" });
  for (const b of hero ? items.slice(1) : items) {
    list.append(item({ one: b.name, two: b.date, val: el("span", { class: `pill ${b.days <= 1 ? "now" : ""}`,
      text: b.days === 0 ? "Today" : b.days === 1 ? "Tomorrow" : `In ${b.days} days` }) }));
  }
  return widget("birthdays", { actions: [adding("Add a birthday", FORMS.birthday),
    asking("Ask for gift ideas", `Give me gift ideas for ${items[0].name}'s birthday`)] }, hero, list.children.length ? list : null,
  hero && items.length === 1 ? el("div", { class: "acts center" }, button("Gift ideas", () => askHer(`Give me gift ideas for ${next.name}'s birthday`), "btn sm")) : null);
}

function clientsW() {
  const p = panelOf("clients");
  const items = p?.items || [];
  if (!items.length) return null;
  const list = el("ul", { class: "fit" });
  for (const c of items) {
    const worth = c.value_cents != null ? `$${Math.round(c.value_cents / 100).toLocaleString("en-US")}` : "";
    list.append(item({ one: c.name, two: [c.follow_up ? `Follow up ${c.follow_up}` : "", c.next].filter(Boolean).join(" · "),
      val: el("span", { class: "val" }, el("span", { class: `pill ${c.late ? "bad" : ""}`, text: c.stage.replace(/^./, (x) => x.toUpperCase()) }),
        worth ? el("small", { text: worth }) : null) }));
  }
  return widget("clients", { actions: [adding("Add a client", FORMS.client)] },
    p.pipeline_cents ? el("div", { class: "num" }, `$${Math.round(p.pipeline_cents / 100).toLocaleString("en-US")}`, el("small", { text: "in the pipeline" })) : null, list);
}

// School, in one widget: what's due soon (tick one handed in), what's overdue, the grades.
function schoolW() {
  const data = latest;
  if (!data) return null;
  const overdue = (data.overdue || []).length;
  const list = el("ul", { class: "fit sc" });
  for (const a of data.due || []) {
    const tick = careful(el("button", { type: "button", class: "tick square", "aria-pressed": "false",
      "aria-label": `Mark handed in: ${a.title}`, title: "Mark handed in" }, icon("check")),
    `/done ${a.title}`, `Press again: mark "${clip(a.title, 40)}" handed in`);
    list.append(item({ lead: tick, one: a.title, two: [a.course, fromNow(a.at)].filter(Boolean).join(" · ") }));
  }
  if (!list.children.length) list.append(el("li", { class: "sub", text: "Nothing due in the next three days." }));
  const grades = (data.grades || []).slice(0, 6);
  const gradeRow = grades.length ? el("div", { class: "gr" }, ...grades.map((g) => {
    const score = Number(g.score) || 0;
    return el("span", { class: score < 80 ? "low" : "", title: g.course },
      `${String(g.course || "").split(/\s+/)[0]} `, el("b", { text: `${Math.round(score)}` }));
  })) : null;
  const meta = overdue ? `${overdue} overdue` : data.due_week ? `${data.due_week} this week` : "";
  const box = widget("school", { meta, metaCls: overdue ? "bad" : "",
    actions: [tool("open", "See what's due and overdue", () => openWaiting(overdue ? "overdue" : "due"))] }, list, gradeRow);
  return box;
}

const STEP_NAMES = ["Plan", "Code", "Test", "CI", "Ready"];

// The workshop item to show, and its step (1-5) and whether that step is under way.
function workshopNow() {
  const items = shop?.items || [];
  const pick = (...statuses) => items.find((i) => statuses.includes(i.status));
  const item = pick("building", "deploying", "accepted") || pick("ready") || pick("queued") || pick("planned", "idea");
  if (!item) return null;
  const s = doing.server;
  let step = 1, active = false, word = item.status;
  if (item.status === "building") {
    step = s.state === "building" && s.step ? s.step : 2;
    active = true;
    word = ["Planning", "Building", "Testing", "Waiting on CI", "Ready"][step - 1];
  } else if (item.status === "deploying" || item.status === "accepted") { step = 5; active = true; word = "Going live"; }
  else if (item.status === "ready") { step = 5; active = true; word = "Ready for you"; }
  else if (item.status === "queued") { step = 2; word = "Queued"; }
  else if (item.status === "planned") { step = 2; word = "Planned"; }
  else { step = 1; active = s.state === "building" && s.step === 1; word = active ? "Planning" : "An idea"; }
  return { item, step, active, word };
}

function workshopW() {
  const now = workshopNow();
  if (!now) return null;
  const segs = el("div", { class: "steps", "aria-hidden": "true" });
  for (let i = 1; i <= 5; i++) segs.append(el("i", { class: i < now.step ? "d" : i === now.step && now.active ? "a" : "" }));
  const labels = el("div", { class: "step-l", "aria-hidden": "true" });
  STEP_NAMES.forEach((name, i) => labels.append(i + 1 === now.step && now.active ? el("b", { text: name }) : el("span", { text: name })));
  const others = (shop?.items || []).filter((i) => ["idea", "planned", "queued"].includes(i.status) && i.id !== now.item.id).length;
  return widget("workshop", { actions: [
    adding("Ask her to build something", FORMS.idea), tool("open", "Open the workshop", () => show("workshop"))] },
  el("div", { class: "spread" },
    el("div", { class: "ring-row" }, el("div", { class: "num-md grow one", text: now.item.title, title: now.item.title }),
      el("span", { class: `pill ${now.active ? "hot" : ""}`, text: now.word })),
    el("div", {}, segs, labels),
    el("div", { class: "sub", text: others ? `${others} more waiting in the workshop` : "Nothing else queued" }),
    el("div", { class: "acts" }, now.item.status === "ready" ? button("Review", () => show("workshop"), "btn sm primary")
      : button("Open the workshop", () => show("workshop"), "btn sm"))));
}

function learnedW() {
  const facts = (latest?.learned || []).filter((f) => f.today);
  if (!facts.length) return null;
  const list = el("ul", { class: "fit" });
  for (const f of facts) list.append(item({ one: f.value, tail: forgetButton(f) }));
  return widget("learned", { actions: [adding("Tell her something to keep", FORMS.remember), tool("open", "See all she knows", () => show("memory"))] }, list);
}

function forgetButton(f) {
  return careful(tool("x", `Forget: ${f.value}`, null, "icon-btn sm hover"), () => post("/api/memory/forget", { key: f.key }),
    `Press again to forget: ${clip(f.value, 48)}`);
}

function gradesW() {
  const grades = latest?.grades || [];
  if (!grades.length) return null;
  const list = el("ul", { class: "fit" });
  for (const g of grades) {
    const score = Number(g.score) || 0;
    list.append(el("li", { class: "li", style: "flex-wrap:wrap;row-gap:6px" },
      el("span", { class: "grow" }, el("span", { class: "one", text: g.course })),
      el("span", { class: `val${score < 80 ? " bad" : ""}`, text: `${score.toFixed(1).replace(/\.0$/, "")}%` }),
      el("span", { class: "meter", style: "width:100%;height:4px", "aria-hidden": "true" },
        el("i", { class: score < 80 ? "over" : "", style: `width:${Math.max(0, Math.min(100, score))}%` }))));
  }
  return widget("grades", { actions: [asking("Ask about your grades", "How are my grades looking?")] }, list);
}

function weekW() {
  const days = latest?.week || [];
  if (!days.length || !days.some((d) => d.count)) return null;
  const most = Math.max(...days.map((d) => d.count));
  const bars = el("div", { class: "bars7" });
  for (const d of days) {
    bars.append(el("span", { class: d.today ? "today" : "", "aria-label": `${d.day}: ${d.count} thing${d.count === 1 ? "" : "s"}` },
      el("i", { class: d.count ? "y" : "", style: `height:${d.count ? Math.max(12, (d.count / most) * 100).toFixed(0) : 6}%` }), d.day[0]));
  }
  const heavy = days.filter((d) => d.count === most);
  return widget("week", { actions: [asking("Ask about the week", "What does my week look like?")] }, bars,
    el("div", { class: "sub", text: heavy.length === 1 && most >= 3 ? `${heavy[0].day} is the heavy one.` : "Spread out evenly." }));
}

function collegesW() {
  const schools = panelOf("colleges")?.schools || [];
  if (!schools.length) return null;
  const list = el("ul", { class: "fit" });
  for (const s of schools) {
    const pips = el("span", { class: "pips", "aria-hidden": "true" });
    for (const t of [...(s.checklist || [])].sort((a, b) => b.done - a.done)) pips.append(el("i", { class: t.done ? "y" : "" }));
    const done = (s.checklist || []).filter((t) => t.done).length;
    const when = s.days == null ? "" : s.days < 0 ? "Late" : s.days === 0 ? "Today" : `${s.days}d`;
    list.append(item({ one: s.plan ? `${s.name} ${s.plan}` : s.name, two: `${done} of ${(s.checklist || []).length} done`,
      val: el("span", { class: "acts" }, pips, when ? el("span", { class: `pill ${when === "Late" ? "bad" : ""}`, text: when }) : null) }));
  }
  return widget("colleges", {}, list);
}

function engineW() {
  const e = latest?.engine;
  if (!e) return null;
  const up = e.up_since ? hm((Date.now() - Date.parse(e.up_since)) / 60000) : "—";
  const share = e.job_budget ? Math.min(1, e.job_calls / e.job_budget) : 0;
  return widget("engine", { actions: [tool("open", "Open the engine room", () => show("engine"))] },
    el("div", { class: "stat" }, el("span", {}, el("b", { text: String(e.calls_today) }), "Calls today"), el("span", {}, el("b", { text: up }), "Up")),
    el("div", { class: "meter", role: "img", "aria-label": `${e.job_calls} of ${e.job_budget} scheduled calls` },
      el("i", { class: share >= 0.9 ? "over" : "", style: `width:${Math.max(2, share * 100).toFixed(0)}%` })),
    el("div", { class: "sub", text: `Scheduled ${e.job_calls}/${e.job_budget} · bulk ${e.bulk_calls}/${e.bulk_budget}` }));
}

function workW() {
  const w = latest?.work;
  if (!w || (!w.now && !w.next)) return null;
  let big, sub;
  if (w.now) { big = `Until ${short(minuteOf(w.now.ends_at))}`; sub = "On shift now"; }
  else {
    const minutes = (Date.parse(w.next.starts_at) - Date.now()) / 60000;
    big = minutes < 24 * 60 ? hm(minutes) : w.next.when;
    sub = minutes < 24 * 60 ? `Shift at ${short(minuteOf(w.next.starts_at))}` : "Next shift";
  }
  return widget("work", {}, el("div", { class: "num", style: "color:var(--work)", text: big }), el("div", { class: "sub", text: sub }),
    el("div", { class: "sub", text: `${w.week_hours}h this week (${w.done_hours}h done) · ${w.last_week_hours}h last week` }));
}

function linesW(p) {
  const list = el("ul", { class: "fit" });
  for (const text of p.lines || []) list.append(item({ one: text }));
  return widget(p.skill, { title: p.title || PANEL_NAMES[p.skill] || p.skill }, list);
}

// A widget with nothing in it yet: what it's for, and the button that fills it.
const EMPTY = {
  whoop: { text: "Recovery, last night's sleep and today's strain from your Whoop.", note: "Connect it on the box: DEPLOY.md, section 7j.",
    ask: ["How do I connect it?", "How do I connect my Whoop to you?"] },
  workouts: { text: "Log a run, a lift or a ride, and she keeps count against a weekly goal.", form: FORMS.workout, cta: "Log a workout" },
  markets: { text: "The stocks and coins you follow, with today's move.", form: FORMS.watch, cta: "Watch a ticker" },
  news: { text: "The top headlines, and the topics you follow.", form: FORMS.follow, cta: "Follow a topic" },
  habits: { text: "The things you mean to do every day. Tick them off here and keep the streak.", form: FORMS.habit, cta: "Track a habit" },
  money: { text: "What you spend each week, against a budget if you set one. Connect your bank and it fills in on its own.",
    note: "Your bank and card, read-only: DEPLOY.md, section 7k.", form: FORMS.spent, cta: "Log spending" },
  portfolio: { text: "Your Fidelity accounts: what they're worth, today's move, and each holding.",
    note: "Connect it read-only through SimpleFIN: DEPLOY.md, section 7k.", ask: ["How do I connect it?", "How do I connect my bank and Fidelity to you?"] },
  countdowns: { text: "Days to go until the things you're waiting for.", form: FORMS.countdown, cta: "Add a countdown" },
  lists: { text: "Groceries, packing, to-dos: any list you name. Check things off here.", form: listForm("grocery"), cta: "Start a list" },
  birthdays: { text: "Birthdays coming up, so none sneak past.", form: FORMS.birthday, cta: "Add a birthday" },
  clients: { text: "Your clients, where each one stands, and who's due a follow-up.", form: FORMS.client, cta: "Add a client" },
  workshop: { text: "Anything you wish she could do, she can build. You approve it before it goes live.", form: FORMS.idea, cta: "Suggest something" },
  learned: { text: "What she picks up about you today shows here.", form: FORMS.remember, cta: "Tell her something" },
};

function emptyWidget(id) {
  const loaded = new Set(latest?.system?.skills || []);
  const spec = EMPTY[id];
  if (!spec || !(loaded.has(id) || CORE_PANELS.has(id) || id === "whoop" || id === "portfolio")) return null;
  const acts = [];
  if (spec.form) acts.push(button(spec.cta, (e) => openForm(e.currentTarget.closest(".card"), spec.form), "btn sm primary"));
  if (spec.ask) acts.push(button(spec.ask[0], () => askHer(spec.ask[1]), "btn sm"));
  const box = widget(id, {}, el("div", { class: "w-empty" }, el("p", { text: spec.text }),
    spec.note ? el("p", { class: "note", text: spec.note }) : null, acts.length ? el("div", { class: "acts" }, ...acts) : null));
  box.classList.add("w-blank");
  return box;
}

const BUILT = {
  weather: weatherW, whoop: whoopW, workouts: workoutsW, markets: marketsW, inbox: inboxW, habits: habitsW,
  news: newsW, focus: focusW, money: moneyW, portfolio: portfolioW, lists: listsW, countdowns: countdownsW, birthdays: birthdaysW,
  clients: clientsW, school: schoolW, workshop: workshopW, learned: learnedW, grades: gradesW, week: weekW,
  colleges: collegesW, engine: engineW, work: workW,
};

// Every widget there could be, in order: the ones the page draws, then every other skill's.
function panelIds() {
  const ids = Object.keys(latest?.prefs?.defaults || BUILT).filter((id) => !FEEDS.has(id));
  for (const p of latest?.panels || []) if (!ids.includes(p.skill) && !FEEDS.has(p.skill)) ids.push(p.skill);
  return ids;
}

function panelOn(id) {
  const prefs = latest?.prefs || {};
  if ((prefs.shown || []).includes(id)) return true;
  if ((prefs.hidden || []).includes(id)) return false;
  return (prefs.defaults || {})[id] ?? true;
}

function buildPanel(id) {
  if (BUILT[id]) {
    try { return BUILT[id](); } catch (error) { console.warn(`widget ${id}:`, error); }
  }
  const p = panelOf(id);
  return p && (p.lines || []).length ? linesW(p) : null;
}

function renderPanels() {
  // Don't redraw under his hands: a form open, or typing in one. It redraws when he's done.
  if (formOpen) { redrawWaiting = true; return; }
  const box = $("#panels");
  const out = [];
  if (wide.matches) {
    // Every widget that's on: the ones with something in them first, then the ones saying how to
    // fill them, so the first page is his real numbers.
    const empty = [];
    for (const id of panelIds()) {
      if (!panelOn(id)) continue;
      const filled = buildPanel(id);
      const node = filled || emptyWidget(id);
      if (node) { node.dataset.id = id; (filled ? out : empty).push(node); }
    }
    out.push(...empty);
  } else {
    // The phone's three, as he chose them; with nothing in one yet, the next widget that has something.
    const chosen = latest?.prefs?.phone || ["whoop", "markets", "habits"];
    const count = latest?.prefs?.phone_count || 3;
    const order = [...chosen, ...panelIds().filter((i) => !chosen.includes(i) && panelOn(i))];
    for (const id of order) {
      if (out.length >= count) break;
      const node = buildPanel(id);
      if (node) out.push(node);
    }
    for (const id of order) {
      if (out.length >= count) break;
      const node = emptyWidget(id);
      if (node) out.push(node);
    }
  }
  // Only the first draw rises in; a refresh swaps them quietly.
  if (box.children.length) out.forEach((n) => { n.style.animation = "none"; });
  box.replaceChildren(...out);
  layoutPanels();
}

// -- one page, no scrolling: the widgets fill the space, in pages when there are more ---------------
// Columns and rows come from the room there is (each widget at least MIN_W by MIN_H). Widgets go
// in order into the first place they fit; what doesn't fit starts the next page. The last row's
// widgets widen to fill it, so no hole is left.

const MIN_W = 212, MIN_H = 184, GAP = 12, MAX_COLS = 6, MAX_ROWS = 4;
let page = 0;

function paginate(spans, cols, rows) {
  const pages = [];
  spans.forEach((w, i) => {
    for (let p = 0; ; p++) {
      pages[p] ??= { cells: Array.from({ length: rows }, () => Array(cols).fill(-1)), items: [] };
      const cells = pages[p].cells;
      let spot = null;
      for (let r = 0; r < rows && !spot; r++) {
        for (let c = 0; c + w <= cols && !spot; c++) {
          if (cells[r].slice(c, c + w).every((x) => x === -1)) spot = { r, c };
        }
      }
      if (!spot) continue;
      for (let c = spot.c; c < spot.c + w; c++) cells[spot.r][c] = i;
      pages[p].items.push({ i, r: spot.r, c: spot.c, w });
      break;
    }
  });
  // Widen each row's widgets into its empty cells, a column at a time, from the right.
  for (const pg of pages) {
    for (let r = 0; r < rows; r++) {
      const row = pg.items.filter((it) => it.r === r).sort((a, b) => a.c - b.c);
      if (!row.length) continue;
      let spare = cols - row.reduce((n, it) => n + it.w, 0);
      for (let k = row.length - 1; spare > 0; k = (k - 1 + row.length) % row.length, spare--) row[k].w++;
      let c = 0;
      for (const it of row) { it.c = c; c += it.w; }
    }
    pg.rows = Math.max(...pg.items.map((it) => it.r)) + 1;
  }
  return pages;
}

function layoutPanels() {
  if (latest && document.body.dataset.view === "today") renderPeek(latest);
  const grid = $("#panels");
  const cards = [...grid.children];
  const pager = $("#pager");
  if (!wide.matches || !cards.length || document.body.dataset.view !== "today") {
    if (!wide.matches) { grid.removeAttribute("style"); cards.forEach((c) => { c.hidden = false; c.style.removeProperty("grid-area"); }); pager.hidden = true; fitLists(); }
    return;
  }
  const board = grid.parentElement;
  const W = grid.clientWidth, H = board.clientHeight;
  if (!W || !H) return;
  const cols = Math.max(1, Math.min(MAX_COLS, Math.floor((W + GAP) / (MIN_W + GAP))));
  const spans = cards.map((c) => Math.min(cols, c.classList.contains("w-blank") ? 1 : spanOf(c.dataset.id)));
  const rowsIn = (h) => Math.max(1, Math.min(MAX_ROWS, Math.floor((h + GAP) / (MIN_H + GAP))));
  let pages = paginate(spans, cols, rowsIn(H));
  if (pages.length > 1) pages = paginate(spans, cols, rowsIn(H - 38));
  page = Math.min(page, pages.length - 1);
  const shown = pages[page];
  // A page's rows share the height, so a last page with fewer rows has taller widgets, not a hole.
  const rows = shown.rows;
  grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
  grid.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  const at = new Map(shown.items.map((it) => [it.i, it]));
  cards.forEach((card, i) => {
    const it = at.get(i);
    card.hidden = !it;
    if (it) card.style.gridArea = `${it.r + 1} / ${it.c + 1} / span 1 / span ${it.w}`;
  });
  pager.hidden = pages.length < 2;
  if (pages.length > 1) {
    const dots = $("#page-dots");
    dots.replaceChildren(...pages.map((_, n) => el("button", { type: "button", "aria-label": `Widgets, page ${n + 1} of ${pages.length}`,
      "aria-current": String(n === page), onclick: () => { page = n; layoutPanels(); } })));
    $("#page-prev").disabled = page === 0;
    $("#page-next").disabled = page === pages.length - 1;
  }
  fitLists();
}

// Each list shows the rows that fit, and says how many more.
function fitLists() {
  for (const list of document.querySelectorAll("#panels .fit")) {
    if (list.closest("[hidden]")) continue;
    const rows = [...list.children].filter((r) => !r.classList.contains("more-n"));
    list.querySelector(".more-n")?.remove();
    rows.forEach((r) => { r.hidden = false; });
    let n = rows.length;
    while (n > 1 && list.scrollHeight > list.clientHeight + 1) rows[--n].hidden = true;
    if (n < rows.length) {
      const more = el("li", { class: "more-n", text: `+${rows.length - n} more` });
      list.append(more);
      while (n > 1 && list.scrollHeight > list.clientHeight + 1) { rows[--n].hidden = true; more.textContent = `+${rows.length - n} more`; }
    }
  }
}

$("#page-prev").addEventListener("click", () => { page = Math.max(0, page - 1); layoutPanels(); });
$("#page-next").addEventListener("click", () => { page += 1; layoutPanels(); });
$("#edit-widgets").addEventListener("click", () => {
  show("engine");
  $("#widgets-block").scrollIntoView({ block: "start", behavior: calm.matches ? "auto" : "smooth" });
});
if ("ResizeObserver" in window) new ResizeObserver(() => requestAnimationFrame(layoutPanels)).observe($(".board"));
document.fonts?.ready.then(() => layoutPanels());

function renderToday(data) {
  renderHeader(data);
  renderTimeline();
  renderPanels();
  renderStarters(data);
  if (waiting.open) renderNeeds();
}

// Workout kinds, offered as he types.
document.body.append(el("datalist", { id: "workout-kinds" }, ...WORKOUT_KINDS.map((k) => el("option", { value: k }))));

// -- memory ---------------------------------------------------------------------------------

function fill(id, rows, render, empty) {
  const list = $(id);
  list.replaceChildren();
  if (!rows || !rows.length) {
    list.append(el("li", {}, el("span", { class: "empty", text: empty })));
    return;
  }
  for (const row of rows) list.append(render(row));
}

function line(text, meta = "", ...actions) {
  return el("li", {}, el("span", { class: "text", text }), meta ? el("span", { class: "meta2", text: meta }) : null,
    actions.length ? el("span", { class: "acts" }, ...actions) : null);
}

function renderLearned() {
  const facts = latest?.learned || [];
  const words = $("#learned-filter").value.trim().toLowerCase();
  const shown = words ? facts.filter((f) => `${f.value} ${f.key} ${f.source}`.toLowerCase().includes(words)) : facts;
  $("#learned-count").textContent = facts.length ? String(facts.length) : "";
  fill("#learned", shown, (f) => el("li", {},
    el("span", { class: "text" }, f.value, el("span", { class: "sub", text: f.key.replace(/^learned\./, "").replace(/\./g, " · ") + (f.source ? ` · ${f.source}` : "") })),
    el("span", { class: "acts" }, button("Forget", () => post("/api/memory/forget", { key: f.key }), "btn danger", { "aria-label": `Forget: ${f.value}` }))),
  words ? "Nothing she knows matches that." : "Nothing yet. Tell her: remember that I'm vegetarian now.");
}
$("#learned-filter").addEventListener("input", renderLearned);

function renderMemory(data) {
  renderLearned();
  $("#loose-count").textContent = (data.loose || []).length ? String(data.loose.length) : "";
  fill("#loose", data.loose, (r) => line(r.summary, r.when, button("Done", () => post(`/api/followups/${r.id}/close`), "btn",
    { "aria-label": `Done: ${r.summary}` })), "No loose ends.");
  const diary = $("#diary");
  diary.replaceChildren();
  if (!data.diary || !data.diary.length) {
    diary.append(el("li", {}, el("span", { class: "empty", text: "Her first entry comes tonight, after you've talked." })));
  }
  for (const d of data.diary || []) diary.append(el("li", {}, el("span", { class: "when", text: d.when }), el("span", { text: d.text })));
}

// -- engine -----------------------------------------------------------------------------------

const PROVIDERS = { claude_code: "Claude, on your plan", anthropic: "Anthropic API", groq: "Groq", local: "Local model",
  piper: "Piper, on the box" };
function provider(name) { return PROVIDERS[name] || name; }

function renderEngine(data) {
  const sys = data.system;
  const tiles = $("#system");
  tiles.replaceChildren();
  const rows = [
    ["Database", sys.database ? "Connected" : "Unreachable", sys.database],
    ["Telegram", sys.telegram ? "Polling" : "Off", sys.telegram],
    ["Main model", provider(sys.main)],
    ["Bulk model", provider(sys.bulk)],
    ["Spoken replies", sys.quick ? provider(sys.quick) + ", then the main model to act" : "The main model"],
    ["Local model", sys.local || "None set up"],
    ["Her voice", sys.voice.replace(/^(\w+)/, (m) => provider(m))],
    ["Quiet hours", sys.quiet],
    ["Web lookups", sys.web_lookup ? "On" : "Off", sys.web_lookup ? true : undefined],
    ["Microphone here", data.talk?.hears ? "On" : "Needs a Groq key", data.talk?.hears ? true : undefined],
  ];
  for (const [label, value, good] of rows) {
    tiles.append(el("div", { class: `tile ${good === true ? "ok" : good === false ? "bad" : ""}`, role: "listitem" },
      el("span", { class: "label", text: label }), el("span", { class: "value", text: value })));
  }
  const skills = $("#skills");
  skills.replaceChildren(...(sys.skills || []).map((name) => el("li", { text: name })));
  if (!skills.children.length) skills.append(el("li", { text: "None loaded" }));

  const body = $("#jobs tbody");
  body.replaceChildren();
  const jobs = [...(data.jobs || [])].sort((a, b) => (b.status === "failed") - (a.status === "failed"));
  for (const job of jobs) {
    const status = job.status === "ok" ? "ok" : job.status === "failed" ? "failed" : "";
    body.append(el("tr", { class: status === "failed" ? "failed" : "" },
      el("td", {}, el("span", { class: "job-name", text: jobName(job.name) }),
        el("span", { class: `pill ${status === "ok" ? "good" : status === "failed" ? "bad" : ""}`, text: job.status }),
        el("span", { class: "job-id", text: job.name }),
        job.error ? el("span", { class: status === "failed" ? "job-error" : "job-note", text: job.error }) : null),
      el("td", { class: "when", text: job.last || "—" }),
      el("td", { class: "when", text: job.next || "—" }),
      el("td", {}, button("Run now", () => post(`/api/jobs/${job.name}/run`), "btn sm", { "aria-label": `Run ${jobName(job.name)} now` }))));
  }

  fill("#trust", data.trust, (t) => line(`${t.action} → ${t.target}`,
    t.hard ? "hard line" : `${t.state}${t.state === "trusted" ? "" : ` · ${t.streak}/10`}`,
    ...(t.hard || t.state !== "trusted" ? [] : [button("Take back", () => post("/api/trust/revoke", { action: t.action, target: t.target }), "btn sm danger")])),
  "She asks before everything.");
  const most = Math.max(1, ...(data.usage || []).map((u) => u.calls));
  fill("#usage", data.usage, (u) => el("li", {},
    el("span", { class: "text", text: `${u.lane} · ${provider(u.provider)}` }),
    el("span", { class: "meta2", text: `${u.calls} call${u.calls === 1 ? "" : "s"}${u.failures ? `, ${u.failures} failed` : ""}` }),
    el("span", { class: "meter", "aria-hidden": "true" }, el("i", { class: u.failures ? "failed" : "", style: `width:${(u.calls / most) * 100}%` }))),
  "No model calls in the last day.");
  renderPrefs(false);
}

// Which panels show, and the phone's three. Saved on the box, so the laptop and phone agree.
let prefsDirty = false;
function renderPrefs(force) {
  if (prefsDirty && !force) return;  // don't undo what he's ticking between refreshes
  const ids = panelIds();
  const phone = latest?.prefs?.phone || [];
  const limit = latest?.prefs?.phone_count || 3;
  const shown = $("#prefs-shown");
  const onPhone = $("#prefs-phone");
  const box = (name, id, checked) => el("label", {}, el("input", { type: "checkbox", name, value: id, checked }),
    el("span", { text: PANEL_NAMES[id] || panelOf(id)?.title || id }));
  shown.replaceChildren(...ids.map((id) => box("shown", id, panelOn(id))));
  onPhone.replaceChildren(...ids.map((id) => box("phone", id, phone.includes(id))));
  $("#prefs-phone-count").textContent = `(up to ${limit})`;
  limitPhone();
}

function limitPhone() {
  const limit = latest?.prefs?.phone_count || 3;
  const boxes = [...document.querySelectorAll('#prefs-phone input')];
  const full = boxes.filter((b) => b.checked).length >= limit;
  boxes.forEach((b) => { b.disabled = full && !b.checked; });
}

$("#prefs-form").addEventListener("change", (event) => {
  prefsDirty = true;
  if (event.target.name === "phone") limitPhone();
});
$("#prefs-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const all = [...document.querySelectorAll('#prefs-shown input')];
  const body = {
    shown: all.filter((b) => b.checked).map((b) => b.value),
    hidden: all.filter((b) => !b.checked).map((b) => b.value),
    phone: [...document.querySelectorAll('#prefs-phone input')].filter((b) => b.checked).map((b) => b.value),
  };
  prefsDirty = false;
  await post("/api/prefs", body);
});

$("#sync-now").addEventListener("click", () => post("/api/jobs/entity_sync/run"));
$("#signout").addEventListener("click", async () => {
  await fetch("/app/logout", { method: "POST", headers: HEAD, credentials: "same-origin" });
  location.href = "/app/login";
});

// -- the workshop ------------------------------------------------------------------------------

const ORIGIN = { him: "your idea", her: "her idea" };
const MOVING = { queued: "queued", building: "building now…", deploying: "going live…", accepted: "merged; waiting for the box" };

async function shopPost(id, verb, body) {
  await post(`/api/workshop/${id}/${verb}`, body);
  loadWorkshop();
}

function fileKind(path) {
  if (path.startsWith("sloane/skills/")) return ["skill", "skill"];
  if (path.startsWith("tests/")) return ["test", "test"];
  if (path.startsWith("sql/")) return ["data", ""];
  if (path.endsWith(".md")) return ["docs", ""];
  return ["code", ""];
}

function shopCard(item, actions = [], opts = {}) {
  const card = el("article", { class: `shop-card ${item.status}` });
  card.append(el("div", { class: "shop-card-head" },
    el("h3", { text: item.title }),
    el("span", { class: `tag ${item.origin}`, text: ORIGIN[item.origin] || item.origin })));
  const meta = [MOVING[item.status] || (opts.showStatus ? item.status.replace("_", " ") : ""), item.when].filter(Boolean).join(" · ");
  if (meta) card.append(el("p", { class: "shop-meta", text: meta }));
  const text = opts.full ? item.summary || item.plan || item.request : item.plan || item.request;
  if (text) {
    const body = el("div", { class: "shop-body" });
    body.innerHTML = markdown(text);
    card.append(body);
  }
  if (item.files && item.files.length && opts.full) {
    const list = el("ul", { class: "shop-files", "aria-label": "Files changed" });
    for (const f of item.files) {
      const [label, cls] = fileKind(f);
      list.append(el("li", {}, el("span", { class: `what ${cls}`, text: label }), el("span", { text: f })));
    }
    if (item.status === "ready") card.append(list);
    else card.append(el("details", { class: "shop-more" }, el("summary", { text: `${item.files.length} file${item.files.length === 1 ? "" : "s"} changed` }), list));
  }
  if (item.checks && opts.full) card.append(el("p", { class: "shop-checks", text: "Checks: " + item.checks }));
  if (opts.full && item.request && item.summary) {
    card.append(el("details", { class: "shop-more" }, el("summary", { text: "What was asked" }), el("p", { text: item.request })));
  }
  if (opts.full && item.plan && item.summary) {
    const plan = el("div", { class: "shop-body" });
    plan.innerHTML = markdown(item.plan);
    card.append(el("details", { class: "shop-more" }, el("summary", { text: "The plan" }), plan));
  }
  if (item.error) card.append(el("p", { class: "shop-error", text: item.error }));
  if (item.feedback) card.append(el("p", { class: "shop-meta", text: `You said: ${item.feedback}` }));
  if (item.pr_url && opts.full && /^https:\/\//.test(item.pr_url)) {
    card.append(el("p", {}, el("a", { href: item.pr_url, target: "_blank", rel: "noopener noreferrer", text: "See every change on GitHub" })));
  }
  if (actions.length) card.append(el("div", { class: "acts shop-acts" }, ...actions));
  return card;
}

function denyButton(item) {
  return button("Deny", () => {
    const card = document.getElementById(`card-${item.id}`);
    if (!card || card.querySelector(".why")) return;
    const why = el("input", { class: "why", type: "text", maxlength: "500", placeholder: "Why not? (optional; she learns from it)", "aria-label": "Why not" });
    const confirm = button("Deny it", () => shopPost(item.id, "deny", { why: why.value }), "btn danger");
    card.append(el("div", { class: "acts shop-acts" }, why, confirm));
    why.focus();
  }, "btn danger", { "aria-label": `Deny ${item.title}` });
}

function lane(id, items, render, empty) {
  const box = $(id);
  box.replaceChildren();
  if (!items.length) { box.append(el("p", { class: "empty", text: empty })); return; }
  for (const item of items) {
    const card = render(item);
    card.id = `card-${item.id}`;
    box.append(card);
  }
}

let shopLoading = null;
function loadWorkshop() {
  shopLoading ??= fetchWorkshop().finally(() => { shopLoading = null; });
  return shopLoading;
}

async function fetchWorkshop() {
  let response;
  try { response = await api("/api/workshop"); } catch { return; }
  if (!response.ok) return;
  const data = await response.json();
  shop = data;
  const items = data.items || [];
  const by = (...statuses) => items.filter((i) => statuses.includes(i.status));
  const notice = $("#shop-notice");
  if (!data.building_on) {
    notice.hidden = false;
    notice.textContent = "Building is off until the GitHub token is on the box (DEPLOY 7h). Ideas and plans still work.";
  } else if (!data.upgrader) {
    notice.hidden = false;
    notice.textContent = "Accepted changes will merge, but go live only after you rerun the installer (it sets up the automatic upgrade).";
  } else {
    notice.hidden = true;
  }
  const ready = by("ready");
  const stages = [["Ideas", by("idea").length, ""], ["Planned", by("planned").length, ""],
    ["Building", by("queued", "building", "deploying", "accepted").length, ""], ["Ready for you", ready.length, "ready"],
    ["Live", by("live").length, ""]];
  $("#stages").replaceChildren(...stages.flatMap(([label, n, cls], i) => [
    i ? el("li", { class: "arrow", "aria-hidden": "true", text: "→" }) : null,
    el("li", { class: n && cls ? cls : "" }, el("b", { text: String(n) }), label)].filter(Boolean)));
  lane("#shop-ready", ready, (i) => shopCard(i, [
    button("Accept", () => shopPost(i.id, "accept"), "btn primary", { "aria-label": `Accept ${i.title}` }), denyButton(i)], { full: true }),
  "Nothing waiting on you.");
  lane("#shop-moving", by("queued", "building", "deploying", "accepted"), (i) => shopCard(i), "Nothing building.");
  lane("#shop-planned", by("planned", "idea"), (i) => shopCard(i, [
    button("Build tonight", () => shopPost(i.id, "build", { now: false }), "btn primary"),
    button("Build now", () => shopPost(i.id, "build", { now: true })),
    ...(i.status === "idea" ? [button("Plan it", () => shopPost(i.id, "plan"))] : []),
    button("Drop", () => shopPost(i.id, "drop"), "btn danger", { "aria-label": `Drop ${i.title}` })]),
  "No ideas yet. Add one above.");
  lane("#shop-live", by("live"), (i) => shopCard(i, [button("Undo", () => shopPost(i.id, "undo"), "btn danger", { "aria-label": `Undo ${i.title}` })], { full: true }),
    "Nothing from the workshop is live yet.");
  lane("#shop-history", by("denied", "failed", "rolled_back", "undone", "dropped"), (i) => shopCard(i,
    i.status === "failed" || i.status === "rolled_back" ? [button("Try again", () => shopPost(i.id, "build", { now: false }))] : [],
    { showStatus: true }), "Nothing here.");
  if (latest) { renderHeader(latest); renderPanels(); if (waiting.open) renderNeeds(); }
}

$("#idea-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("#idea-input").value.trim();
  if (!text) return;
  $("#idea-input").value = "";
  await post("/api/workshop/ideas", { text });
  loadWorkshop();
});

// -- the refresh ---------------------------------------------------------------------------------

async function refresh() {
  try {
    const response = await api("/api/overview");
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    latest = data;
    clockState = { tz: data.now.tz, minutes: data.now.minutes, fetchedAt: Date.now() };
    tick();
    renderToday(data);
    renderMemory(data);
    renderEngine(data);
    mic.hidden = !(data.talk?.hears && canRecord());
    callButton.hidden = mic.hidden;
    $("#orb-talk").disabled = mic.hidden;
  } catch (error) {
    if (error.message === "signed out") return;
    doing.server = { ...doing.server, state: "needs", label: "Can't reach her", detail: "Trying again in a minute" };
    paintOrb();
  }
}

// The clock every second; the day's shape (the now line, what's past, countdowns) every minute.
let lastMinute = -1;
function everySecond() {
  tick();
  const minute = nowMinutes();
  if (minute === lastMinute || !latest) return;
  lastMinute = minute;
  renderHeader(latest);
  renderTimeline();
  renderPanels();
}

document.querySelectorAll(".js-orb").forEach(orb);
paintOrb();
initChat();
$("#add-reminder").addEventListener("click", () => openForm($(".agenda"), FORMS.remind));
const startAt = recall("sloane.view", "today");
show(VIEWS.includes(startAt) || (startAt === "talk" && !wide.matches) ? startAt : "today");
loadHistory();
refresh();
pollActivity();
setInterval(refresh, 60000);
setInterval(everySecond, 1000);
