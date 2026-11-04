// Adsmith Studio: vanilla JS over the REST + SSE API (no build step).
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isAr = (s) => /[؀-ۿ]/.test(s || "");
const money = (v) => `$${Number(v || 0).toFixed(2)}`;

const state = { run: null, view: null, es: null, filter: "all", events: [], refreshTimer: null, poll: null };

const ICONS = {
  stage_started: "▶", stage_completed: "✓", decision: "•", attempt_finished: "■", retry_scheduled: "↻",
  recovery: "✎", cache_hit: "⚡", scene_status: "◆", edit_parsed: "✂", blast_radius: "◎", llm_call: "·",
  llm_repair: "⟲", awaiting_input: "?", export: "⤓", error: "✗", run_completed: "★",
};
const STATUS_LABEL = { pending: "pending", generating: "generating", qa_review: "QA", approved: "approved", failed: "failed", stale: "stale" };
const EDIT_SUGGESTIONS = ["خل المشهد الثاني أبطأ", "make scene 3 at night", "غيّر لبسه لثوب أسود"];

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
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
    renderTrace(true);
  };
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => showView(t.dataset.view)));
  $("#recent").onchange = (ev) => ev.target.value && openRun(ev.target.value);
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
function showError(msg) { const el = $("#error"); el.textContent = msg; el.classList.toggle("hidden", !msg); }

// ------------------------------------------------------------------ run + SSE
function openRun(id) {
  state.run = id; state.events = []; location.hash = `run=${id}`;
  showView("studio");
  $("#run").classList.remove("hidden");
  $("#teaser").classList.add("hidden");
  $("#trace").innerHTML = "";
  if (state.es) state.es.close();
  state.es = new EventSource(`/api/runs/${id}/events`);
  state.es.onmessage = (m) => { const e = JSON.parse(m.data); state.events.push(e); appendTrace(e); scheduleRefresh(); };
  state.es.onopen = () => $("#live-dot").classList.add("on");
  state.es.onerror = () => $("#live-dot").classList.remove("on");
  refresh();
  clearInterval(state.poll);
  state.poll = setInterval(() => state.view && state.view.busy && refresh(), 2000);
}

function scheduleRefresh() { clearTimeout(state.refreshTimer); state.refreshTimer = setTimeout(refresh, 220); }

async function refresh() {
  if (!state.run) return;
  try { state.view = await api(`/api/runs/${state.run}`); render(state.view); } catch (e) { alertError(e); }
}

// ------------------------------------------------------------------ render
function render(v) {
  $("#run-request").textContent = v.request;
  $("#run-request").dir = isAr(v.request) ? "rtl" : "ltr";
  const meta = [];
  if (v.brief) meta.push(`${v.brief.duration_s}s`, v.brief.dialect.replace("_", " "));
  meta.push(v.interactive ? "interactive" : "auto");
  if (v.chaos) meta.push(`<span class="pill warn">chaos on</span>`);
  meta.push(`<span class="pill">${esc(v.stage.replaceAll("_", " "))}</span>`);
  $("#run-meta").innerHTML = meta.map((m) => (m.startsWith("<") ? m : `<span class="pill">${esc(m)}</span>`)).join("");
  $("#pipeline").classList.toggle("idle", !v.busy);
  $("#pipeline").innerHTML = v.pipeline.map((p, i) => `<li class="${p.state}"><span class="n">${String(i + 1).padStart(2, "0")}</span>${esc(p.en)}</li>`).join("");
  renderConcepts(v);
  renderBoard(v);
  renderFinal(v);
  renderBudget(v.budget);
  showError(v.error || "");
}

function renderConcepts(v) {
  $("#concepts-wrap").classList.toggle("hidden", !v.concepts.length);
  if (!v.concepts.length) return;
  const awaiting = v.stage === "awaiting_concept_choice";
  const rec = v.concepts.find((c) => c.recommended);
  const sel = v.concepts.find((c) => c.selected);
  $("#concepts-hint").textContent = awaiting
    ? (rec ? `The judge recommends 0${rec.number} · you pick` : "Pick one to continue")
    : sel ? (v.selected_by === "user" ? `You picked 0${sel.number}${rec ? ` · the judge recommended 0${rec.number}` : ""}` : `The agent picked 0${sel.number} · weighted rubric, code decides`) : "";
  const maxScore = 10;
  $("#concepts").innerHTML = v.concepts.map((c) => {
    const flags = [];
    if (c.producibility != null) flags.push(`<span class="flag ${c.producibility >= 7 ? "good" : c.producibility <= 4 ? "bad" : ""}">producibility ${c.producibility}/10</span>`);
    if (c.originality != null) flags.push(`<span class="flag ${c.originality >= 7 ? "good" : ""}">originality ${c.originality}/10</span>`);
    if (c.cliche === true) flags.push(`<span class="flag bad">cliché</span>`);
    if (c.cliche === false) flags.push(`<span class="flag good">no cliché</span>`);
    if (c.single_shot === false) flags.push(`<span class="flag bad">needs an edit to work</span>`);
    if (c.product_late) flags.push(`<span class="flag bad">product only at end</span>`);
    const score = c.score != null ? `<div class="score"><div class="score-bar"><div style="width:${Math.max(4, (c.score / maxScore) * 100)}%"></div></div><span class="score-num">${c.score.toFixed(2)}</span></div>` : "";
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

function renderBoard(v) {
  $("#board-wrap").classList.toggle("hidden", !v.scenes.length);
  if (!v.scenes.length) return;
  const blast = v.blast;
  const showBlast = blast && v.scenes.length;
  const editable = ["done", "done_partial", "awaiting_storyboard_approval", "errored"].includes(v.stage) && !v.busy;
  $("#edit").disabled = !editable; $("#edit-go").disabled = !editable;
  $("#edit").placeholder = editable ? "Edit the ad in plain words…  خل المشهد الثاني أبطأ" : "Edits open when the run is finished";
  $("#approve-wrap").classList.toggle("hidden", v.stage !== "awaiting_storyboard_approval");
  if (showBlast) {
    const parts = Object.entries(blast.affected).map(([o, d]) => `S${o} <b>${d}</b>`).join(" · ");
    const untouched = blast.untouched.length ? ` · untouched ${blast.untouched.map((o) => "S" + o).join(", ")} <b>$0</b>` : "";
    $("#blast").innerHTML = `<span class="icon">◎</span>
      <div class="what" dir="auto">Blast radius of <b>${esc(blast.edit.split(" → ")[0])}</b>: ${parts}${untouched}${blast.bible_bump ? " · <b>bible bumped</b>" : ""}</div>
      <div class="why" dir="auto">${esc(blast.explanation.join(" · "))}</div>`;
  }
  $("#blast").classList.toggle("hidden", !showBlast);
  const board = $("#board");
  const existing = new Map([...board.children].map((el) => [el.dataset.id, el]));
  board.innerHTML = "";
  for (const s of v.scenes) {
    const depth = showBlast ? blast.affected[String(s.order)] : null;
    const untouched = showBlast && !depth;
    const el = document.createElement("article");
    el.className = `scene ${s.status}${depth ? " blast-" + depth : ""}${untouched ? " blast-untouched" : ""}`;
    el.dataset.id = s.id;
    const prev = existing.get(s.id);
    const media = s.keyframe
      ? `<img src="${s.keyframe}" alt="">${s.clip ? `<video src="${s.clip}" muted loop playsinline preload="metadata"></video>` : ""}`
      : `<div class="placeholder">${s.status === "generating" ? "rendering…" : "keyframe pending"}</div>`;
    const tag = depth ? `<div class="blast-tag ${depth}">${depth === "clip" ? "new clip · keyframe kept" : depth === "keyframe" ? "new keyframe + clip" : "new prompt · keyframe · clip"}</div>`
      : untouched ? `<div class="blast-tag untouched">untouched · $0</div>` : "";
    el.innerHTML = `<div class="media">${media}<div class="top"><span class="order">S${s.order}</span><span class="chip s-${s.status}">${STATUS_LABEL[s.status] || s.status}</span></div>${tag}</div>
      <div class="body">
        <div class="desc" dir="auto">${esc(s.description)}</div>
        <div class="meta" title="${esc(s.camera.replace("_", " "))} · ${s.attempts} attempts"><span>${s.duration_s}s · v${s.version}</span><span>${money(s.spend)}</span></div>
        ${s.recoveries.length ? `<div class="recov">${s.recoveries.map((r) => `<span>${r === "retry" ? "↻" : "✎"} ${esc(r)}</span>`).join("")}</div>` : ""}
        ${s.failure ? `<div class="fail" dir="auto">${esc(s.failure)}</div><button class="btn retry" data-retry="${s.order}">Retry scene</button>` : ""}
        ${s.qa != null && s.status === "approved" ? `<div class="meta"><span>QA ${s.qa.toFixed(2)}</span><span>${s.keyframe_approved ? "keyframe gate ✓" : ""}</span></div>` : ""}
      </div>`;
    const vid = el.querySelector("video");
    if (vid) {
      vid.addEventListener("loadeddata", () => vid.classList.add("ready"));
      el.addEventListener("mouseenter", () => vid.play().catch(() => {}));
      el.addEventListener("mouseleave", () => { vid.pause(); vid.currentTime = 0; });
    }
    if (prev && prev.querySelector("img")?.getAttribute("src") === s.keyframe) { /* same media: no flicker */ }
    board.appendChild(el);
  }
  board.onclick = (ev) => { const n = ev.target.dataset.retry; if (n) act("retry", { scene: Number(n) }); };
}

function renderFinal(v) {
  $("#final-wrap").classList.toggle("hidden", !v.final);
  if (!v.final) return;
  const video = $("#final-video");
  if (video.dataset.src !== v.final) { video.src = v.final; video.dataset.src = v.final; }
  const approved = v.scenes.filter((s) => s.status === "approved").length;
  const b = v.budget;
  $("#final-stats").innerHTML = [
    ["Duration", `${(v.final_duration || 0).toFixed(1)}s`],
    ["Scenes", `${approved}/${v.scenes.length} approved`],
    ["Media spend", money((b.by_category.image || 0) + (b.by_category.video || 0))],
    ["LLM spend", money(b.by_category.llm || 0)],
  ].map(([k, val]) => `<div class="stat"><div class="k">${k}</div><div class="v">${val}</div></div>`).join("");
  const e = v.endcard;
  $("#endcard-note").innerHTML = e ? `End card<br><b dir="auto">${esc(e.brand_name)}</b> · <span dir="auto">${esc(e.tagline)}</span>${e.brand_placeholder || e.tagline_placeholder ? `<br><span class="ph">placeholder brand · invented by the agent · replace before publishing</span>` : ""}` : "";
  $("#pack").href = `/api/runs/${v.id}/pack.zip`;
}

function renderBudget(b) {
  const pct = b.limit ? Math.min(100, ((b.spent + b.reserved) / b.limit) * 100) : 0;
  $("#meter-fill").style.width = `${pct}%`;
  $("#meter-fill").style.backgroundPosition = `${100 - pct}% 0`;
  $("#meter-text").textContent = `${money(b.spent)} of ${money(b.limit)}${b.reserved ? `  ·  ${money(b.reserved)} reserved` : ""}`;
  $("#meter-note").textContent = b.simulated === true ? "simulated: mock media priced at real Kling rates, no money spent" : b.simulated === false ? "real spend (fal + Anthropic)" : "";
  $("#meter-cats").innerHTML = Object.entries(b.by_category).map(([k, val]) => `<span>${k} ${money(val)}</span>`).join("");
}

// ------------------------------------------------------------------ trace
function visible(e) { return state.filter === "all" || e.tags.includes(state.filter); }

function traceItem(e) {
  const li = document.createElement("li");
  li.className = `t-${e.type} lvl-${e.level}${e.tags.includes("chaos") ? " chaos" : ""}`;
  li.innerHTML = `<span class="ic">${ICONS[e.type] || "•"}</span><span><span class="st">${esc(e.stage)}</span><span dir="auto">${esc(e.summary)}</span>${e.tags.includes("chaos") ? `<span class="tagc">chaos</span>` : ""}</span>`;
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

// ------------------------------------------------------------------ landing teaser
async function loadTeaser() {
  const items = await api("/api/gallery").catch(() => []);
  const el = $("#teaser");
  if (!items.length) { el.classList.add("hidden"); return; }
  el.innerHTML = `<div class="teaser-head"><span class="eyebrow">Made with real models</span><button class="link" id="teaser-all">See all real runs →</button></div>
    <div class="teaser-row">${items.map((it) => { const v = it.videos[it.videos.length > 1 ? 0 : 0];
      return `<button class="teaser-card" data-key="${esc(it.key)}"><video src="${v.url}${v.poster ? `#t=${v.poster}` : ""}" muted playsinline preload="auto"></video><span>${esc(it.title)}</span></button>`; }).join("")}</div>`;
  el.onclick = (ev) => { if (ev.target.closest(".teaser-card") || ev.target.id === "teaser-all") showView("gallery"); };
}

// ------------------------------------------------------------------ gallery
async function loadGallery() {
  const items = await api("/api/gallery");
  $("#gallery-grid").innerHTML = items.length ? items.map((it) => `
    <article class="g-card">
      <div class="g-videos">${it.videos.map((v) => `<div class="g-video"><video src="${v.url}${v.poster ? `#t=${v.poster}` : ""}" controls muted loop playsinline preload="auto"></video><div class="vlabel ${/rejected|before/i.test(v.label) ? "bad" : /after|winner/i.test(v.label) ? "good" : ""}">${esc(v.label)}</div></div>`).join("")}</div>
      <div class="g-text">
        <div class="subtitle">${esc(it.subtitle)}</div>
        <h3>${esc(it.title)}</h3>
        <p>${esc(it.caption)}</p>
        ${it.defects ? `<p class="defects">${esc(it.defects)}</p>` : ""}
        ${it.endcard ? `<div class="meta" dir="auto">end card: ${esc(it.endcard.brand_name)} · ${esc(it.endcard.tagline)}</div>` : ""}
      </div>
    </article>`).join("") : `<p class="sub">No real runs in this data directory yet.</p>`;
}

boot();
