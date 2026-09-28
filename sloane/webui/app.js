// Sloane's control room. No framework and nothing loaded from outside: the
// page is served with CSP 'self'. Everything shown that came from outside
// (assignment titles, calendar text, her replies) goes in as text, or through
// `markdown()`, which escapes first and only then adds a few safe tags.
"use strict";

const $ = (sel) => document.querySelector(sel);
const HEAD = { "X-Sloane": "1", "Content-Type": "application/json" };
const RAIL_START = 6 * 60;   // 6 AM
const RAIL_END = 24 * 60;    // midnight: an 11:59 PM deadline is on it
let state = { tz: undefined, minutes: 0, fetchedAt: Date.now() };

// -- small helpers ------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
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
    else node.setAttribute(key, value);
  }
  for (const child of children) if (child != null) node.append(child);
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
    // Runs of list lines become a list; the lines around them stay text
    // ("Tonight, in order:" then the items).
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

// -- views ---------------------------------------------------------------------------

const wide = window.matchMedia("(min-width: 900px)");

function show(view) {
  if (wide.matches && view === "talk") view = "today";
  document.querySelectorAll(".tab").forEach((tab) => {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  });
  for (const id of ["today", "memory", "engine"]) $("#" + id).hidden = id !== view;
  $("#talk").hidden = !wide.matches && view !== "talk";
  try { localStorage.setItem("sloane.view", view); } catch { /* private mode */ }
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => show(tab.dataset.view)));
wide.addEventListener("change", () => show(document.querySelector(".tab[aria-current]")?.dataset.view || "today"));

// -- the conversation ------------------------------------------------------------------

const thread = $("#thread");

function scrollDown() { thread.scrollTop = thread.scrollHeight; }

function hisMessage(text, when = "", forwarded = false) {
  const item = el("li", { class: "msg him" },
    when ? el("span", { class: "when", text: when }) : null,
    el("span", { class: "body", text }),
    forwarded ? el("span", { class: "tag", text: "forwarded" }) : null);
  thread.append(item);
  scrollDown();
  return item;
}

function herMessage({ speech = "", detail = "", when = "", outside = false, cls = "" } = {}) {
  const item = el("li", { class: "msg her " + cls });
  if (when) item.append(el("span", { class: "when", text: when }));
  const said = el("p", { class: "speech", text: speech });
  if (outside) said.append(el("span", { class: "tag", text: "from outside text" }));
  item.append(said);
  if (detail) {
    const more = el("div", { class: "detail" });
    more.innerHTML = markdown(detail);
    item.append(more);
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
  const response = await api("/api/history");
  if (!response.ok) return;
  const rows = await response.json();
  thread.replaceChildren();
  for (const row of rows) {
    if (row.from === "him") hisMessage(row.text, row.when, row.forwarded);
    else herMessage({ ...splitLogged(row.text), when: row.when, outside: row.outside });
  }
  if (!rows.length) {
    herMessage({ speech: "Ask me anything, or tell me what needs doing.", cls: "hello" });
  }
}

const input = $("#composer-input");
const sendButton = $("#send");

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

async function say(text) {
  text = text.trim();
  if (!text || sendButton.disabled) return;
  sendButton.disabled = true;
  input.value = "";
  grow();
  hisMessage(text);
  let draft = null;
  const place = () => draft || (draft = herMessage({ speech: "…", cls: "thinking" }));
  try {
    const response = await api("/api/chat", { method: "POST", headers: HEAD, body: JSON.stringify({ text }) });
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      place().replaceWith(herMessage({ speech: data.error || "That didn't go through.", cls: "error" }));
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
        if (event.t === "typing") {
          place();
        } else if (event.t === "partial") {
          const node = place();
          node.className = "msg her draft";
          node.querySelector(".speech").textContent = event.text;
          scrollDown();
        } else if (event.t === "reply") {
          const finished = herMessage({ speech: event.speech, detail: event.detail, outside: event.outside });
          if (draft) { draft.replaceWith(finished); draft = null; }
        } else if (event.t === "error") {
          const failed = herMessage({ speech: event.text, cls: "error" });
          if (draft) { draft.replaceWith(failed); draft = null; }
        }
      }
    }
    if (draft) draft.remove();
  } catch (error) {
    if (error.message !== "signed out") herMessage({ speech: "Lost the connection. Try that again.", cls: "error" });
  } finally {
    sendButton.disabled = false;
    input.focus();
    refresh();
  }
}

$("#composer").addEventListener("submit", (event) => { event.preventDefault(); say(input.value); });
document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => say(chip.textContent)));

// -- today -----------------------------------------------------------------------------

function nowMinutes() {
  return state.minutes + Math.floor((Date.now() - state.fetchedAt) / 60000);
}

function pct(minutes) {
  const clamped = Math.max(RAIL_START, Math.min(RAIL_END, minutes));
  return ((clamped - RAIL_START) / (RAIL_END - RAIL_START)) * 100;
}

function hourLabel(h) { return (h % 12 || 12) + (h < 12 ? "a" : "p"); }

function drawRail(items) {
  const rail = $("#rail");
  rail.replaceChildren();
  const now = nowMinutes();
  rail.append(el("div", { class: "past", style: `width:${pct(now)}%` }));
  for (let h = 6; h < 24; h += 3) {
    rail.append(el("div", { class: "hour", style: `left:${pct(h * 60)}%` }, el("span", { text: hourLabel(h) })));
  }
  for (const item of items) {
    if (item.kind === "shift" || item.kind === "event") {
      const left = pct(item.start);
      const width = Math.max(pct(item.end) - left, 1.2);
      rail.append(el("div", { class: `block ${item.kind}`, style: `left:${left}%;width:${width}%`, title: item.label, text: item.label }));
    } else {
      rail.append(el("div", { class: `mark-${item.kind}`, style: `left:${pct(item.start)}%`, title: item.label }));
    }
  }
  if (now >= RAIL_START && now <= RAIL_END) {
    rail.append(el("div", { class: now > RAIL_END - 120 ? "now late" : "now", style: `left:${pct(now)}%` },
      el("span", { text: $("#clock").textContent })));
  }
}

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
  return el("li", {}, el("span", { class: "text", text }), meta ? el("span", { class: "meta", text: meta }) : null,
    actions.length ? el("span", { class: "acts" }, ...actions) : null);
}

function button(label, onclick, cls = "act") {
  return el("button", { type: "button", class: cls, text: label, onclick });
}

function renderToday(data) {
  drawRail(data.rail || []);
  fill("#schedule-today", data.schedule.today, (t) => line(t), "Nothing on the calendar today.");
  fill("#schedule-tomorrow", data.schedule.tomorrow, (t) => line(t), "A clear day.");

  const needs = $("#needs-list");
  needs.replaceChildren();
  for (const a of data.alerts || []) needs.append(el("li", {}, el("span", { class: "text bad", text: a.message })));
  for (const name of data.unreadable || []) needs.append(el("li", {}, el("span", { class: "text bad", text: `Couldn't read ${name}.` })));
  for (const p of data.proposals || []) {
    needs.append(line(p.preview, p.status === "editing" ? "editing" : "",
      button("Approve", () => post(`/api/proposals/${p.id}/approve`), "act primary"),
      button("Deny", () => post(`/api/proposals/${p.id}/deny`), "act danger")));
  }
  if (!needs.children.length) needs.append(el("li", {}, el("span", { class: "empty", text: "Nothing needs you." })));

  fill("#due", data.due, (a) => line(a.title + (a.course ? ` · ${a.course}` : ""), a.when), "Nothing due in the next three days.");
  fill("#overdue", data.overdue, (a) => el("li", {}, el("span", { class: "text bad", text: a.title + (a.course ? ` · ${a.course}` : "") }),
    el("span", { class: "meta", text: a.when })), "Nothing overdue.");
  fill("#reminders", data.reminders, (r) => line(r.text + (r.repeats ? `  ↻ ${r.repeats}` : ""), r.when,
    button(r.repeats ? "Stop" : "Cancel", () => post(`/api/reminders/${r.id}/cancel`), "act danger")),
  "No reminders. Tell her: remind me at 5 to call Keegan.");
  fill("#grades", data.grades, (g) => line(g.course, `${g.score}%${g.grade ? " " + g.grade : ""}`), "No grades from Canvas yet.");
  fill("#promises", data.promises, (c) => line(c.what + (c.to ? ` (to ${c.to})` : ""), c.when), "No open promises.");

  const panels = $("#panels");
  panels.replaceChildren();
  for (const panel of data.panels || []) {
    const list = el("ul", { class: "lines" });
    for (const text of panel.lines) list.append(line(text));
    panels.append(el("article", { class: "card" }, el("h2", { text: panel.title }), list));
  }
}

// -- memory --------------------------------------------------------------------------------

function renderMemory(data) {
  fill("#learned", data.learned, (f) => el("li", {},
    el("span", { class: "text" }, f.value, el("span", { class: "sub", text: f.key.replace(/^learned\./, "") + (f.source ? ` · ${f.source}` : "") })),
    el("span", { class: "acts" }, button("Forget", () => post("/api/memory/forget", { key: f.key }), "act danger"))),
  "Nothing yet. Tell her: remember that I'm vegetarian now.");
  fill("#loose", data.loose, (r) => line(r.summary, r.when, button("Done", () => post(`/api/followups/${r.id}/close`))),
    "No loose ends.");
  const diary = $("#diary");
  diary.replaceChildren();
  if (!data.diary || !data.diary.length) {
    diary.append(el("li", {}, el("span", { class: "text", text: "Her first entry comes tonight, after you've talked." })));
  }
  for (const d of data.diary || []) diary.append(el("li", {}, el("span", { class: "when", text: d.when }), el("span", { class: "text", text: d.text })));
}

// -- engine -----------------------------------------------------------------------------------

function renderEngine(data) {
  const sys = data.system;
  const facts = $("#system");
  facts.replaceChildren();
  const rows = [
    ["Database", sys.database ? "connected" : "unreachable", sys.database],
    ["Telegram", sys.telegram ? "polling" : "off", sys.telegram],
    ["Main model", sys.main],
    ["Bulk model", sys.bulk],
    ["Local model", sys.local || "none set up"],
    ["Voice", sys.voice],
    ["Quiet hours", sys.quiet],
    ["Web lookups", sys.web_lookup ? "on" : "off"],
    ["Skills", (sys.skills || []).join(", ") || "none"],
  ];
  for (const [label, value, good] of rows) {
    facts.append(el("dt", { text: label }), el("dd", { class: good === true ? "ok" : good === false ? "bad" : "", text: value }));
  }

  const body = $("#jobs tbody");
  body.replaceChildren();
  for (const job of data.jobs || []) {
    const status = job.status === "ok" ? "ok" : job.status === "failed" ? "failed" : "";
    body.append(el("tr", {},
      el("td", {}, job.name, el("span", { class: `pill ${status}`, text: job.status }),
        job.error ? el("span", { class: "sub", text: job.error }) : null),
      el("td", { class: "mono", text: job.last || "—" }),
      el("td", { class: "mono", text: job.next || "—" }),
      el("td", {}, button("Run now", () => post(`/api/jobs/${job.name}/run`)))));
  }

  fill("#trust", data.trust, (t) => line(`${t.action} → ${t.target}`,
    t.hard ? "hard line" : `${t.state}${t.state === "trusted" ? "" : ` · ${t.streak}/10`}`,
    ...(t.hard || t.state !== "trusted" ? [] : [button("Take back", () => post("/api/trust/revoke", { action: t.action, target: t.target }), "act danger")])),
  "She asks before everything.");
  fill("#usage", data.usage, (u) => line(`${u.lane} · ${u.provider}`, `${u.calls} calls${u.failures ? `, ${u.failures} failed` : ""}`),
    "No model calls in the last day.");
}

$("#sync-now").addEventListener("click", () => post("/api/jobs/entity_sync/run"));

// -- the clock, the pulse, the refresh ------------------------------------------------------

function tick() {
  const options = { hour: "numeric", minute: "2-digit" };
  try { $("#clock").textContent = new Intl.DateTimeFormat("en-US", { ...options, timeZone: state.tz }).format(new Date()); }
  catch { $("#clock").textContent = new Date().toLocaleTimeString([], options); }
}

async function refresh() {
  const pulse = $("#pulse");
  try {
    const response = await api("/api/overview");
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    state = { tz: data.now.tz, minutes: data.now.minutes, fetchedAt: Date.now() };
    $("#date").textContent = data.now.date;
    tick();
    renderToday(data);
    renderMemory(data);
    renderEngine(data);
    const trouble = (data.alerts || []).length + (data.unreadable || []).length;
    pulse.textContent = trouble ? `${trouble} to look at` : "All systems normal";
    pulse.className = "pulse " + (trouble ? "bad" : "ok");
  } catch (error) {
    if (error.message === "signed out") return;
    pulse.textContent = "Can't reach her";
    pulse.className = "pulse bad";
  }
}

$("#signout").addEventListener("click", async () => {
  await fetch("/app/logout", { method: "POST", headers: HEAD, credentials: "same-origin" });
  location.href = "/app/login";
});

let saved = "today";
try { saved = localStorage.getItem("sloane.view") || (wide.matches ? "today" : "talk"); } catch { /* private mode */ }
show(saved);
loadHistory();
refresh();
setInterval(refresh, 60000);
setInterval(() => { tick(); drawRailNow(); }, 1000);

let lastDrawn = -1;
function drawRailNow() {
  const minute = nowMinutes();
  if (minute !== lastDrawn && document.querySelector("#rail .now, #rail .past")) {
    lastDrawn = minute;
    const past = document.querySelector("#rail .past");
    if (past) past.style.width = pct(minute) + "%";
    const now = document.querySelector("#rail .now");
    if (now) { now.style.left = pct(minute) + "%"; now.firstChild.textContent = $("#clock").textContent; }
  }
}
