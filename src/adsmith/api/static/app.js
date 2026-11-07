// Adsmith Studio: vanilla JS + SVG over the REST + SSE API (no build step).
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isAr = (s) => /[؀-ۿ]/.test(s || "");
const money = (v) => `$${Number(v || 0).toFixed(2)}`;
const SVGNS = "http://www.w3.org/2000/svg";

const state = { run: null, view: null, es: null, filter: "all", events: [], refreshTimer: null, poll: null, drawQueued: false };

const ICONS = {
  stage_started: "▸", stage_completed: "✓", decision: "·", attempt_finished: "■", retry_scheduled: "↻",
  recovery: "↺", cache_hit: "=", scene_status: "◆", edit_parsed: "✎", blast_radius: "◎", llm_call: "·",
  llm_repair: "⟲", awaiting_input: "?", export: "↓", error: "✕", run_completed: "■",
};
const STATUS_LABEL = { pending: "pending", generating: "generating", qa_review: "QA", approved: "approved", failed: "failed", stale: "stale" };
const KIND_LABEL = { llm: "LLM", mixed: "LLM · CODE", code: "CODE" };
const EDIT_SUGGESTIONS = ["خل المشهد الثاني أبطأ", "make scene 3 at night", "غيّر لبسه لثوب أسود"];

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function el(tag, attrs = {}, parent = null) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

// ------------------------------------------------------------------ boot
async function boot() {
  const cfg = await api("/api/config");
  $("#mode-badge").textContent = cfg.zero_key ? "zero-key · scripted LLM · mock media" : `${cfg.llm} · ${cfg.media}`;
  $("#examples").innerHTML = cfg.examples.map((e) => `<button class="example" dir="auto">${esc(e)}</button>`).join("");
  $("#examples").onclick = (ev) => { if (ev.target.classList.contains("example")) $("#request").value = ev.target.textContent; };
  $("#request").value = cfg.examples[0];
  $("#edit-suggest").innerHTML = EDIT_SUGGESTIONS.map((e) => `<button class="example" dir="auto">${esc(e)}</button>`).join("");
  $("#edit-suggest").onclick = (ev) => { if (ev.target.classList.contains("example")) $("#edit").value = ev.target.textContent; };
  $("#go").onclick = startRun;
  $("#edit-go").onclick = () => applyEdit($("#edit").value);
  $("#edit").onkeydown = (ev) => { if (ev.key === "Enter") applyEdit($("#edit").value); };
  $("#approve").onclick = () => act("approve");
  $("#filters").onclick = (ev) => {
    const f = ev.target.dataset.f; if (!f) return;
    state.filter = f; document.querySelectorAll(".filter").forEach((b) => b.classList.toggle("active", b.dataset.f === f));
    renderTrace();
  };
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => showView(t.dataset.view)));
  $("#recent").onchange = (ev) => ev.target.value && openRun(ev.target.value);
  setupTips();
  await loadRecent();
  loadTeaser();
  const m = location.hash.match(/run=([\w]+)/);
  if (location.hash.includes("gallery")) showView("gallery");
  if (m) openRun(m[1]);
}

async function loadRecent() {
  const runs = await api("/api/runs");
  $("#recent").innerHTML = `<option value="">Recent runs</option>` +
    runs.map((r) => `<option value="${r.id}">${esc(r.stage)} · ${esc(r.request.slice(0, 46))}</option>`).join("");
}

function showView(v) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === v));
  $("#studio").classList.toggle("hidden", v !== "studio");
  $("#gallery").classList.toggle("hidden", v !== "gallery");
  document.querySelector(".layout").classList.toggle("no-panel", v === "gallery");
  if (v === "gallery") loadGallery();
}

// ------------------------------------------------------------------ actions
async function startRun() {
  const request = $("#request").value.trim();
  if (!request) return;
  $("#go").disabled = true;
  try {
    const { id } = await api("/api/runs", { method: "POST", body: JSON.stringify({ request, auto: $("#auto").checked, chaos: $("#chaos").checked }) });
    openRun(id);
    loadRecent();
  } catch (e) { alertError(e); }
  finally { $("#go").disabled = false; }
}

async function act(kind, body) {
  try { await api(`/api/runs/${state.run}/${kind}`, { method: "POST", body: JSON.stringify(body || {}) }); scheduleRefresh(); }
  catch (e) { alertError(e); }
}

async function applyEdit(text) {
  text = (text || "").trim();
  if (!text || !state.run) return;
  $("#edit-go").disabled = true;
  try {
    const out = await api(`/api/runs/${state.run}/edit`, { method: "POST", body: JSON.stringify({ text }) });
    if (out.status !== "applied") showError(`${out.status}: ${out.message}`);
    else { showError(""); $("#edit").value = ""; }
    scheduleRefresh();
  } catch (e) { alertError(e); }
  finally { $("#edit-go").disabled = false; }
}

function alertError(e) { showError(e.message || String(e)); }
function showError(msg) { const n = $("#error"); n.textContent = msg; n.classList.toggle("hidden", !msg); }

// ------------------------------------------------------------------ run + SSE
function openRun(id) {
  state.run = id; state.events = []; state.view = null; location.hash = `run=${id}`;
  showView("studio");
  $("#run").classList.remove("hidden");
  $("#teaser").classList.add("hidden");
  $("#trace").innerHTML = "";
  $("#decisions").innerHTML = "";
  if (state.es) state.es.close();
  state.es = new EventSource(`/api/runs/${id}/events`);
  state.es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (state.events.length && e.seq <= state.events[state.events.length - 1].seq) return;
    state.events.push(e); appendTrace(e); appendDecision(e); queueDraw(); scheduleRefresh();
  };
  state.es.onopen = () => $("#live-dot").classList.add("on");
  state.es.onerror = () => $("#live-dot").classList.remove("on");
  refresh();
  clearInterval(state.poll);
  state.poll = setInterval(() => state.view && state.view.busy && refresh(), 2000);
}

function scheduleRefresh() { clearTimeout(state.refreshTimer); state.refreshTimer = setTimeout(refresh, 220); }
function queueDraw() {
  if (state.drawQueued) return;
  state.drawQueued = true;
  requestAnimationFrame(() => { state.drawQueued = false; drawLanes(); drawBurn(); });
}

async function refresh() {
  if (!state.run) return;
  try { state.view = await api(`/api/runs/${state.run}`); render(state.view); } catch (e) { alertError(e); }
}

// ------------------------------------------------------------------ render
function render(v) {
  $("#run-id").textContent = v.id;
  $("#run-request").textContent = v.request;
  $("#run-request").dir = isAr(v.request) ? "rtl" : "ltr";
  const meta = [];
  if (v.brief) meta.push(`${v.brief.duration_s}s`, v.brief.dialect.replace("_", " "));
  meta.push(v.interactive ? "interactive" : "auto", `${v.llm} · ${v.media}`);
  const pills = meta.map((m) => `<span class="pill">${esc(m)}</span>`);
  if (v.chaos) pills.push(`<span class="pill warn">chaos on</span>`);
  pills.push(`<span class="pill stage">${esc(v.stage.replaceAll("_", " "))}</span>`);
  $("#run-meta").innerHTML = pills.join("");
  drawGraph(v);
  drawLanes();
  renderConcepts(v);
  renderBoard(v);
  renderProductRef(v);
  renderFinal(v);
  renderBudget(v.budget);
  drawBurn();
  showError(v.error || "");
}

// ------------------------------------------------------------------ pipeline graph (run FSM)
function drawGraph(v) {
  const svg = $("#graph");
  svg.innerHTML = "";
  const defs = el("defs", {}, svg);
  const mk = el("marker", { id: "arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 6, markerHeight: 6, orient: "auto" }, defs);
  el("path", { d: "M0,0 L8,4 L0,8 z", class: "marker" }, mk);
  const n = v.pipeline.length, w = 82, h = 40, y = 14, gap = (1000 - 20 - n * w) / (n - 1);
  const x = (i) => 10 + i * (w + gap);
  const idx = (key) => v.pipeline.findIndex((p) => p.stage === key);
  const awaiting = { awaiting_concept_choice: idx("select"), awaiting_storyboard_approval: idx("keyframe"), awaiting_generation_approval: idx("estimate") }[v.stage];
  v.pipeline.forEach((p, i) => {
    if (i < n - 1) {
      el("line", { x1: x(i) + w, y1: y + h / 2, x2: x(i + 1) - 2, y2: y + h / 2, class: `edge ${p.state === "done" ? "done" : ""}`, "marker-end": "url(#arrow)" }, svg);
    }
  });
  // human gates (interactive runs only stop there)
  const gen = v.generation;
  const gates = [[idx("select"), "concept", "awaiting_concept_choice", v.interactive]];
  if (gen) gates.push([idx("estimate"), "spend", "awaiting_generation_approval", !gen.approved_by || gen.approved_by === "user"]);
  else gates.push([idx("keyframe"), "storyboard", "awaiting_storyboard_approval", v.interactive]);
  gates.forEach(([i, key, st, human]) => {
    if (i < 0) return;
    const cx = x(i) + w + gap / 2, cy = y + h / 2, on = v.stage === st;
    const tip = key === "spend" ? "Human gate · spend: real money waits for your approval of the estimate" : `Human gate · ${key}: ${human ? "the run waits here for you" : "skipped (auto run)"}`;
    const g = el("g", { "data-tip": tip }, svg);
    el("rect", { x: cx - 4.5, y: cy - 4.5, width: 9, height: 9, transform: `rotate(45 ${cx} ${cy})`, class: `gate ${on ? "on" : ""}` }, g);
    el("text", { x: cx, y: y + h + 16, "text-anchor": "middle", class: "gate-label" }, g).textContent = human ? "you" : "auto";
  });
  v.pipeline.forEach((p, i) => {
    const active = p.state === "active" || i === awaiting;
    const g = el("g", { class: `node ${active ? "active" : p.state}`, "data-tip": `${p.en} · ${p.who}` }, svg);
    el("rect", { x: x(i), y, width: w, height: h, rx: 3 }, g);
    el("text", { x: x(i) + w / 2, y: y + 17, "text-anchor": "middle", class: "label" }, g).textContent = p.en;
    el("text", { x: x(i) + w / 2, y: y + 31, "text-anchor": "middle", class: "kind" }, g).textContent = KIND_LABEL[p.kind] || "";
  });
  // the one loop in the table: qa → generate while any scene still has work
  if (!gen) {
    const gx = x(idx("generate")) + w / 2, qx = x(idx("qa")) + w / 2, by = y + h;
    el("path", { d: `M${qx},${by + 2} C${qx},${by + 34} ${gx},${by + 34} ${gx},${by + 4}`, class: "loop", "marker-end": "url(#arrow)" }, svg);
    el("text", { x: (gx + qx) / 2, y: by + 44, "text-anchor": "middle", class: "loop-label" }, svg).textContent = "until every scene settles";
  }
  const end = { done: "done", done_partial: "done · partial", errored: "errored" }[v.stage];
  if (end) el("text", { x: x(n - 1) + w / 2, y: y + h + 16, "text-anchor": "middle", class: "gate-label" }, svg).textContent = `→ ${end}`;
}

// ------------------------------------------------------------------ swimlanes (one per scene)
function stepOf(e) {
  const d = e.data || {}, s = e.summary;
  if (e.type === "attempt_started") {
    if (d.phase === "clip" || / clip attempt|best-of-/.test(s)) return { k: "clip", n: d.candidates || 1, tip: s };
    if (d.phase === "keyframe" || /keyframe attempt/.test(s)) return { k: "kf", tip: s, sub: d.identity };
    return null;
  }
  if (e.type === "cache_hit") {
    const k = /keyframe/.test(s) ? "kf" : /clip/.test(s) ? "clip" : null;
    return k ? { k, cached: true, tip: s } : null;
  }
  if (d.strategy === "vocab_fallback") return { k: "tag", label: `→ ${d.to_vocab}`, loop: "kf", tip: s };
  if (d.gate === "keyframe") {
    if (e.type === "recovery") return { k: "gate", ok: false, loop: "kf", score: d.score, tip: s };
    if (e.level === "info") return { k: "gate", ok: true, score: d.score, tip: s };
    return { k: "gate", ok: false, final: true, tip: s };
  }
  if (d.strategy === "policy_rewrite") return { k: "tag", label: "policy rewrite", loop: "same", tip: s };
  if (e.type === "retry_scheduled") return { k: "tag", label: "retry", tip: s, small: true };
  if (/rejected by QA/.test(s)) return { k: "qa", ok: false, loop: "clip", score: d.score, tip: s };
  if (/approved by QA/.test(s)) return { k: "qa", ok: true, score: d.score, tip: s };
  if (d.winner != null) return { k: "qa", ok: true, score: d.score, tip: s };
  if (e.type === "scene_status" && e.level === "error") {
    if (e.stage === "qa" || /QA/.test(s)) return { k: "qa", ok: false, final: true, score: d.score, tip: s };
    return { k: "end", ok: false, tip: s };
  }
  return null;
}

function tagWidth(st) { return Math.max(38, st.label.length * 6.1 + 12); }

function drawLanes() {
  const v = state.view, svg = $("#lanes");
  const scenes = v ? v.scenes : [];
  $("#lanes-empty").classList.toggle("hidden", scenes.length > 0);
  svg.innerHTML = "";
  if (!scenes.length) { svg.setAttribute("width", 0); svg.setAttribute("height", 0); return; }
  const byScene = new Map(scenes.map((sc) => [sc.id, []]));
  for (const e of state.events) {
    if (!e.scene_id || !byScene.has(e.scene_id)) continue;
    const st = stepOf(e);
    if (st) byScene.get(e.scene_id).push(st);
  }
  const ROW = 46, LABEL = 188, STEP = 30;
  let maxX = 0;
  scenes.forEach((sc, r) => {
    const steps = byScene.get(sc.id);
    const last = steps[steps.length - 1];
    if (sc.status === "approved" && !(last && last.k === "end")) steps.push({ k: "end", ok: true, tip: `Scene ${sc.order} approved${sc.qa != null ? ` · QA ${sc.qa.toFixed(2)}` : ""}` });
    if (sc.status === "failed" && !(last && (last.k === "end" || last.final))) steps.push({ k: "end", ok: false, tip: sc.failure || "failed" });
    if (sc.status === "failed" && last && last.final) steps.push({ k: "end", ok: false, tip: sc.failure || last.tip });
    const y0 = r * ROW, cy = y0 + 28;
    const row = el("g", { class: "row" }, svg);
    el("rect", { x: 0, y: y0, width: "100%", height: ROW, class: "row-bg" }, row);
    el("text", { x: 4, y: y0 + 24, class: "row-label" }, row).textContent = `S${sc.order}`;
    el("text", { x: 30, y: y0 + 24, class: "row-sub" }, row).textContent = (sc.vocab || sc.purpose).slice(0, 24);
    el("text", { x: 30, y: y0 + 37, class: "row-sub" }, row).textContent = `${sc.duration_s}s · v${sc.version} · ${STATUS_LABEL[sc.status] || sc.status}`;
    let x = LABEL;
    const pos = [];
    const xs = [];
    // node centres advance by STEP; a tag is a box whose left edge sits where a node would start
    steps.forEach((st) => {
      if (st.k === "tag") { const tw = tagWidth(st); xs.push(x - 8); x += tw + 14; }
      else { xs.push(x); x += STEP; }
    });
    if (steps.length) el("line", { x1: LABEL - 8, y1: cy, x2: xs[xs.length - 1], y2: cy, class: "track" }, row);
    steps.forEach((st, i) => {
      const sx = xs[i];
      const live = i === steps.length - 1 && v.busy && ["generating", "pending", "qa_review"].includes(sc.status) && st.k !== "end";
      const g = el("g", { class: `step ${st.k} ${st.ok === true ? "ok" : st.ok === false ? "fail" : ""} ${live ? "live" : ""} ${st.cached ? "cached" : ""}`, "data-tip": st.tip, "data-head": `S${sc.order} · ${st.k === "kf" ? "keyframe" : st.k}` }, row);
      if (st.k === "kf" || st.k === "clip") {
        el("rect", { x: sx - 8, y: cy - 8, width: 16, height: 16, rx: 2, class: "shape" }, g);
        el("text", { x: sx, y: cy, class: "glyph" }, g).textContent = st.k === "kf" ? "K" : st.n > 1 ? `×${st.n}` : "C";
      } else if (st.k === "gate" || st.k === "qa") {
        el("rect", { x: sx - 7, y: cy - 7, width: 14, height: 14, transform: `rotate(45 ${sx} ${cy})`, class: "shape" }, g);
        el("text", { x: sx, y: cy, class: "glyph" }, g).textContent = st.k === "gate" ? "G" : "Q";
        if (st.score != null) el("text", { x: sx, y: cy + 19, class: "score" }, g).textContent = Number(st.score).toFixed(2);
      } else if (st.k === "tag") {
        const tw = tagWidth(st);
        el("rect", { x: sx, y: cy - 9, width: tw, height: 18, rx: 2 }, g);
        el("text", { x: sx + 6, y: cy, class: "" }, g).textContent = st.label;
      } else if (st.k === "end") {
        el("circle", { cx: sx, cy, r: 7 }, g);
        el("path", { d: st.ok ? `M${sx - 3.5},${cy} l2.5,2.5 l4.5,-5` : `M${sx - 3},${cy - 3} l6,6 M${sx + 3},${cy - 3} l-6,6` }, g);
      }
      pos.push({ k: st.k, x: st.k === "tag" ? sx + tagWidth(st) / 2 : sx });
      if (st.loop) {
        const target = st.loop === "same" ? i - 1 : [...pos].slice(0, -1).map((p, j) => ({ ...p, j })).reverse().find((p) => p.k === st.loop)?.j;
        if (target != null && target >= 0) {
          const fx = pos[pos.length - 1].x, tx = pos[target].x, top = cy - 20;
          el("path", { d: `M${fx},${cy - 9} C${fx},${top} ${tx},${top} ${tx},${cy - 9}`, class: `loop ${st.ok === false ? "bad" : ""}`, "marker-end": "url(#lane-arrow)" }, row);
        }
      }
    });
    maxX = Math.max(maxX, x);
  });
  const defs = el("defs", {}, svg);
  const mk = el("marker", { id: "lane-arrow", viewBox: "0 0 8 8", refX: 6, refY: 4, markerWidth: 5, markerHeight: 5, orient: "auto" }, defs);
  el("path", { d: "M0,0 L8,4 L0,8 z", fill: "#e3a33f" }, mk);
  const width = Math.max($("#lanes-wrap").clientWidth, maxX + 24);
  svg.setAttribute("width", width);
  svg.setAttribute("height", scenes.length * ROW + 4);
}

// ------------------------------------------------------------------ budget burn + decisions
function drawBurn() {
  const svg = $("#burn"), v = state.view;
  svg.innerHTML = "";
  const W = 300, H = 84, pts = [];
  let cum = 0;
  const t0 = state.events.length ? Date.parse(state.events[0].ts) : 0;
  for (const e of state.events) {
    const c = e.data && e.data.cost_usd;
    if ((e.type === "llm_call" || e.type === "attempt_finished") && c) { cum += Number(c); pts.push([Date.parse(e.ts) - t0, cum]); }
  }
  const spent = v ? v.budget.spent : cum;
  const tEnd = state.events.length ? Date.parse(state.events[state.events.length - 1].ts) - t0 : 1;
  pts.unshift([0, 0]);
  pts.push([Math.max(tEnd, 1), Math.max(spent, cum)]);
  const ymax = Math.max(0.05, Math.max(spent, cum) * 1.2), xmax = Math.max(1, tEnd);
  const X = (t) => (t / xmax) * W, Y = (u) => H - 2 - (u / ymax) * (H - 14);
  [0.5, 1].forEach((f) => {
    el("line", { x1: 0, x2: W, y1: Y(ymax * f / 1.2), y2: Y(ymax * f / 1.2), class: "grid" }, svg);
  });
  let d = "";
  pts.forEach(([t, u], i) => { d += `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(u).toFixed(1)} `; });
  el("path", { d: `${d} L${W},${H} L0,${H} Z`, class: "area" }, svg);
  el("path", { d, class: "line" }, svg);
  el("text", { x: 2, y: 10, class: "tick" }, svg).textContent = money(Math.max(spent, cum));
}

function appendDecision(e) {
  if (!e.plain) return;
  const list = $("#decisions");
  list.querySelector(".empty")?.remove();
  const li = document.createElement("li");
  const text = e.plain.length > 200 ? `${e.plain.slice(0, 197)}…` : e.plain;
  li.innerHTML = `<span class="who ${e.actor}">${esc(e.actor)}</span><span dir="auto" title="${esc(e.plain)}">${esc(text)}</span>`;
  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
}

// ------------------------------------------------------------------ tooltips
function setupTips() {
  const tip = $("#tip");
  document.addEventListener("mousemove", (ev) => {
    const t = ev.target.closest && ev.target.closest("[data-tip]");
    if (!t || !t.dataset.tip) { tip.classList.add("hidden"); return; }
    tip.innerHTML = `${t.dataset.head ? `<div class="tip-h">${esc(t.dataset.head)}</div>` : ""}<div dir="auto">${esc(t.dataset.tip)}</div>`;
    tip.classList.remove("hidden");
    const r = tip.getBoundingClientRect();
    tip.style.left = `${Math.min(ev.clientX + 14, window.innerWidth - r.width - 8)}px`;
    tip.style.top = `${Math.min(ev.clientY + 14, window.innerHeight - r.height - 8)}px`;
  });
}

// ------------------------------------------------------------------ concepts, board, final
function renderConcepts(v) {
  $("#concepts-wrap").classList.toggle("hidden", !v.concepts.length);
  if (!v.concepts.length) return;
  const awaiting = v.stage === "awaiting_concept_choice";
  const rec = v.concepts.find((c) => c.recommended);
  const sel = v.concepts.find((c) => c.selected);
  $("#concepts-hint").textContent = awaiting
    ? (rec ? `The judge recommends 0${rec.number} · you pick` : "Pick one to continue")
    : sel ? (v.selected_by === "user" ? `You picked 0${sel.number}${rec ? ` · the judge recommended 0${rec.number}` : ""}` : `Code picked 0${sel.number} · the LLM scored, code weighted`) : "";
  $("#concepts").innerHTML = v.concepts.map((c) => {
    const flags = [];
    for (const [k, v] of Object.entries(c.rubric || {})) flags.push(`<span class="flag ${v >= 8 ? "good" : v <= 4 ? "bad" : ""}">${k.replace("_", " ")} ${v}</span>`);
    if (c.product_role) flags.push(`<span class="flag">product: ${c.product_role.role}</span>`);
    if (c.producibility != null) flags.push(`<span class="flag ${c.producibility >= 7 ? "good" : c.producibility <= 4 ? "bad" : ""}">producibility ${c.producibility}</span>`);
    if (c.originality != null && !c.rubric?.originality) flags.push(`<span class="flag ${c.originality >= 7 ? "good" : ""}">originality ${c.originality}</span>`);
    if (c.cliche === true) flags.push(`<span class="flag bad">cliché</span>`);
    if (c.cliche === false) flags.push(`<span class="flag">no cliché</span>`);
    if (c.single_shot === false) flags.push(`<span class="flag bad">needs an edit</span>`);
    if (c.product_late) flags.push(`<span class="flag bad">product late</span>`);
    const score = c.score != null ? `<div class="score"><div class="score-bar"><div style="width:${Math.max(4, c.score * 10)}%"></div></div><span class="score-num">${c.score.toFixed(2)}</span></div>` : "";
    const pick = awaiting ? `<button class="btn primary" data-pick="${c.number}">Choose this concept</button>` : "";
    return `<article class="concept ${c.selected ? "selected" : ""} ${c.recommended && !c.selected ? "recommended" : ""}">
      ${c.selected ? `<span class="badge-win">selected</span>` : c.recommended ? `<span class="badge-rec">judge's pick</span>` : ""}
      <span class="num">0${c.number}</span>
      <div class="title" dir="auto">${esc(c.title)}</div>
      <div class="logline" dir="auto">${esc(c.logline)}</div>
      <div><div class="label">Insight</div><div class="insight" dir="auto">${esc(c.insight)}</div></div>
      <div class="flags">${flags.join("")}</div>
      ${score}${pick}
    </article>`;
  }).join("");
  $("#concepts").onclick = (ev) => { const n = ev.target.dataset.pick; if (n) act("choose", { concept: n }); };
  $("#selection").textContent = v.selection || "";
}

function renderProductRef(v) {
  const box = $("#product-ref"), pr = v.product_reference;
  if (!pr || !pr.url) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const status = pr.approved
    ? `<span class="chip s-approved">locked · gate ${pr.score?.toFixed(2) ?? ""}</span>`
    : `<span class="chip s-failed">rejected · try ${pr.tries}</span>`;
  box.innerHTML = `<img src="${pr.url}" alt="product reference">
    <div><div class="section-label">Product reference</div>
    <div class="pr-text">One gated packshot. Every keyframe that shows the product is generated from it, so the product is identical in every shot.</div>
    <div class="pr-meta">${status}<span>${pr.tries} ${pr.tries === 1 ? "try" : "tries"} · ${money(pr.cost)}</span></div></div>`;
}

function renderBoard(v) {
  $("#board-wrap").classList.toggle("hidden", !v.scenes.length);
  if (!v.scenes.length) return;
  const blast = v.blast;
  const showBlast = blast && v.scenes.length;
  const editable = ["done", "done_partial", "awaiting_storyboard_approval", "errored"].includes(v.stage) && !v.busy;
  $("#edit").disabled = !editable; $("#edit-go").disabled = !editable;
  $("#edit").placeholder = editable ? "Edit the ad in plain words…  خل المشهد الثاني أبطأ" : "Edits open when the run is finished";
  const atSpend = v.stage === "awaiting_generation_approval";
  $("#approve-wrap").classList.toggle("hidden", v.stage !== "awaiting_storyboard_approval" && !atSpend);
  $("#approve-text").textContent = atSpend && v.generation
    ? `One generation of the whole ad is priced at ${money(v.generation.computed_usd ?? v.generation.est_usd ?? 0)}${v.generation.computed_usd != null && v.generation.computed_usd !== v.generation.est_usd ? `; ${money(v.generation.est_usd || 0)} is reserved with the safety margin` : ""} (${v.generation.model}, ${v.generation.duration_s}s ${v.generation.resolution}). Nothing is spent until you approve.`
    : "Keyframes are ready. Approve to start paid video generation.";
  $("#approve").textContent = atSpend ? `Approve ${money(v.generation?.est_usd || 0)}` : "Approve storyboard";
  renderGeneration(v);
  if (showBlast) {
    const parts = Object.entries(blast.affected).map(([o, d]) => `S${o} <b>${d}</b>`).join(" · ");
    const untouched = blast.untouched.length ? ` · untouched ${blast.untouched.map((o) => "S" + o).join(", ")} <b>$0</b>` : "";
    $("#blast").innerHTML = `<span class="icon">◎</span>
      <div class="what" dir="auto">Blast radius of <b>${esc(blast.edit.split(" → ")[0])}</b>: ${parts}${untouched}${blast.bible_bump ? " · <b>bible bumped</b>" : ""}</div>
      <div class="why" dir="auto">${esc(blast.explanation.join(" · "))}</div>`;
  }
  $("#blast").classList.toggle("hidden", !showBlast);
  const board = $("#board");
  board.innerHTML = "";
  for (const s of v.scenes) {
    const depth = showBlast ? blast.affected[String(s.order)] : null;
    const untouched = showBlast && !depth;
    const card = document.createElement("article");
    card.className = `scene ${s.status}${depth ? " blast-" + depth : ""}${untouched ? " blast-untouched" : ""}`;
    card.dataset.id = s.id;
    const media = s.keyframe
      ? `<img src="${s.keyframe}" alt="">${s.clip ? `<video src="${s.clip}" muted loop playsinline preload="metadata"></video>` : ""}`
      : `<div class="placeholder">${v.generation ? `shot ${s.order} of one generation` : s.status === "generating" ? "rendering" : "keyframe pending"}</div>`;
    const tag = depth ? `<div class="blast-tag ${depth}">${depth === "clip" ? "new clip · keyframe kept" : depth === "keyframe" ? "new keyframe + clip" : "new prompt · keyframe · clip"}</div>`
      : untouched ? `<div class="blast-tag untouched">untouched · $0</div>` : "";
    card.innerHTML = `<div class="media">${media}<div class="top"><span class="order">S${s.order}</span><span class="chip s-${s.status}">${STATUS_LABEL[s.status] || s.status}</span></div>${tag}</div>
      <div class="body">
        <div class="desc" dir="auto">${esc(s.description)}</div>
        <div class="meta" title="${esc(s.camera.replace("_", " "))} · ${s.attempts} attempts"><span>${s.duration_s}s · v${s.version}</span><span>${money(s.spend)}</span></div>
        ${s.human_override ? `<div class="recov" title="${esc(s.human_override)}"><span>human override</span></div>` : ""}
        ${s.recoveries.length ? `<div class="recov">${s.recoveries.map((r) => `<span>${esc(r)}</span>`).join("")}</div>` : ""}
        ${s.failure ? `<div class="fail" dir="auto">${esc(s.failure)}</div><button class="btn retry" data-retry="${s.order}">Retry scene</button>` : ""}
        ${s.qa != null && s.status === "approved" ? `<div class="meta"><span>QA ${s.qa.toFixed(2)}</span><span>${s.keyframe_approved ? "gate passed" : ""}</span></div>` : ""}
      </div>`;
    const vid = card.querySelector("video");
    if (vid) {
      vid.addEventListener("loadeddata", () => vid.classList.add("ready"));
      card.addEventListener("mouseenter", () => vid.play().catch(() => {}));
      card.addEventListener("mouseleave", () => { vid.pause(); vid.currentTime = 0; });
    }
    board.appendChild(card);
  }
  board.onclick = (ev) => { const n = ev.target.dataset.retry; if (n) act("retry", { scene: Number(n) }); };
}

function renderGeneration(v) {
  const box = $("#generation");
  const g = v.generation;
  box.classList.toggle("hidden", !g);
  if (!g) return;
  const scores = Object.entries(g.rubric || {}).map(([k, n]) => `<span class="flag ${n >= 8 ? "good" : n <= 4 ? "bad" : ""}">${esc(k.replace("_", " "))} ${n}</span>`).join("");
  const row = (k, val) => `<div class="gen-row"><span>${k}</span><b>${val}</b></div>`;
  const hard = g.score == null ? "–" : g.reasons.length ? `FAILED (${esc(g.reasons.join("; "))})` : `passed · ${g.score.toFixed(2)}`;
  const creative = g.creative_score == null ? "–" : `${g.creative_score.toFixed(2)}/10${g.warnings.length ? " · WARN" : ""}`;
  box.innerHTML = `<div class="gen-media">${g.video ? `<video src="${g.video}" controls playsinline preload="metadata"></video>` : `<div class="placeholder">not generated yet</div>`}</div>
    <div class="section-label">Whole ad · one generation <span class="chip s-${esc(g.status)}">${esc(g.status)}</span></div>
    ${row("model", `${esc(g.model)} · ${g.duration_s}s ${esc(g.resolution)} · audio ${g.audio ? "on" : "off"}`)}
    ${row("price", g.computed_usd != null ? money(g.computed_usd) : (g.est_usd != null ? money(g.est_usd) : "–"))}
    ${row("reserved", g.est_usd != null ? money(g.est_usd) + (g.computed_usd != null && g.computed_usd !== g.est_usd ? " (with margin)" : "") : "–")}
    ${row("approved by", g.approved_by ? esc(g.approved_by) : "waiting for you")}
    ${row("hard gate", hard)}
    ${row("creative", creative)}
    ${scores ? `<div class="flags">${scores}</div>` : ""}
    ${g.execution ? `<div class="flags">${[["handled", g.execution.product_handled], ["hook 0-2 s", g.execution.hook_first_2s], ["key moment", g.execution.key_moment_visible], ["one location", g.execution.one_location]].map(([k, ok]) => `<span class="flag ${ok ? "good" : "bad"}">${k}</span>`).join("")}<span class="flag">product ${g.execution.product_seconds}s</span></div>` : ""}
    ${g.observed_cuts && g.observed_cuts.length ? row("cuts seen", g.observed_cuts.map((c) => c.toFixed(2)).join(" / ") + " s") : ""}
    ${g.warnings.length ? `<div class="desc">${esc(g.warnings.join(" · "))}</div>` : ""}
    ${g.creative_note ? `<div class="desc" dir="auto">${esc(g.creative_note)}</div>` : ""}
    ${g.failure ? `<div class="fail" dir="auto">${esc(g.failure)}</div>` : ""}
    ${renderImages(g.images)}
    ${renderDirector(g.director)}
    <details><summary>Prompt sent to the model</summary><pre>${esc(g.prompt)}</pre></details>`;
}

function renderImages(images) {
  if (!images || !images.length) return "";
  const cards = images.map((im) => `<figure class="ref-image"><img src="${im.url}" alt="${esc(im.label)}" loading="lazy"><figcaption>${esc(im.label)}${im.source === "generated" ? ` · generated ${money(im.cost)}${im.check ? ` · ${esc(im.check)}` : ""}` : ""}</figcaption></figure>`).join("");
  return `<div class="section-label">Images the model receives</div><div class="ref-images">${cards}</div>`;
}

function renderDirector(d) {
  if (!d) return "";
  const label = { kept: "writer's prompt kept", rewritten: "director's rewrite used", fallback: "writer's prompt kept (rewrite failed the checks)" }[d.outcome] || d.outcome;
  const rounds = d.rounds.map((r) => {
    const flags = r.verdicts.map((x) => `<span class="flag ${x.passed ? "good" : "bad"}" title="${esc(x.note)}">${esc(x.rule_id.replaceAll("_", " "))}</span>`).join("");
    const changes = r.changes.length ? `<ul class="desc" dir="auto">${r.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : "";
    return `<div class="gen-row"><span>round ${r.round}</span><b>${r.failed.length ? `${r.failed.length} rule(s) failed` : "all rules pass"}</b></div><div class="flags">${flags}</div><div class="desc" dir="auto">${esc(r.summary)}</div>${changes}`;
  }).join("");
  const diff = d.outcome === "rewritten" ? `<details><summary>Writer's prompt (before the director)</summary><pre>${esc(d.before)}</pre></details>` : "";
  return `<div class="section-label">Creative director · ${esc(label)}${d.retimed ? " · re-timed" : ""}</div>${rounds}${d.note ? `<div class="desc warn">${esc(d.note)}</div>` : ""}${diff}`;
}

function renderFinal(v) {
  $("#final-wrap").classList.toggle("hidden", !v.final);
  if (!v.final) return;
  const video = $("#final-video");
  if (video.dataset.src !== v.final) { video.src = `${v.final}#t=0.6`; video.dataset.src = v.final; }
  const approved = v.scenes.filter((s) => s.status === "approved").length;
  const b = v.budget, c = b.by_category;
  $("#final-stats").innerHTML = [
    ["Duration", `${(v.final_duration || 0).toFixed(1)}s`],
    v.generation ? ["Whole ad", `one generation · ${v.generation.status}`] : ["Scenes", `${approved}/${v.scenes.length} approved`],
    ["Media spend", money((c.image || 0) + (c.video || 0) + (c.audio || 0))],
    ["LLM spend", money(c.llm || 0)],
  ].map(([k, val]) => `<div class="stat"><div class="k">${k}</div><div class="v">${val}</div></div>`).join("");
  const e = v.endcard;
  $("#endcard-note").innerHTML = e ? `<div class="section-label">End card</div><div style="margin-top:6px"><b dir="auto">${esc(e.brand_name)}</b> · <span dir="auto">${esc(e.tagline)}</span></div>${e.brand_placeholder || e.tagline_placeholder ? `<div class="ph">Placeholder brand invented by the agent: replace before publishing.</div>` : ""}` : "";
  $("#pack").href = `/api/runs/${v.id}/pack.zip`;
}

function renderBudget(b) {
  const pct = b.limit ? Math.min(100, ((b.spent + b.reserved) / b.limit) * 100) : 0;
  $("#meter-fill").style.width = `${pct}%`;
  $("#meter-text").textContent = `${money(b.spent)} of ${money(b.limit)}${b.reserved ? `  ·  ${money(b.reserved)} reserved` : ""}`;
  $("#meter-note").textContent = b.simulated === true ? "Simulated: mock media priced at real rates, no money spent." : b.simulated === false ? "Real spend (fal + Anthropic)." : "";
  $("#meter-cats").innerHTML = Object.entries(b.by_category).map(([k, val]) => `<span>${k} ${money(val)}</span>`).join("");
}

// ------------------------------------------------------------------ trace
function visible(e) { return state.filter === "all" || e.tags.includes(state.filter); }

function traceItem(e) {
  const li = document.createElement("li");
  li.className = `t-${e.type} lvl-${e.level}`;
  li.innerHTML = `<span class="ic">${ICONS[e.type] || "·"}</span><span><span class="st">${esc(e.stage)}</span><span dir="auto">${esc(e.summary)}</span>${e.tags.includes("chaos") ? `<span class="tagc">chaos</span>` : ""}</span>`;
  return li;
}

function appendTrace(e) {
  const list = $("#trace");
  list.querySelector(".empty")?.remove();
  if (!visible(e)) return;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  list.appendChild(traceItem(e));
  if (nearBottom) list.scrollTop = list.scrollHeight;
}

function renderTrace() {
  const list = $("#trace");
  list.innerHTML = "";
  state.events.filter(visible).forEach((e) => list.appendChild(traceItem(e)));
  list.scrollTop = list.scrollHeight;
}

// ------------------------------------------------------------------ landing teaser + gallery
// The landing teaser: the two montages, then the curtain before/after pair (the hero-spray
// close-up shows invented label text, so it is kept for the gallery only).
const TEASER = ["montage_v2", "montage_v1", "curtain"];

async function loadTeaser() {
  const items = await api("/api/gallery").catch(() => []);
  const box = $("#teaser");
  const pick = TEASER.map((k) => items.find((it) => it.key === k)).filter(Boolean);
  if (!pick.length) { box.classList.add("hidden"); return; }
  const media = (v) => `<video src="${v.url}${v.poster ? `#t=${v.poster}` : ""}" muted playsinline preload="auto"></video>`;
  box.innerHTML = `<div class="teaser-head"><span class="section-label">Made with real models</span><button class="link" id="teaser-all">All real runs →</button></div>
    <div class="teaser-row">${pick.map((it) => {
      const pair = it.videos.length > 1;
      const body = pair
        ? `<div class="teaser-pair">${it.videos.slice(0, 2).map((v) => `<div>${media(v)}<em>${esc(v.label)}</em></div>`).join("")}</div>`
        : media(it.videos[0]);
      return `<button class="teaser-card" data-key="${esc(it.key)}">${body}<span>${esc(it.title)}</span></button>`; }).join("")}</div>`;
  box.onclick = (ev) => { if (ev.target.closest(".teaser-card") || ev.target.id === "teaser-all") showView("gallery"); };
}

async function loadGallery() {
  const items = await api("/api/gallery");
  $("#gallery-grid").innerHTML = items.length ? [...items].reverse().map((it) => `
    <article class="g-card">
      <div class="g-videos">${it.videos.map((v) => `<div class="g-video"><video src="${v.url}${v.poster ? `#t=${v.poster}` : ""}" controls muted loop playsinline preload="auto"></video><div class="vlabel ${/rejected|before/i.test(v.label) ? "bad" : /after|winner/i.test(v.label) ? "good" : ""}">${esc(v.label)}</div></div>`).join("")}</div>
      <div class="g-text">
        <div class="subtitle">${esc(it.subtitle)}</div>
        <h3 dir="auto">${esc(it.title)}</h3>
        <p dir="auto">${esc(it.caption)}</p>
        ${it.defects ? `<p class="defects">${esc(it.defects)}</p>` : ""}
        ${it.endcard ? `<div class="meta" dir="auto">end card: ${esc(it.endcard.brand_name)} · ${esc(it.endcard.tagline)}</div>` : ""}
      </div>
    </article>`).join("") : `<p class="sub">No real runs in this data directory yet.</p>`;
}

window.addEventListener("resize", () => queueDraw());
boot();
