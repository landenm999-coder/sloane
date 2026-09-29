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

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
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
  toast(data.message || (response.ok ? "Done." : "That didn't work."));
  refresh();
  return data;
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
  if (doing.listening) return { state: "listening", label: "Listening", detail: "Recording your voice note" };
  if (doing.speaking) return { state: "speaking", label: "Speaking", detail: "Reading her reply aloud" };
  if (doing.asking) return { state: "thinking", label: "Thinking", detail: "Working on your message" };
  return doing.server;
}

function paintOrb() {
  const now = currentOrb();
  const arc = now.state === "building" && now.step ? (RING * now.step) / 5 : 0;
  document.querySelectorAll(".js-orb").forEach((o) => {
    o.dataset.state = now.state;
    o.querySelector(".prog")?.setAttribute("stroke-dasharray", `${arc.toFixed(1)} 220`);
  });
  $("#status-label").textContent = now.label;
  $("#status-detail").textContent = now.detail || "";
  $("#status-short").textContent = now.label.toLowerCase();
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
    // On a desk the chat is always open: Talk goes to it, and the view stays.
    input.focus();
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

document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => {
  if (item.dataset.view === "talk" && wide.matches) { input.focus(); return; }
  show(item.dataset.view);
}));
document.querySelectorAll(".subnav .seg-b").forEach((b) => b.addEventListener("click", () => show(b.dataset.go)));
wide.addEventListener("change", () => {
  const view = document.body.dataset.view;
  show(wide.matches && view === "talk" ? "today" : view);
  if (latest) renderPanels();
});

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

// Her reply, read out by the browser's own voice (never leaves the device).
function speakAloud(text) {
  if (readAloud.getAttribute("aria-pressed") !== "true" || !("speechSynthesis" in window) || !text) return;
  const words = new SpeechSynthesisUtterance(text);
  words.lang = latest?.talk?.lang || "en-US";
  const voice = speechSynthesis.getVoices().find((v) => v.lang.replace("_", "-") === words.lang);
  if (voice) words.voice = voice;
  words.addEventListener("start", () => { doing.speaking = true; paintOrb(); });
  for (const over of ["end", "error"]) words.addEventListener(over, () => { doing.speaking = false; paintOrb(); });
  speechSynthesis.speak(words);
}

function hush() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  doing.speaking = false;
  paintOrb();
}

if ("speechSynthesis" in window) {
  readAloud.setAttribute("aria-pressed", recall("sloane.readAloud", "false"));
  readAloud.addEventListener("click", () => {
    const on = readAloud.getAttribute("aria-pressed") !== "true";
    readAloud.setAttribute("aria-pressed", String(on));
    remember("sloane.readAloud", String(on));
    if (!on) hush();
    toast(on ? "She'll read her replies aloud." : "Reading aloud is off.");
  });
} else {
  readAloud.hidden = true;
}

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
        } else if (event.t === "reply") {
          const finished = herMessage({ speech: event.speech, detail: event.detail, outside: event.outside });
          if (draft) { draft.replaceWith(finished); draft = null; }
          speakAloud(event.speech);
        } else if (event.t === "error") {
          const failed = herMessage({ speech: event.text, cls: "error" });
          if (draft) { draft.replaceWith(failed); draft = null; }
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
    const bins = new Uint8Array(analyser.frequencyBinCount);
    const live = document.querySelectorAll(".js-orb");
    live.forEach((o) => o.classList.add("live"));
    const hear = { context, frame: 0 };
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
  hear.context.close().catch(() => {});
}

async function startRecording() {
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

// "/" goes to the message box from anywhere that isn't already a field.
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || document.activeElement?.isContentEditable) return;
  event.preventDefault();
  if (!wide.matches) show("talk");
  input.focus();
});

// What to ask, from what's actually on today.
function renderStarters(data) {
  const now = nowMinutes();
  const items = data.agenda?.today?.items || [];
  const asks = [];
  if (items.some((i) => i.kind === "due" && i.start > now)) asks.push("What should I do first tonight?");
  else asks.push("What's due tomorrow?");
  if (items.some((i) => i.kind === "shift" && i.end > now)) asks.push("Plan my evening around work");
  else asks.push("Help me plan tonight");
  if ((data.overdue || []).length) asks.push("How do I catch up on what's overdue?");
  asks.push("/reminders", "What do you remember about me?");
  const box = $("#starters");
  const key = asks.join("|");
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren(...asks.map((text) => el("button", { type: "button", class: "chip", text, onclick: () => say(text) })));
}

// -- the header: the clock, the day, what's next, what's waiting -------------------------------

function tick() {
  let text;
  try {
    const parts = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: clockState.tz }).formatToParts(new Date());
    text = `${parts.find((p) => p.type === "hour").value}:${parts.find((p) => p.type === "minute").value}`;
  } catch {
    const d = new Date();
    text = `${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, "0")}`;
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

// The header's countdown: work first (on it, or the next shift today), else what's next, else what's on.
function nextUp(data) {
  const now = nowMinutes();
  const blocks = (data.agenda?.today?.items || []).filter((i) => !i.all_day && (i.kind === "shift" || i.kind === "event" || i.kind === "focus"));
  const on = (i) => i.start <= now && now < i.end;
  const shiftNow = blocks.find((i) => i.kind === "shift" && on(i));
  const shiftNext = blocks.find((i) => i.kind === "shift" && i.start > now);
  const next = blocks.find((i) => i.start > now);
  const current = blocks.find(on);
  const box = $("#next-up");
  if (shiftNow) box.textContent = `At work until ${short(shiftNow.end)}`;
  else if (shiftNext) box.textContent = `Work in ${hm(shiftNext.start - now)}`;
  else if (next) box.textContent = `${jobLabel(next)} in ${hm(next.start - now)}`;
  else if (current) box.textContent = `${jobLabel(current)} until ${short(current.end)}`;
  box.hidden = !(shiftNow || shiftNext || next || current);
}

function renderHeader(data) {
  $("#date").textContent = data.now.date;
  const weather = panelOf("weather");
  $("#weather-short").textContent = weather && weather.temp != null ? `${Math.round(weather.temp)}° ${weather.sky || ""}`.trim() : "";
  nextUp(data);
  const waiting = needsCount();
  const open = $("#waiting-open");
  open.hidden = !waiting;
  open.textContent = `${waiting} waiting on you`;
  open.classList.toggle("hot", !!waiting);
  const due = $("#due-open");
  due.hidden = !data.due_week;
  due.textContent = `${data.due_week} due this week`;
  $("#links").hidden = open.hidden && due.hidden;
}

// -- what's waiting on him: approvals, what's broken, overdue work, builds to accept ------------

const JOBS = {
  entity_sync: "Canvas and calendar sync", morning_brief: "Morning brief", pre_shift: "Before your shift",
  post_shift: "After your shift", wrap: "Night wrap", reflection: "Reflection", reminders: "Reminders",
  heartbeat: "Heartbeat", inbox: "Inbox triage", learn: "Nightly learning", backup: "Backup", watchdog: "Watchdog",
  weekly_review: "Weekly review", think: "Thinking", workshop: "Workshop night shift",
};
function jobName(name) { return JOBS[name] || name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()); }

function readyBuilds() { return (shop?.items || []).filter((i) => i.status === "ready"); }

function needsCount() {
  const data = latest;
  if (!data) return 0;
  return (data.proposals || []).length + (data.alerts || []).length + (data.unreadable || []).length
    + (data.jobs || []).filter((j) => j.status === "failed").length + (data.overdue || []).length
    + (readyBuilds().length ? 1 : 0);
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
  for (const a of data.overdue || []) list.append(row("Overdue", a.title, [a.course, a.when].filter(Boolean).join(" · "), [], "bad"));
  if (!list.children.length) list.append(el("li", {}, el("span", { class: "empty", text: "Nothing needs you." })));

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
  (at === "due" ? $("#due-title") : $("#waiting-title")).scrollIntoView({ block: "nearest" });
  $("#waiting-close").focus();
}
function closeWaiting() { if (waiting.open) waiting.close(); }
$("#waiting-open").addEventListener("click", () => openWaiting("needs"));
$("#due-open").addEventListener("click", () => openWaiting("due"));
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
    ? el("button", { type: "button", class: "x", text: "Cancel", "aria-label": `Cancel reminder: ${item.title}`,
      onclick: () => post(`/api/reminders/${item.id}/cancel`) }) : null;
  return el("li", { class: `row ${item.kind}${past ? " past" : ""}${item.all_day ? " all-day" : ""}` },
    el("span", { class: "h" }, el("span", { "aria-hidden": "true", text: item.all_day ? "all day" : short(item.start) }),
      el("span", { class: "sr", text: item.all_day ? "All day" : item.time })),
    el("div", { class: "ev" }, el("div", {}, item.title, sub ? el("small", { text: sub }) : null), cancel));
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
    el("span", { text: "now" }), el("i"));
  for (const item of items) {
    if (!placed && !item.all_day && item.start > now) { list.append(nowbar()); placed = true; }
    list.append(timelineRow(item, now, live));
  }
  if (!placed) list.append(nowbar());
  if (!items.length) list.append(el("li", { class: "empty", text: live ? "Nothing on the calendar today." : "A clear day." }));
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
      button(r.repeats ? "Stop" : "Cancel", () => post(`/api/reminders/${r.id}/cancel`), "btn danger",
        { "aria-label": `${r.repeats ? "Stop" : "Cancel"} reminder: ${r.text}` })));
  }
  for (const c of data.promises || []) {
    list.append(el("li", {}, el("span", { class: "text" }, c.what + (c.to ? ` (to ${c.to})` : ""),
      el("span", { class: "sub", text: ["promise", c.when].filter(Boolean).join(" · ") }))));
  }
  $("#later-wrap").hidden = !list.children.length;
}

function setDay(tomorrow) {
  tomorrowShown = tomorrow;
  $("#day-today").setAttribute("aria-pressed", String(!tomorrow));
  $("#day-tomorrow").setAttribute("aria-pressed", String(tomorrow));
  renderTimeline();
}
$("#day-today").addEventListener("click", () => setDay(false));
$("#day-tomorrow").addEventListener("click", () => setDay(true));

// -- today: the panels ----------------------------------------------------------------------

const PANEL_NAMES = {
  weather: "Weather", countdowns: "Countdowns", workshop: "Workshop", habits: "Habits", focus: "Focus",
  grades: "Grades", week: "This week", learned: "Learned today", colleges: "College", engine: "Engine",
  work: "Up next at work", plan: "Tonight's plan", memory: "Loose ends", lists: "Lists", cards: "Flashcards",
  clients: "Clients", money: "Money", birthdays: "Birthdays", deca: "DECA practice",
};
const DESK_PANELS = 9;

function pn(title, right, ...body) {
  return el("section", { class: "pn" }, el("h3", {}, title, right ? el("span", { text: right }) : null), ...body);
}

function weatherPanel() {
  const p = panelOf("weather");
  if (!p || p.temp == null) return null;
  const hours = (p.hours || []).filter((h) => h.temp != null).slice(0, 8);
  const box = pn("Weather", p.sky || "", el("div", { class: "big-n", text: `${Math.round(p.temp)}°` }));
  if (hours.length) {
    const temps = hours.map((h) => h.temp);
    const lo = Math.min(...temps), hi = Math.max(...temps);
    let pm;
    const bars = el("div", { class: "wx", "aria-hidden": "true" });
    const labels = el("div", { class: "wx-l", "aria-hidden": "true" });
    for (const h of hours) {
      bars.append(el("i", { class: h.chance >= 50 ? "wet" : "", style: `height:${hi === lo ? 70 : 35 + ((h.temp - lo) / (hi - lo)) * 65}%` }));
      const [label, isPm] = hourLabel(h.at, pm);
      pm = isPm;
      labels.append(el("span", { text: label }));
    }
    const said = hours.map((h) => `${short(minuteOf(h.at))} ${Math.round(h.temp)}°`).join(", ");
    box.append(bars, labels, el("span", { class: "sr", text: `Next hours: ${said}` }));
  } else {
    box.append(el("div", { class: "sub", text: (p.lines || [])[1] || "" }));
  }
  return box;
}

function daysBetween(fromIso, toIso) {
  return Math.round((Date.parse(toIso) - Date.parse(fromIso)) / 86400000);
}

function countdownsPanel() {
  const p = panelOf("countdowns");
  const items = (p?.items || []).slice(0, 4);
  if (!items.length) return null;
  const list = el("div", { class: "cd" });
  for (const c of items) {
    // How much of the wait is gone: from the day it was set, or the last 90 days.
    const total = c.created_on ? Math.max(1, daysBetween(c.created_on, c.date)) : 90;
    const gone = Math.max(0, Math.min(1, 1 - c.days / total));
    list.append(el("div", {}, el("span", { class: "mono", text: c.days === 0 ? "today" : `${c.days}d` }),
      el("span", { class: "name", text: c.name }),
      el("span", { class: "bar", "aria-hidden": "true" }, el("i", { style: `width:${(gone * 100).toFixed(0)}%` }))));
  }
  return pn("Countdowns", "", list);
}

const STEP_NAMES = ["plan", "code", "test", "CI", "ready"];

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
    word = ["planning", "building", "testing", "waiting on CI", "ready"][step - 1];
  } else if (item.status === "deploying" || item.status === "accepted") { step = 5; active = true; word = "going live"; }
  else if (item.status === "ready") { step = 5; active = true; word = "ready for you"; }
  else if (item.status === "queued") { step = 2; word = "queued"; }
  else if (item.status === "planned") { step = 2; word = "planned"; }
  else { step = 1; active = s.state === "building" && s.step === 1; word = active ? "planning" : "an idea"; }
  return { item, step, active, word };
}

function workshopPanel() {
  const now = workshopNow();
  if (!now) return null;
  // Done steps amber, the one under way blinking.
  const segs = el("div", { class: "steps", "aria-hidden": "true" });
  for (let i = 1; i <= 5; i++) segs.append(el("i", { class: i < now.step ? "d" : i === now.step && now.active ? "a" : "" }));
  const labels = el("div", { class: "sub step-l" });
  STEP_NAMES.forEach((name, i) => {
    if (i) labels.append(" · ");
    labels.append(i + 1 === now.step && now.active ? el("b", { text: name }) : name);
  });
  const box = el("button", { type: "button", class: "pn", onclick: () => show("workshop"),
    "aria-label": `Workshop: ${now.item.title}, ${now.word}. Open the workshop.` },
  el("h3", {}, "Workshop", el("span", { class: now.active ? "hot" : "", text: now.word })),
  el("div", { class: "title", text: now.item.title }), segs, labels);
  return box;
}

function habitsPanel() {
  const p = panelOf("habits");
  const habits = p?.habits || [];
  if (!habits.length) return null;
  const rows = el("div", { class: "hb" });
  for (const h of habits) {
    const dots = el("div", { class: "dots", "aria-hidden": "true" });
    for (const done of h.days || []) dots.append(el("i", { class: done ? "y" : "" }));
    const kept = (h.days || []).filter(Boolean).length;
    rows.append(el("div", { class: "r" }, el("span", { text: h.name }), dots,
      el("span", { class: "mono", text: String(h.streak), "aria-label": `${h.streak} day streak, ${kept} of the last 14 days` })));
  }
  const box = pn("Habits", "last 14 days", rows);
  box.classList.add("span2");
  return box;
}

function focusPanel() {
  const p = panelOf("focus");
  if (!p || p.today_minutes == null) return null;
  const C = 2 * Math.PI * 22;
  let fraction, big, sub;
  const running = p.running;
  if (running) {
    // A block running: the ring counts down.
    const total = Math.max(1, Date.parse(running.ends_at) - Date.parse(running.started_at));
    const left = Math.max(0, Date.parse(running.ends_at) - Date.now());
    fraction = left / total;
    big = `${Math.ceil(left / 60000)}m left`;
    sub = [running.what, `${hm(p.today_minutes)} today`];
  } else {
    fraction = Math.min(1, p.today_minutes / Math.max(1, p.goal_minutes));
    big = hm(p.today_minutes);
    sub = ["focused today", `goal ${hm(p.goal_minutes)}`];
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "54"); svg.setAttribute("height", "54"); svg.setAttribute("viewBox", "0 0 54 54");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = `<circle class="track" cx="27" cy="27" r="22" fill="none" stroke-width="5"/>
    <circle class="done" cx="27" cy="27" r="22" fill="none" stroke-width="5" stroke-linecap="round"
      stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C * (1 - fraction)).toFixed(1)}" transform="rotate(-90 27 27)"/>`;
  return pn("Focus", running ? "running" : "", el("div", { class: "ring" }, svg,
    el("div", {}, el("div", { class: "mono", style: "font-size:17px", text: big }),
      el("div", { class: "sub" }, sub[0], el("br"), sub[1]))));
}

function gradesPanel() {
  const grades = latest?.grades || [];
  if (!grades.length) return null;
  const rows = el("div", { class: "gr" });
  for (const g of grades) {
    const score = Number(g.score) || 0;
    rows.append(el("div", { class: `r${score < 80 ? " low" : ""}` }, el("span", { text: g.course }),
      el("span", { class: "mono", text: `${score.toFixed(1).replace(/\.0$/, "")}%` }),
      el("span", { class: "bar", "aria-hidden": "true" }, el("i", { style: `width:${Math.max(0, Math.min(100, score))}%` }))));
  }
  return pn("Grades", "", rows);
}

function weekPanel() {
  const days = latest?.week || [];
  if (!days.length || !days.some((d) => d.count)) return null;
  const strip = el("div", { class: "wk" });
  for (const d of days) {
    strip.append(el("span", { "aria-label": `${d.day}: ${d.count} thing${d.count === 1 ? "" : "s"}${d.today ? ", today" : ""}` },
      el("b", { class: [d.level ? `l${d.level}` : "", d.today ? "today" : ""].join(" ").trim() }), d.day[0]));
  }
  const most = Math.max(...days.map((d) => d.count));
  const heavy = days.filter((d) => d.count === most);
  const said = heavy.length === 1 && most >= 3 ? `${heavy[0].day} is the heavy one.` : "Spread out evenly.";
  return pn("This week", "", strip, el("div", { class: "sub", text: said }));
}

function learnedPanel() {
  const facts = (latest?.learned || []).filter((f) => f.today);
  if (!facts.length) return null;
  const list = el("div", { class: "lr" });
  for (const f of facts.slice(0, 5)) {
    if (forgetting === f.key) {
      list.append(el("div", { class: "confirm" }, el("span", { class: "text", text: f.value }),
        button("Forget", () => { forgetting = ""; post("/api/memory/forget", { key: f.key }); }, "btn danger", { "aria-label": `Forget: ${f.value}` }),
        button("Keep", () => { forgetting = ""; renderPanels(); })));
    } else {
      list.append(el("button", { type: "button", text: f.value, "aria-label": `${f.value}. Forget it?`,
        onclick: () => { forgetting = f.key; renderPanels(); } }));
    }
  }
  return pn("Learned today", "", list);
}

function collegesPanel() {
  const schools = panelOf("colleges")?.schools || [];
  if (!schools.length) return null;
  const rows = el("div", { class: "cl" });
  for (const s of schools.slice(0, 5)) {
    // Done first, so the pips fill like a bar.
    const pips = el("span", { class: "pips", "aria-hidden": "true" });
    for (const t of [...(s.checklist || [])].sort((a, b) => b.done - a.done)) pips.append(el("i", { class: t.done ? "y" : "" }));
    const done = (s.checklist || []).filter((t) => t.done).length;
    const when = s.days == null ? "" : s.days < 0 ? "late" : s.days === 0 ? "today" : `${s.days}d`;
    rows.append(el("div", { class: "r", "aria-label": `${s.name}: ${done} of ${(s.checklist || []).length} done${when ? `, ${when === "late" ? "past its deadline" : when === "today" ? "due today" : `${s.days} days to go`}` : ""}` },
      el("span", { text: s.plan ? `${s.name} ${s.plan}` : s.name }),
      el("span", { class: "right" }, pips, when ? el("span", { class: `mono${when === "late" ? " late" : ""}`, text: when }) : null)));
  }
  return pn("College", "", rows);
}

function enginePanel() {
  const e = latest?.engine;
  if (!e) return null;
  const up = e.up_since ? hm((Date.now() - Date.parse(e.up_since)) / 60000) : "—";
  const share = e.job_budget ? Math.min(1, e.job_calls / e.job_budget) : 0;
  return pn("Engine", "", el("div", { class: "en" },
    el("span", {}, el("span", { class: "mono", text: String(e.calls_today) }), "calls today"),
    el("span", {}, el("span", { class: "mono", text: up }), "up"),
    el("span", { class: "meter", role: "img", "aria-label": `${e.job_calls} of ${e.job_budget} scheduled calls` },
      el("i", { class: share >= 0.9 ? "hot" : "", style: `width:${Math.max(2, share * 100).toFixed(0)}%` }))),
  el("div", { class: "sub", text: `scheduled ${e.job_calls}/${e.job_budget} · bulk ${e.bulk_calls}/${e.bulk_budget}` }));
}

function workPanel() {
  const w = latest?.work;
  if (!w || (!w.now && !w.next)) return null;
  let big, sub;
  if (w.now) { big = `until ${short(minuteOf(w.now.ends_at))}`; sub = "on shift now"; }
  else {
    const minutes = (Date.parse(w.next.starts_at) - Date.now()) / 60000;
    big = minutes < 24 * 60 ? hm(minutes) : w.next.when;
    sub = minutes < 24 * 60 ? `shift at ${short(minuteOf(w.next.starts_at))}` : "next shift";
  }
  return pn("Up next at work", "", el("div", { class: "big-n", style: "font-size:22px;color:var(--work)", text: big }),
    el("div", { class: "sub", text: sub }),
    el("div", { class: "sub", text: `${w.week_hours}h this week (${w.done_hours}h done) · ${w.last_week_hours}h last week` }));
}

function linesPanel(p) {
  const list = el("ul", {});
  for (const text of (p.lines || []).slice(0, 6)) list.append(el("li", { text }));
  return pn(p.title || PANEL_NAMES[p.skill] || p.skill, "", list);
}

const BUILT = {
  weather: weatherPanel, countdowns: countdownsPanel, workshop: workshopPanel, habits: habitsPanel,
  focus: focusPanel, grades: gradesPanel, week: weekPanel, learned: learnedPanel, colleges: collegesPanel,
  engine: enginePanel, work: workPanel,
};

// Every panel there could be, in order: the ones the page draws, then every other skill's.
function panelIds() {
  const ids = Object.keys(latest?.prefs?.defaults || BUILT);
  for (const p of latest?.panels || []) if (!ids.includes(p.skill)) ids.push(p.skill);
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
    try { return BUILT[id](); } catch { /* a panel that can't draw falls back to its lines */ }
  }
  const p = panelOf(id);
  return p && (p.lines || []).length ? linesPanel(p) : null;
}

function renderPanels() {
  const box = $("#panels");
  const out = [];
  if (wide.matches) {
    for (const id of panelIds()) {
      if (out.length >= DESK_PANELS) break;
      if (!panelOn(id)) continue;
      const node = buildPanel(id);
      if (node) out.push(node);
    }
  } else {
    // The phone's three, as he chose them; with no data for one, the next panel that's on.
    const chosen = latest?.prefs?.phone || ["countdowns", "habits", "workshop"];
    for (const id of [...chosen, ...panelIds().filter((i) => !chosen.includes(i) && panelOn(i))]) {
      if (out.length >= (latest?.prefs?.phone_count || 3)) break;
      const node = buildPanel(id);
      if (node) out.push(node);
    }
  }
  box.replaceChildren(...out);
  box.closest(".panel-col").hidden = !out.length;
}

function renderToday(data) {
  renderHeader(data);
  renderTimeline();
  renderPanels();
  renderStarters(data);
  if (waiting.open) renderNeeds();
}

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
      el("td", {}, el("span", { class: "job-name", text: jobName(job.name) }), el("span", { class: `pill ${status}`, text: job.status }),
        el("span", { class: "job-id", text: job.name }),
        job.error ? el("span", { class: "job-error", text: job.error }) : null),
      el("td", { class: "mono", text: job.last || "—" }),
      el("td", { class: "mono", text: job.next || "—" }),
      el("td", {}, button("Run now", () => post(`/api/jobs/${job.name}/run`), "btn", { "aria-label": `Run ${jobName(job.name)} now` }))));
  }

  fill("#trust", data.trust, (t) => line(`${t.action} → ${t.target}`,
    t.hard ? "hard line" : `${t.state}${t.state === "trusted" ? "" : ` · ${t.streak}/10`}`,
    ...(t.hard || t.state !== "trusted" ? [] : [button("Take back", () => post("/api/trust/revoke", { action: t.action, target: t.target }), "btn danger")])),
  "She asks before everything.");
  const most = Math.max(1, ...(data.usage || []).map((u) => u.calls));
  fill("#usage", data.usage, (u) => el("li", {},
    el("span", { class: "text", text: `${u.lane} · ${provider(u.provider)}` }),
    el("span", { class: "meta2", text: `${u.calls} call${u.calls === 1 ? "" : "s"}${u.failures ? `, ${u.failures} failed` : ""}` }),
    el("span", { class: "bar", "aria-hidden": "true" }, el("i", { class: u.failures ? "failed" : "", style: `width:${(u.calls / most) * 100}%` }))),
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
const startAt = recall("sloane.view", "today");
show(VIEWS.includes(startAt) || (startAt === "talk" && !wide.matches) ? startAt : "today");
loadHistory();
refresh();
pollActivity();
setInterval(refresh, 60000);
setInterval(everySecond, 1000);
