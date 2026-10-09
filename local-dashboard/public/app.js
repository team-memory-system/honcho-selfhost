const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  config: null,
  apiKey: sessionStorage.getItem("honcho_api_key") || "",
  workspace: localStorage.getItem("honcho_workspace") || "",
  workspaces: [], peers: [], sessions: [], conclusions: [], queue: null,
  totals: { peers: 0, sessions: 0, conclusions: 0 },
  chatBusy: false,
  mcpTools: null,
  audit: null,
  selectedPeer: null, selectedSession: null,
};

const titles = {
  overview: ["WORKSPACE OVERVIEW", "기억의 현재 상태"], peers: ["PEER DIRECTORY", "기억 속 주체"],
  sessions: ["SESSION LOGS", "대화의 타임라인"], memory: ["MEMORY EXPLORER", "근거와 결론"],
  dialectic: ["DIALECTIC CHAT", "Honcho와 대화"],
  mcp: ["MCP TOOL CONTROL", "에이전트 도구 권한"],
  audit: ["MCP CALL AUDIT", "누가 무엇을 물었는가"],
};

function esc(value = "") { const d = document.createElement("div"); d.textContent = String(value); return d.innerHTML; }
function shortDate(value) { if (!value) return "—"; return new Intl.DateTimeFormat("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value)); }
function initials(id = "?") { return id.split(/[-_\s]/).filter(Boolean).slice(0, 2).map(x => x[0]).join("").toUpperCase(); }
function items(page) { return Array.isArray(page) ? page : page?.items || []; }
function flash(message, error = false) { const el = $("#flash"); el.textContent = message; el.className = `show${error ? " error" : ""}`; clearTimeout(flash.timer); flash.timer = setTimeout(() => el.className = "", 2600); }
function setChatBusy(busy) {
  state.chatBusy = busy;
  const button = $("#chat-form button");
  button.disabled = busy;
  button.textContent = busy ? "…" : "↑";
  $("#chat-query").placeholder = busy ? "Peer들이 기억을 탐색하고 있습니다…" : "Honcho에게 질문…";
}
function updateObserverCount() {
  const selected = $$("#observer-peers input:checked").length;
  const total = $$("#observer-peers input").length;
  $("#observer-limit").textContent = `${selected}/${total}개 선택 · 제한 없음`;
}

async function api(path, options = {}) {
  const headers = { accept: "application/json", ...(options.body ? { "content-type": "application/json" } : {}), ...options.headers };
  if (state.apiKey) headers["x-honcho-api-key"] = state.apiKey;
  const response = await fetch(`/api${path}`, { ...options, headers, body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body });
  const contentType = response.headers.get("content-type") || "";
  const result = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(result?.detail?.[0]?.msg || result?.detail || result?.error || result || `HTTP ${response.status}`);
  return result;
}

async function init() {
  state.config = await fetch("/api/dashboard/config").then(r => r.json());
  $("#connection-url").textContent = state.config.honcho_url;
  if (state.config.has_audit_log) $("#nav-audit").hidden = false;
  bindEvents();
  await loadWorkspaces();
}

async function loadWorkspaces() {
  setConnection("checking", "연결 중");
  try {
    const page = await api("/v3/workspaces/list?page=1&size=100", { method: "POST", body: {} });
    state.workspaces = items(page);
    if (!state.workspace || !state.workspaces.some(w => w.id === state.workspace)) state.workspace = state.workspaces[0]?.id || "";
    $("#workspace-select").innerHTML = state.workspaces.length ? state.workspaces.map(w => `<option value="${esc(w.id)}" ${w.id === state.workspace ? "selected" : ""}>${esc(w.id)}</option>`).join("") : `<option value="">워크스페이스 없음</option>`;
    setConnection("online", "연결됨");
    if (state.workspace) await loadWorkspace(); else renderEmptyWorkspace();
  } catch (error) {
    setConnection("offline", "연결 실패");
    flash(error.message, true);
    renderEmptyWorkspace(error.message);
  }
}

async function loadWorkspace() {
  localStorage.setItem("honcho_workspace", state.workspace);
  const w = encodeURIComponent(state.workspace);
  try {
    const [peers, sessionPage, conclusions, queue] = await Promise.all([
      api(`/v3/workspaces/${w}/peers/list?page=1&size=100`, { method: "POST", body: {} }),
      api(`/v3/workspaces/${w}/sessions/list?page=1&size=100`, { method: "POST", body: {} }),
      api(`/v3/workspaces/${w}/conclusions/list?page=1&size=100`, { method: "POST", body: {} }),
      api(`/v3/workspaces/${w}/queue/status`),
    ]);
    let latestSessions = sessionPage;
    if (sessionPage.pages > 1) {
      latestSessions = await api(`/v3/workspaces/${w}/sessions/list?page=${sessionPage.pages}&size=100`, { method: "POST", body: {} });
    }
    state.peers = items(peers);
    state.sessions = items(latestSessions).sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    state.conclusions = items(conclusions);
    state.queue = queue;
    state.totals = { peers: peers.total ?? state.peers.length, sessions: sessionPage.total ?? state.sessions.length, conclusions: conclusions.total ?? state.conclusions.length };
    renderAll();
    setConnection("online", "연결됨");
    $("#last-updated").textContent = `업데이트 ${new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })}`;
  } catch (error) { setConnection("offline", "일부 오류"); flash(error.message, true); }
}

function setConnection(status, label) {
  $("#connection-dot").className = status; $("#connection-label").textContent = label;
}

async function loadMcpTools() {
  try {
    state.mcpTools = await api("/dashboard/mcp/tools");
    renderMcpTools();
  } catch (error) {
    state.mcpTools = { tools: [], error: error.message };
    renderMcpTools();
  }
}

function renderMcpTools() {
  const data = state.mcpTools || { tools: [] };
  $("#mcp-tool-count").textContent = data.error ? "오류" : `${data.enabled_count} / ${data.total_count}`;
  $("#mcp-nav-count").textContent = data.error ? "!" : data.enabled_count ?? "—";
  $("#mcp-bridge-label").textContent = data.error ? "상태 확인 실패" : data.bridge_running ? "● Bridge 실행 중" : "● Bridge 중지됨";
  $("#mcp-bridge-label").className = data.bridge_running ? "online" : "offline";
  const groups = Object.groupBy ? Object.groupBy(data.tools || [], tool => tool.group) : (data.tools || []).reduce((result, tool) => ((result[tool.group] ||= []).push(tool), result), {});
  $("#mcp-tool-list").innerHTML = data.error
    ? `<div class="empty-inline">${esc(data.error)}</div>`
    : Object.entries(groups).map(([group, tools]) => `<section class="panel mcp-tool-group"><div class="mcp-group-head"><div><p class="eyebrow">${esc(group.toUpperCase())}</p><h2>${esc(group)} 도구</h2></div><span>${tools.filter(tool => tool.enabled).length}/${tools.length} ON</span></div><div class="mcp-tool-grid">${tools.map(tool => { const access = { write: ["쓰기", "write"], danger: ["삭제", "danger"], llm: ["LLM", "llm"] }[tool.access] || ["조회", "read"]; return `<label class="mcp-tool-card ${tool.enabled ? "enabled" : "disabled"}"><div class="mcp-tool-head"><div><code>${esc(tool.name)}</code><em class="access-badge ${access[1]}">${access[0]}</em></div><span class="switch"><input type="checkbox" data-mcp-tool="${esc(tool.name)}" ${tool.enabled ? "checked" : ""}><span></span></span></div><p class="tool-description">${esc(tool.description)}</p><p class="tool-translation">${esc(tool.translation)}</p></label>`; }).join("")}</div></section>`).join("");
}
async function loadAudit() {
  const form = $("#audit-filter-form");
  const params = new URLSearchParams([...new FormData(form)].filter(([, value]) => value !== ""));
  $("#audit-list").innerHTML = `<div class="empty-inline">기록을 불러오는 중…</div>`;
  try {
    state.audit = await api(`/dashboard/audit?${params}`);
  } catch (error) {
    state.audit = { error: error.message };
  }
  renderAudit();
}

function renderAudit() {
  const data = state.audit || {};
  const rows = data.rows || [];
  const summary = data.summary || {};
  const total = Object.values(summary).reduce((sum, n) => sum + n, 0);
  $("#audit-total").textContent = data.error ? "오류" : total;
  $("#audit-nav-count").textContent = data.error ? "!" : (summary.denied || 0) || total || "—";
  $("#audit-denied-label").textContent = data.error ? "확인 실패" : `거부 ${summary.denied || 0} · 오류 ${summary.error || 0}`;
  $("#audit-schema").textContent = data.schema ? `스키마 ${data.schema}` : "—";
  $("#audit-count").textContent = data.error ? "—" : `${rows.length}건`;

  if (data.error) { $("#audit-list").innerHTML = `<div class="empty-inline">${esc(data.error)}</div>`; return; }
  if (data.enabled === false) { $("#audit-list").innerHTML = `<div class="empty-inline">${esc(data.reason || "감사 로그가 설정되지 않았습니다.")}</div>`; return; }
  if (!rows.length) { $("#audit-list").innerHTML = `<div class="empty-inline">조건에 맞는 호출이 없습니다.</div>`; return; }

  $("#audit-list").innerHTML = rows.map(row => {
    const badge = { ok: ["통과", "read"], denied: ["거부", "danger"], error: ["오류", "write"] }[row.status] || ["?", "read"];
    const scoreBadge = (value, label) => value === null || value === undefined ? "" : `<em class="access-badge llm">${label} ${Number(value).toFixed(2)}</em>`;
    const score = scoreBadge(row.jev_score, "판정") + scoreBadge(row.answer_score, "답 판정");
    return `<article class="audit-row ${esc(row.status)}">
      <div class="audit-head">
        <code>${esc(row.tool)}</code>
        <em class="access-badge ${badge[1]}">${badge[0]}</em>
        <span class="audit-bridge">${esc(row.bridge)}</span>
        ${score}
        <span class="audit-when">${esc(shortDate(row.at))}</span>
      </div>
      <p class="audit-caller">${esc(row.caller)} <small>${esc(row.caller_source)}</small>${row.workspace_id ? ` · ${esc(row.workspace_id)}` : ""}${row.duration_ms === null ? "" : ` · ${esc(row.duration_ms)}ms`}</p>
      ${row.query_text ? `<p class="audit-query">${esc(row.query_text)}</p>` : `<p class="audit-query muted">질의 원문 없음</p>`}
      ${row.error ? `<p class="audit-error">${esc(row.error)}</p>` : ""}
    </article>`;
  }).join("");
}

function renderEmptyWorkspace(message = "Workspace를 먼저 생성하세요.") { $("#recent-sessions").innerHTML = `<div class="empty-inline">${esc(message)}</div>`; }
function renderAll() { renderOverview(); renderPeerList(); renderSessionList(); renderConclusions(); fillSelectors(); }

function renderOverview() {
  const q = state.queue || {}; const total = q.total_work_units || 0; const completed = q.completed_work_units || 0;
  const percent = total ? Math.round(completed / total * 100) : 100;
  $("#metric-peers").textContent = state.totals.peers.toLocaleString(); $("#peer-count").textContent = state.totals.peers.toLocaleString();
  $("#metric-sessions").textContent = state.totals.sessions.toLocaleString(); $("#session-count").textContent = state.totals.sessions.toLocaleString();
  $("#metric-conclusions").textContent = state.totals.conclusions.toLocaleString();
  $("#metric-queue").textContent = q.pending_work_units || 0; $("#metric-queue-detail").textContent = `처리 중 ${q.in_progress_work_units || 0}`;
  $("#queue-completed").textContent = completed; $("#queue-progressing").textContent = q.in_progress_work_units || 0; $("#queue-pending").textContent = q.pending_work_units || 0;
  $("#queue-progress").textContent = `${percent}%`; $("#queue-ring").style.setProperty("--progress", `${percent * 3.6}deg`);
  $("#queue-state").textContent = (q.pending_work_units || q.in_progress_work_units) ? "PROCESSING" : "IDLE";
  $("#recent-sessions").innerHTML = [...state.sessions].sort((a,b) => new Date(b.created_at)-new Date(a.created_at)).slice(0,5).map(s => `<div class="stack-row" data-session="${esc(s.id)}"><span class="row-icon">≋</span><div><strong>${esc(s.id)}</strong><small>${s.is_active ? "활성 세션" : "종료된 세션"}</small></div><time>${shortDate(s.created_at)}</time></div>`).join("") || `<div class="empty-inline">세션이 없습니다.</div>`;
  $("#recent-conclusions").innerHTML = state.conclusions.slice(0,6).map(c => `<article class="conclusion-card"><p>${esc(c.content)}</p><small>${esc(c.observer_id)} → ${esc(c.observed_id)} · ${shortDate(c.created_at)}</small></article>`).join("") || `<div class="empty-inline">아직 도출된 결론이 없습니다.</div>`;
}

function renderPeerList(filter = "") {
  const list = state.peers.filter(p => p.id.toLowerCase().includes(filter.toLowerCase()));
  $("#peer-list").innerHTML = list.map(p => `<button class="entity-row ${state.selectedPeer === p.id ? "active" : ""}" data-peer="${esc(p.id)}"><span class="avatar">${initials(p.id)}</span><span><strong>${esc(p.id)}</strong><small>${shortDate(p.created_at)} 생성</small></span><small>→</small></button>`).join("") || `<div class="empty-inline">Peer가 없습니다.</div>`;
}

async function selectPeer(id) {
  state.selectedPeer = id; renderPeerList($("#peer-filter").value); const detail = $("#peer-detail");
  detail.innerHTML = `<div class="empty-inline">기억을 불러오는 중…</div>`;
  const w = encodeURIComponent(state.workspace), p = encodeURIComponent(id);
  const [card, representation] = await Promise.allSettled([api(`/v3/workspaces/${w}/peers/${p}/card`), api(`/v3/workspaces/${w}/peers/${p}/representation`, { method: "POST", body: {} })]);
  const tags = card.status === "fulfilled" ? card.value.peer_card || [] : [];
  const memory = representation.status === "fulfilled" ? representation.value.representation : `표현을 불러오지 못했습니다: ${representation.reason?.message || "unknown"}`;
  detail.innerHTML = `<div class="detail-hero"><span class="avatar">${initials(id)}</span><div><h2>${esc(id)}</h2><p>${esc(state.workspace)}의 Peer</p></div></div><section class="detail-section"><h3>PEER CARD</h3><div class="card-tags">${tags.length ? tags.map(t => `<span>${esc(t)}</span>`).join("") : `<span>카드 없음</span>`}</div></section><section class="detail-section"><h3>REPRESENTATION</h3><div class="memory-copy">${esc(memory || "표현이 아직 비어 있습니다.")}</div></section><div class="dialog-actions"><button class="primary" data-chat-peer="${esc(id)}">Dialectic으로 질문</button></div>`;
}

function renderSessionList(filter = "") {
  const list = state.sessions.filter(s => s.id.toLowerCase().includes(filter.toLowerCase()));
  $("#session-list").innerHTML = list.map(s => `<button class="entity-row ${state.selectedSession === s.id ? "active" : ""}" data-session="${esc(s.id)}"><span class="avatar">≋</span><span><strong>${esc(s.id)}</strong><small>${s.is_active ? "ACTIVE" : "INACTIVE"} · ${shortDate(s.created_at)}</small></span><small>→</small></button>`).join("") || `<div class="empty-inline">세션이 없습니다.</div>`;
}

async function selectSession(id) {
  state.selectedSession = id; renderSessionList($("#session-filter").value); const detail = $("#session-detail");
  detail.innerHTML = `<div class="empty-inline">메시지를 불러오는 중…</div>`;
  try {
    const page = await api(`/v3/workspaces/${encodeURIComponent(state.workspace)}/sessions/${encodeURIComponent(id)}/messages/list?reverse=false&page=1&size=100`, { method: "POST", body: {} });
    const messages = items(page); const session = state.sessions.find(s => s.id === id);
    detail.innerHTML = `<div class="detail-hero"><span class="avatar">≋</span><div><h2>${esc(id)}</h2><p>${messages.length} messages · ${session?.is_active ? "active" : "inactive"}</p></div></div><div class="message-list">${messages.map(m => `<article class="message"><div class="message-head"><strong>${esc(m.peer_id)}</strong><span>${shortDate(m.created_at)} · ${m.token_count} tokens</span></div><p>${esc(m.content)}</p></article>`).join("") || `<div class="empty-inline">메시지가 없습니다.</div>`}</div><form class="message-compose" data-message-form><select name="peer">${state.peers.map(p => `<option>${esc(p.id)}</option>`).join("")}</select><input name="content" placeholder="새 메시지를 기록…" required><button class="primary">추가</button></form>`;
  } catch (error) { detail.innerHTML = `<div class="empty-inline">${esc(error.message)}</div>`; }
}

function renderConclusions() {
  $("#conclusion-total").textContent = `최근 ${state.conclusions.length} / 전체 ${state.totals.conclusions.toLocaleString()}개`;
  $("#conclusion-list").innerHTML = state.conclusions.map(c => `<article class="result-card"><p>${esc(c.content)}</p><footer>${esc(c.observer_id)} → ${esc(c.observed_id)}${c.session_id ? ` · ${esc(c.session_id)}` : ""} · ${shortDate(c.created_at)}</footer></article>`).join("") || `<div class="empty-inline">결론이 없습니다.</div>`;
}

function fillSelectors() {
  $("#chat-peer").innerHTML = state.peers.map(p => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join("");
  $("#chat-focus").innerHTML = state.peers.filter(p => !p.id.startsWith("automation_")).map(p => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join("");
  $("#chat-session").innerHTML = `<option value="">전체 기억</option>${state.sessions.map(s => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("")}`;
  const defaults = new Set(["user_chen", "assistant_codex", "assistant_claude", "assistant_gemini"]);
  $("#observer-peers").innerHTML = state.peers.filter(p => !p.id.startsWith("automation_")).map(p => `<label><input type="checkbox" value="${esc(p.id)}" ${defaults.has(p.id) ? "checked" : ""}><span>${esc(p.id)}</span></label>`).join("");
  updateObserverCount();
}

async function searchMemory(query) {
  const box = $("#search-results"); box.innerHTML = `<div class="empty-inline">검색 중…</div>`;
  try { const results = await api(`/v3/workspaces/${encodeURIComponent(state.workspace)}/search`, { method: "POST", body: { query, limit: 30 } }); $("#search-result-count").textContent = `${results.length}개 결과`; box.innerHTML = results.map(m => `<article class="result-card"><p>${esc(m.content)}</p><footer>${esc(m.peer_id)} · ${esc(m.session_id)} · ${shortDate(m.created_at)}</footer></article>`).join("") || `<div class="empty-inline">가까운 메시지를 찾지 못했습니다.</div>`; }
  catch (error) { box.innerHTML = `<div class="empty-inline">${esc(error.message)}</div>`; }
}

async function askDialectic(query) {
  if ($("#chat-mode").value === "integrated") return askIntegrated(query);
  const peer = $("#chat-peer").value; if (!peer) return flash("먼저 Peer를 선택하세요.", true);
  $(".chat-welcome")?.remove(); const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="bubble user">${esc(query)}</div><div class="bubble honcho loading">기억을 탐색하는 중…</div>`); log.scrollTop = log.scrollHeight;
  const loading = $(".bubble.loading", log);
  const body = { query, reasoning_level: $("#reasoning-level").value, stream: false }; if ($("#chat-session").value) body.session_id = $("#chat-session").value;
  try { const response = await api(`/v3/workspaces/${encodeURIComponent(state.workspace)}/peers/${encodeURIComponent(peer)}/chat`, { method: "POST", body }); loading.classList.remove("loading"); loading.textContent = response.content || "응답이 비어 있습니다."; }
  catch (error) { loading.classList.remove("loading"); loading.textContent = `오류: ${error.message}`; }
  log.scrollTop = log.scrollHeight;
}

async function askIntegrated(query) {
  const focus = $("#chat-focus").value;
  const observers = $$("#observer-peers input:checked").map(input => input.value);
  if (!focus || !observers.length) return flash("Focus와 Observer Peer를 선택하세요.", true);
  $(".chat-welcome")?.remove();
  const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="bubble user">${esc(query)}</div><div class="bubble honcho loading"><span class="bubble-label">PEER 통합</span>${observers.length}개 관점을 병렬로 탐색하는 중…</div>`);
  log.scrollTop = log.scrollHeight;
  const loading = $(".bubble.loading", log);
  const reasoning_level = $("#reasoning-level").value;
  const session_id = $("#chat-session").value || undefined;
  const responses = await Promise.allSettled(observers.map(observer => api(`/v3/workspaces/${encodeURIComponent(state.workspace)}/peers/${encodeURIComponent(observer)}/chat`, {
    method: "POST", body: { query, target: focus, reasoning_level, stream: false, ...(session_id ? { session_id } : {}) },
  })));
  const perspectives = responses.map((result, index) => ({ observer: observers[index], content: result.status === "fulfilled" ? result.value.content : `오류: ${result.reason?.message || "응답 실패"}`, ok: result.status === "fulfilled" }));
  const successful = perspectives.filter(p => p.ok && p.content);
  loading.remove();
  log.insertAdjacentHTML("beforeend", `<details class="perspectives"><summary>${successful.length}/${observers.length}개 Peer 관점 보기</summary>${perspectives.map(p => `<div class="perspective"><strong>${esc(p.observer)} → ${esc(focus)}</strong>${esc(p.content || "응답 없음")}</div>`).join("")}</details>`);
  if (!successful.length) {
    log.insertAdjacentHTML("beforeend", `<div class="bubble honcho">모든 Peer 호출이 실패했습니다.</div>`);
    return;
  }
  const perPeerBudget = Math.max(120, Math.floor(8200 / successful.length));
  const evidence = successful.map(p => `[${p.observer}의 관점]\n${p.content.slice(0, perPeerBudget)}`).join("\n\n");
  const synthesisQuery = `원래 질문: ${query}\n\n아래는 여러 Peer가 ${focus}에 관해 답한 내용이다. 공통점과 차이를 판단해 중복 없이 하나의 직접적인 한국어 답변으로 종합하라. 근거가 충돌하면 그 불확실성을 짧게 밝혀라.\n\n${evidence}`.slice(0, 9800);
  log.insertAdjacentHTML("beforeend", `<div class="bubble honcho loading"><span class="bubble-label">${esc(focus)} SYNTHESIS</span>관점을 하나의 답으로 종합하는 중…</div>`);
  const synthesisLoading = $(".bubble.loading", log); log.scrollTop = log.scrollHeight;
  try {
    const synthesis = await api(`/v3/workspaces/${encodeURIComponent(state.workspace)}/peers/${encodeURIComponent(focus)}/chat`, { method: "POST", body: { query: synthesisQuery, reasoning_level, stream: false } });
    synthesisLoading.classList.remove("loading"); synthesisLoading.innerHTML = `<span class="bubble-label">${esc(focus)} · 통합 답변</span>${esc(synthesis.content || "응답이 비어 있습니다.")}`;
  } catch (error) {
    synthesisLoading.classList.remove("loading"); synthesisLoading.innerHTML = `<span class="bubble-label">통합 실패</span>${esc(error.message)}`;
  }
  log.scrollTop = log.scrollHeight;
}

function showView(view) {
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === view)); $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${view}`));
  $("#page-eyebrow").textContent = titles[view][0]; $("#page-title").textContent = titles[view][1];
  if (view === "mcp") loadMcpTools();
  if (view === "audit") loadAudit();
}

function openCreate(type) {
  const dialog = $("#create-dialog"), fields = $("#create-fields"); dialog.dataset.type = type;
  if (type === "peer") { $("#create-title").textContent = "새 Peer"; fields.innerHTML = `<label>PEER ID<input name="id" required pattern="[a-zA-Z0-9_-]+" placeholder="user_chen"></label>`; }
  else { $("#create-title").textContent = "새 Session"; fields.innerHTML = `<label>SESSION ID<input name="id" required pattern="[a-zA-Z0-9_-]+" placeholder="session-001"></label><label>초기 PEERS<select name="peers" multiple size="5">${state.peers.map(p => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join("")}</select></label>`; }
  dialog.showModal();
}

function bindEvents() {
  $("#nav").addEventListener("click", e => { const button = e.target.closest("[data-view]"); if (button) showView(button.dataset.view); });
  document.addEventListener("click", e => {
    const go = e.target.closest("[data-go]"); if (go) showView(go.dataset.go);
    const peer = e.target.closest("[data-peer]"); if (peer) selectPeer(peer.dataset.peer).catch(err => flash(err.message, true));
    const session = e.target.closest("[data-session]"); if (session) { showView("sessions"); selectSession(session.dataset.session).catch(err => flash(err.message, true)); }
    const chatPeer = e.target.closest("[data-chat-peer]"); if (chatPeer) { showView("dialectic"); $("#chat-peer").value = chatPeer.dataset.chatPeer; }
    const dialog = e.target.closest("[data-dialog]"); if (dialog) openCreate(dialog.dataset.dialog);
    if (e.target.closest("[data-close-dialog]")) $("#create-dialog").close();
  });
  $("#workspace-select").addEventListener("change", async e => { state.workspace = e.target.value; state.selectedPeer = state.selectedSession = null; await loadWorkspace(); });
  $("#refresh-button").addEventListener("click", () => {
    const view = $(".nav-item.active")?.dataset.view;
    if (view === "mcp") return loadMcpTools();
    if (view === "audit") return loadAudit();
    return loadWorkspace();
  });
  $("#audit-filter-form").addEventListener("submit", e => { e.preventDefault(); loadAudit(); });
  $("#peer-filter").addEventListener("input", e => renderPeerList(e.target.value)); $("#session-filter").addEventListener("input", e => renderSessionList(e.target.value));
  $("#mcp-tool-list").addEventListener("change", async e => {
    if (!e.target.matches("[data-mcp-tool]")) return;
    const input = e.target, desired = input.checked, name = input.dataset.mcpTool;
    input.disabled = true;
    try {
      state.mcpTools = await api("/dashboard/mcp/tools", { method: "POST", body: { name, enabled: desired } });
      renderMcpTools();
      flash(`${name} 도구를 ${desired ? "켰습니다" : "껐습니다"}.`);
    } catch (error) {
      flash(error.message, true);
      await loadMcpTools();
    }
  });
  $("#memory-search-form").addEventListener("submit", e => { e.preventDefault(); const q = $("#memory-query").value.trim(); if (q) searchMemory(q); });
  $("#chat-form").addEventListener("submit", async e => {
    e.preventDefault();
    if (state.chatBusy) return;
    const input = $("#chat-query"), q = input.value.trim();
    if (!q) return;
    input.value = "";
    setChatBusy(true);
    try { await askDialectic(q); } finally { setChatBusy(false); }
  });
  $("#chat-query").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); } });
  $("#clear-chat").addEventListener("click", () => $("#chat-log").innerHTML = `<div class="chat-welcome"><span>✦</span><p>Dialectic은 선택한 Peer의 기억을 바탕으로 답합니다.</p></div>`);
  $("#chat-mode").addEventListener("change", e => {
    const integrated = e.target.value === "integrated";
    $$(".single-control").forEach(el => el.hidden = integrated);
    $$(".integrated-control").forEach(el => el.hidden = !integrated);
    $("#integrated-settings").hidden = !integrated;
  });
  $("#observer-peers").addEventListener("change", e => {
    if (!e.target.matches("input")) return;
    updateObserverCount();
  });
  $$(".prompt-chips button").forEach(b => b.addEventListener("click", () => { $("#chat-query").value = b.textContent; $("#chat-form").requestSubmit(); }));
  $("#create-form").addEventListener("submit", async e => { e.preventDefault(); const type = $("#create-dialog").dataset.type, data = new FormData(e.target), id = data.get("id"); const w = encodeURIComponent(state.workspace); try { if (type === "peer") await api(`/v3/workspaces/${w}/peers`, { method: "POST", body: { id } }); else { const peers = Object.fromEntries(data.getAll("peers").map(p => [p, {}])); await api(`/v3/workspaces/${w}/sessions`, { method: "POST", body: { id, peers } }); } $("#create-dialog").close(); flash(`${id} 생성 완료`); await loadWorkspace(); } catch (error) { flash(error.message, true); } });
  $("#session-detail").addEventListener("submit", async e => { const form = e.target.closest("[data-message-form]"); if (!form) return; e.preventDefault(); const data = new FormData(form); try { await api(`/v3/workspaces/${encodeURIComponent(state.workspace)}/sessions/${encodeURIComponent(state.selectedSession)}/messages`, { method: "POST", body: { messages: [{ peer_id: data.get("peer"), content: data.get("content") }] } }); flash("메시지를 기록했습니다."); await selectSession(state.selectedSession); } catch (error) { flash(error.message, true); } });
}

init().catch(error => { setConnection("offline", "초기화 실패"); flash(error.message, true); });
