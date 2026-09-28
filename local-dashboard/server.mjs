import { createServer } from "node:http";
import { createReadStream, existsSync, promises as fs } from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "public");
const host = process.env.DASHBOARD_HOST || "127.0.0.1";
const port = Number(process.env.DASHBOARD_PORT || 4173);
const honchoUrl = (process.env.HONCHO_URL || "http://127.0.0.1:8001").replace(/\/$/, "");
const serverApiKey = process.env.HONCHO_API_KEY || "";
const execFileAsync = promisify(execFile);
const launchDomain = `gui/${process.getuid()}`;
const mcpDryRun = process.env.MCP_CONTROL_DRY_RUN === "1";
const mcpControlMode = process.env.MCP_CONTROL_MODE || "launchd";
const allowRemoteMcpControl = process.env.MCP_CONTROL_ALLOW_REMOTE === "1";
let dryRunEnabled = true;
let dryRunDisabledTools = new Set();
let mcpToolConfigQueue = Promise.resolve();
const mcpToolConfigPath = process.env.HONCHO_MCP_TOOL_CONFIG || join(process.env.HOME, ".config/honcho/mcp-bridge/tool-config.json");
// The audit log lives in the bridge's database schema, and the bridge is the only
// thing holding a driver for it. The dashboard reads it over the bridge's guarded
// /audit route rather than opening a second connection of its own.
const auditUrl = (process.env.HONCHO_MCP_AUDIT_URL || "").replace(/\/$/, "");
const auditTokenFile = process.env.HONCHO_MCP_BEARER_TOKEN_FILE || "";
const auditTokenInline = process.env.HONCHO_MCP_BEARER_TOKEN || "";
const auditFilters = ["limit", "caller", "tool", "status", "bridge", "hours"];
const mcpTools = [
  { name: "server_info", group: "상태", description: "MCP 브리지의 주소, 기본 Workspace·Peer, 읽기 전용 여부와 Honcho 연결 상태를 한 번에 확인합니다.", use_case: "연결 문제 진단이나 에이전트의 기본 조회 범위를 확인할 때", off_impact: "에이전트가 브리지 설정과 상태를 스스로 진단할 수 없습니다." },
  { name: "get_queue_status", group: "상태", description: "메시지에서 사실과 추론을 만드는 Deriver 작업의 완료·진행·대기 수를 조회합니다.", use_case: "새 기억이 아직 처리 중인지, 추론 생성이 밀렸는지 확인할 때", off_impact: "에이전트가 기억 처리 완료 여부를 확인할 수 없습니다." },
  { name: "inspect_workspace", group: "Workspace", description: "지정한 Workspace의 생성 정보, 설정과 metadata를 포함한 상세 항목을 조회합니다.", use_case: "현재 기억 저장소의 정확한 설정과 식별자를 점검할 때", off_impact: "Workspace 상세 진단이 불가능해집니다." },
  { name: "list_workspaces", group: "Workspace", description: "Honcho에 존재하는 Workspace를 필터와 함께 나열해 사용할 기억 저장소를 찾습니다.", use_case: "여러 프로젝트나 사용자 기억 중 조회 대상을 선택할 때", off_impact: "에이전트가 사용 가능한 Workspace를 탐색할 수 없습니다." },
  { name: "search", group: "기억", description: "대화 원문을 의미 기반으로 검색합니다. Workspace 전체 또는 특정 Peer·Session 범위로 좁힐 수 있습니다.", use_case: "과거 발언, 결정의 근거, 정확한 대화 맥락을 넓게 찾을 때", off_impact: "원문 기억을 자율적으로 탐색하는 핵심 경로가 사라집니다." },
  { name: "get_metadata", group: "기억", description: "Workspace, Peer 또는 Session에 붙은 구조화 metadata를 범위별로 조회합니다.", use_case: "태그, 외부 식별자, 애플리케이션별 부가 정보를 확인할 때", off_impact: "구조화된 부가 정보를 읽을 수 없습니다." },
  { name: "set_metadata", group: "기억", access: "write", description: "Workspace, Peer 또는 Session의 metadata와 일부 configuration을 새 값으로 갱신합니다.", use_case: "태그, 외부 식별자나 애플리케이션 설정을 에이전트가 직접 관리할 때", off_impact: "에이전트가 metadata를 읽을 수는 있지만 수정할 수 없습니다." },
  { name: "create_peer", group: "Peer", access: "write", description: "현재 Workspace에 새로운 사람·에이전트·프로젝트 등의 Peer를 생성합니다.", use_case: "새로운 기억 주체를 등록하고 Session에 참여시키기 전에", off_impact: "에이전트가 새 Peer를 만들 수 없습니다." },
  { name: "list_peers", group: "Peer", description: "Workspace 안의 사용자·에이전트 등 모든 Peer를 나열하고 대상의 존재 여부를 확인합니다.", use_case: "누구의 관점이나 기억을 조회할지 결정할 때", off_impact: "에이전트가 사용 가능한 Peer를 탐색할 수 없습니다." },
  { name: "chat", group: "Peer", access: "llm", description: "Dialectic LLM이 축적된 Conclusion과 문맥을 탐색해 특정 Peer에 관한 자연어 답변을 생성합니다.", use_case: "검색 결과를 직접 조립하지 않고 Honcho의 추론형 답변이 필요할 때", off_impact: "에이전트가 Dialectic 답변을 요청할 수 없습니다." },
  { name: "get_peer_card", group: "Peer", description: "Peer를 설명하는 짧고 구조화된 특성 카드와 핵심 속성을 조회합니다.", use_case: "대상 인물의 성향과 핵심 특성을 빠르게 파악할 때", off_impact: "간결한 Peer 요약을 사용할 수 없습니다." },
  { name: "set_peer_card", group: "Peer", access: "write", description: "Peer의 핵심 특성을 담는 Peer card를 직접 지정하거나 기존 카드 내용을 교체합니다.", use_case: "자동 생성 결과 대신 명시적인 인물 요약을 저장할 때", off_impact: "Peer card 조회만 가능하고 수정은 할 수 없습니다." },
  { name: "get_peer_context", group: "Peer", description: "Representation과 Peer card를 묶어 LLM이 바로 사용할 수 있는 대상 중심 문맥을 만듭니다.", use_case: "한 번의 호출로 개인화된 답변 문맥을 확보할 때", off_impact: "에이전트가 Peer 통합 문맥을 직접 조립해야 합니다." },
  { name: "get_representation", group: "Peer", description: "관찰자가 특정 Peer를 어떻게 이해하는지 축적된 사실·추론을 바탕으로 서술형 표현을 생성합니다.", use_case: "사용자의 성향, 관심사, 관계나 변화에 관해 답할 때", off_impact: "Honcho의 고차원 인물 이해를 직접 조회할 수 없습니다." },
  { name: "create_session", group: "Session", access: "write", description: "참여 Peer와 관찰 관계를 지정해 새로운 대화 Session을 생성합니다.", use_case: "새 대화 흐름이나 문서 수집 단위를 시작할 때", off_impact: "에이전트가 새 Session을 만들 수 없습니다." },
  { name: "list_sessions", group: "Session", description: "Workspace의 대화 Session을 필터와 페이지 단위로 나열합니다.", use_case: "관련 대화 묶음을 찾거나 최근 Session을 탐색할 때", off_impact: "에이전트가 Session 목록을 탐색할 수 없습니다." },
  { name: "delete_session", group: "Session", access: "danger", description: "지정한 Session과 그 Session에 속한 기록을 Honcho에서 삭제합니다.", use_case: "잘못 만든 대화 묶음이나 보존할 필요가 없는 기록을 정리할 때", off_impact: "에이전트가 Session을 삭제할 수 없습니다." },
  { name: "clone_session", group: "Session", access: "write", description: "기존 Session의 참여 구조와 메시지를 바탕으로 별도의 새 Session 사본을 만듭니다.", use_case: "원본을 보존한 채 분기된 실험이나 문맥을 만들 때", off_impact: "에이전트가 Session을 복제할 수 없습니다." },
  { name: "add_peers_to_session", group: "Session", access: "write", description: "기존 Session에 Peer를 추가하고 서로를 관찰할 수 있는 범위를 설정합니다.", use_case: "대화 도중 새 사용자나 에이전트를 참여시킬 때", off_impact: "Session 참여자를 추가할 수 없습니다." },
  { name: "remove_peers_from_session", group: "Session", access: "write", description: "기존 Session에서 지정한 Peer의 참여 관계를 제거합니다.", use_case: "더 이상 해당 대화 문맥에 포함할 필요가 없는 Peer를 분리할 때", off_impact: "Session 참여자를 제거할 수 없습니다." },
  { name: "get_session_peers", group: "Session", description: "특정 Session에 참여한 Peer와 상호 관찰 설정을 조회합니다.", use_case: "대화 참여자와 각 관점의 관계를 이해할 때", off_impact: "Session 참여 구조를 확인할 수 없습니다." },
  { name: "inspect_session", group: "Session", description: "Session의 활성 상태, 설정, metadata 등 대화 묶음의 상세 정보를 조회합니다.", use_case: "Session 범위와 상태를 정확히 진단할 때", off_impact: "Session 상세 상태를 읽을 수 없습니다." },
  { name: "add_messages_to_session", group: "Session", access: "write", description: "하나 이상의 메시지를 Peer, 시각, metadata와 함께 Session에 기록하고 기억 처리를 시작합니다.", use_case: "새 대화, 이벤트, 문서 조각을 장기 기억으로 저장할 때", off_impact: "에이전트가 Honcho에 새 기억을 기록할 수 없습니다." },
  { name: "get_session_messages", group: "Session", description: "특정 Session의 메시지를 순서·페이지 조건과 함께 조회해 전체 대화 흐름을 복원합니다.", use_case: "긴 대화를 시간순으로 읽거나 여러 메시지를 비교할 때", off_impact: "Session 단위 대화 원문을 묶어서 읽을 수 없습니다." },
  { name: "get_session_message", group: "Session", description: "메시지 ID로 원문 한 건과 작성 Peer, 생성 시각 등 정확한 기록을 조회합니다.", use_case: "검색 결과의 특정 발언을 정밀하게 검증할 때", off_impact: "개별 메시지의 정확한 원문 확인이 제한됩니다." },
  { name: "get_session_context", group: "Session", description: "메시지와 선택한 Peer의 표현·카드를 결합해 LLM 입력용 Session 문맥을 구성합니다.", use_case: "특정 대화에 근거한 답변을 한 번의 호출로 준비할 때", off_impact: "Session 기반 답변 문맥을 자동 구성할 수 없습니다." },
  { name: "list_conclusions", group: "Conclusion", description: "Deriver가 대화에서 추출한 사실과 추론을 관찰자·대상·Session 조건으로 나열합니다.", use_case: "Honcho가 이미 알고 있는 명시적 사실과 판단을 검토할 때", off_impact: "도출된 기억을 목록 형태로 읽을 수 없습니다." },
  { name: "query_conclusions", group: "Conclusion", description: "도출된 Conclusion만을 의미 검색해 원문 검색보다 압축된 고수준 기억을 찾습니다.", use_case: "반복 패턴, 성향, 결정 같은 추론 중심 질문에 답할 때", off_impact: "고수준 기억을 의미 기반으로 탐색할 수 없습니다." },
  { name: "create_conclusions", group: "Conclusion", access: "write", description: "관찰자와 대상 Peer 사이의 사실·추론을 Conclusion으로 직접 저장합니다.", use_case: "Deriver를 기다리지 않고 검증된 인사이트를 명시적으로 기억시킬 때", off_impact: "에이전트가 Conclusion을 직접 추가할 수 없습니다." },
  { name: "delete_conclusion", group: "Conclusion", access: "danger", description: "Conclusion ID로 특정 사실이나 추론 기록을 영구적으로 삭제합니다.", use_case: "틀렸거나 더 이상 유지하면 안 되는 파생 기억을 제거할 때", off_impact: "에이전트가 잘못된 Conclusion을 직접 삭제할 수 없습니다." },
  { name: "schedule_dream", group: "상태", access: "llm", description: "축적된 Conclusion을 다시 검토해 더 높은 수준의 패턴과 추론을 만드는 Dream 작업을 예약합니다.", use_case: "새로운 고차 추론이나 장기 패턴 갱신이 필요할 때", off_impact: "에이전트가 Dream 처리를 직접 예약할 수 없습니다." },
];
const mcpToolDescriptions = {
  server_info: ["Inspect local MCP/Honcho bridge defaults and upstream health.", "로컬 MCP/Honcho 브리지의 기본값과 upstream 상태를 확인합니다."],
  inspect_workspace: ["Inspect one workspace by ID.", "ID로 Workspace 하나의 상세 정보를 확인합니다."],
  list_workspaces: ["List all available workspaces.", "사용 가능한 모든 Workspace를 나열합니다."],
  search: ["Search messages at workspace scope, or scope to a peer or session.", "Workspace 전체 또는 특정 Peer·Session 범위에서 메시지를 검색합니다."],
  get_metadata: ["Get metadata for a workspace, peer, or session.", "Workspace, Peer 또는 Session의 metadata를 조회합니다."],
  set_metadata: ["Set metadata for a workspace, peer, or session.", "Workspace, Peer 또는 Session의 metadata를 설정합니다."],
  create_peer: ["Create or get a peer.", "Peer를 생성하거나 기존 Peer를 가져옵니다."],
  list_peers: ["List peers in a workspace.", "Workspace에 속한 Peer를 나열합니다."],
  chat: ["Ask Honcho what it knows about a peer using natural language.", "자연어로 특정 Peer에 관해 Honcho가 알고 있는 내용을 질문합니다."],
  get_peer_card: ["Get a peer card for a target peer, defaulting to the assistant's perspective.", "기본적으로 Assistant 관점에서 대상 Peer의 Peer card를 조회합니다."],
  set_peer_card: ["Set a peer card for a target peer, defaulting to the assistant's perspective.", "기본적으로 Assistant 관점에서 대상 Peer의 Peer card를 설정합니다."],
  get_peer_context: ["Get representation + peer card for a target peer.", "대상 Peer의 Representation과 Peer card를 함께 조회합니다."],
  get_representation: ["Get a textual representation for a target peer.", "대상 Peer에 대한 텍스트 Representation을 조회합니다."],
  create_session: ["Create or get a session.", "Session을 생성하거나 기존 Session을 가져옵니다."],
  list_sessions: ["List sessions in a workspace.", "Workspace에 속한 Session을 나열합니다."],
  delete_session: ["Delete a session.", "Session을 삭제합니다."],
  clone_session: ["Clone a session, optionally cutting off at a specific message ID.", "선택적으로 특정 메시지 ID까지만 포함해 Session을 복제합니다."],
  add_peers_to_session: ["Add peers to a session. Accepts either a peer->config map or a list of peer config objects.", "Peer→설정 map 또는 Peer 설정 객체 목록을 받아 Session에 Peer를 추가합니다."],
  remove_peers_from_session: ["Remove peers from a session.", "Session에서 Peer를 제거합니다."],
  get_session_peers: ["List peers in a session.", "Session에 속한 Peer를 나열합니다."],
  inspect_session: ["Inspect one session by ID.", "ID로 Session 하나의 상세 정보를 확인합니다."],
  add_messages_to_session: ["Add messages to a session. Each message needs peer_id+content, or role=user/assistant+content.", "Session에 메시지를 추가합니다. 각 메시지에는 peer_id+content 또는 role=user/assistant+content가 필요합니다."],
  get_session_messages: ["List messages in a session.", "Session에 속한 메시지를 나열합니다."],
  get_session_message: ["Get one message from a session.", "Session에서 메시지 하나를 조회합니다."],
  get_session_context: ["Get LLM-ready context for a session, optionally including peer representation/card.", "선택적으로 Peer Representation/card를 포함한 LLM용 Session context를 조회합니다."],
  list_conclusions: ["List conclusions in a workspace.", "Workspace에 속한 Conclusion을 나열합니다."],
  query_conclusions: ["Semantic search across derived conclusions.", "도출된 Conclusion 전체에서 의미 검색을 수행합니다."],
  create_conclusions: ["Create one or more conclusions.", "하나 이상의 Conclusion을 생성합니다."],
  delete_conclusion: ["Delete a conclusion by ID.", "ID로 Conclusion을 삭제합니다."],
  schedule_dream: ["Trigger dream/consolidation work for a peer pair.", "Peer 쌍에 대한 Dream/통합 작업을 실행합니다."],
  get_queue_status: ["Inspect the Honcho derivation queue.", "Honcho derivation queue의 상태를 확인합니다."],
};
for (const tool of mcpTools) {
  [tool.description, tool.translation] = mcpToolDescriptions[tool.name];
}
const mcpToolNames = new Set(mcpTools.map(tool => tool.name));
// launchd labels are per-installation, so they come from the environment rather
// than from this file. MCP_CONTROL_MODE=file skips launchd entirely.
const BRIDGE_LABEL = process.env.HONCHO_BRIDGE_LAUNCHD_LABEL || "honcho-external-mcp";
const TUNNEL_LABEL = process.env.HONCHO_TUNNEL_LAUNCHD_LABEL || "cloudflared.honcho-mcp";
const mcpServices = [
  {
    id: "bridge",
    label: BRIDGE_LABEL,
    plist: join(process.env.HOME, `Library/LaunchAgents/${BRIDGE_LABEL}.plist`),
  },
  {
    id: "tunnel",
    label: TUNNEL_LABEL,
    plist: join(process.env.HOME, `Library/LaunchAgents/${TUNNEL_LABEL}.plist`),
  },
];

const mime = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".json": "application/json; charset=utf-8",
};

function json(res, status, body) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 16_384) throw new Error("Request body is too large.");
    chunks.push(chunk);
  }
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
}

function isLoopback(address = "") {
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

function isLoopbackHostname(hostname = "") {
  const value = hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (value === "localhost" || value === "::1") return true;
  if (value.startsWith("::ffff:")) return isLoopbackHostname(value.slice("::ffff:".length));
  const octets = value.split(".");
  return octets.length === 4
    && octets.every(octet => /^\d{1,3}$/.test(octet) && Number(octet) <= 255)
    && Number(octets[0]) === 127;
}

function parseHostHeader(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = new URL(`http://${value.trim()}`);
    if (parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash) return null;
    return { hostname: parsed.hostname, port: parsed.port };
  } catch {
    return null;
  }
}

function validateLocalApiHost(req) {
  const requestHost = parseHostHeader(req.headers.host);
  if (!requestHost || !isLoopbackHostname(requestHost.hostname)) {
    return { status: 403, error: "Dashboard API requires a localhost Host header." };
  }
  return null;
}

function requestHasBody(req) {
  const transferEncoding = req.headers["transfer-encoding"];
  if (typeof transferEncoding === "string" && transferEncoding.trim()) return true;
  const contentLength = req.headers["content-length"];
  if (typeof contentLength !== "string" || !/^\d+$/.test(contentLength.trim())) return false;
  return Number(contentLength) > 0;
}

function validateJsonApiRequest(req, { requireJson = true } = {}) {
  const requestHost = parseHostHeader(req.headers.host);
  if (!requestHost || !isLoopbackHostname(requestHost.hostname)) return validateLocalApiHost(req);
  const originHeader = req.headers.origin;
  if (originHeader !== undefined) {
    if (typeof originHeader !== "string") return { status: 403, error: "Invalid dashboard API Origin." };
    try {
      const origin = new URL(originHeader);
      const isBareOrigin = origin.pathname === "/" && !origin.search && !origin.hash && !origin.username && !origin.password;
      const expectedOrigin = new URL(`http://${req.headers.host}`).origin;
      if (!isBareOrigin || !isLoopbackHostname(origin.hostname) || origin.origin !== expectedOrigin) {
        return { status: 403, error: "Dashboard API only accepts same-origin localhost browser requests." };
      }
    } catch {
      return { status: 403, error: "Invalid dashboard API Origin." };
    }
  }

  if (requireJson && (req.headers["content-type"] || "").split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    return { status: 415, error: "Dashboard API requests must use application/json." };
  }
  return null;
}

function validateMcpControlRequest(req) {
  if (!allowRemoteMcpControl && !isLoopback(req.socket.remoteAddress)) {
    return { status: 403, error: "MCP control is only available from localhost." };
  }
  return validateJsonApiRequest(req);
}

function serializeMcpToolConfig(operation) {
  const result = mcpToolConfigQueue.then(operation, operation);
  mcpToolConfigQueue = result.then(() => undefined, () => undefined);
  return result;
}

async function serviceLoaded(label) {
  if (mcpControlMode === "file") return true;
  try {
    await execFileAsync("launchctl", ["print", `${launchDomain}/${label}`], { timeout: 5_000 });
    return true;
  } catch {
    return false;
  }
}

async function getMcpStatus() {
  if (mcpDryRun) {
    return {
      enabled: dryRunEnabled,
      state: dryRunEnabled ? "running" : "stopped",
      dry_run: true,
      components: { bridge: dryRunEnabled, tunnel: dryRunEnabled },
    };
  }
  if (mcpControlMode === "file") {
    return {
      enabled: true,
      state: "host-managed",
      dry_run: false,
      components: { bridge: true, tunnel: false },
    };
  }
  const values = await Promise.all(mcpServices.map(async service => [service.id, await serviceLoaded(service.label)]));
  const components = Object.fromEntries(values);
  const enabled = Object.values(components).every(Boolean);
  const anyRunning = Object.values(components).some(Boolean);
  return { enabled, state: enabled ? "running" : anyRunning ? "partial" : "stopped", dry_run: false, components };
}

async function readDisabledToolsUnlocked() {
  if (mcpDryRun) return new Set(dryRunDisabledTools);
  try {
    const payload = JSON.parse(await fs.readFile(mcpToolConfigPath, "utf8"));
    return new Set(Array.isArray(payload.disabled_tools) ? payload.disabled_tools.filter(name => mcpToolNames.has(name)) : []);
  } catch (error) {
    if (error.code === "ENOENT") return new Set();
    throw error;
  }
}

async function readDisabledTools() {
  return serializeMcpToolConfig(readDisabledToolsUnlocked);
}

async function getMcpTools() {
  const disabled = await readDisabledTools();
  const bridge = mcpDryRun || mcpControlMode === "file"
    ? true
    : await serviceLoaded(BRIDGE_LABEL);
  return {
    bridge_running: bridge,
    dry_run: mcpDryRun,
    enabled_count: mcpTools.length - disabled.size,
    total_count: mcpTools.length,
    tools: mcpTools.map(tool => ({ ...tool, enabled: !disabled.has(tool.name) })),
  };
}

async function setMcpToolEnabled(name, enabled) {
  if (!mcpToolNames.has(name)) throw new Error(`Unknown MCP tool: ${name}`);
  await serializeMcpToolConfig(async () => {
    const disabled = await readDisabledToolsUnlocked();
    if (enabled) disabled.delete(name); else disabled.add(name);
    if (mcpDryRun) {
      dryRunDisabledTools = disabled;
    } else {
      await fs.mkdir(dirname(mcpToolConfigPath), { recursive: true, mode: 0o700 });
      const tempPath = `${mcpToolConfigPath}.${process.pid}.${randomUUID()}.tmp`;
      try {
        await fs.writeFile(tempPath, `${JSON.stringify({ version: 1, disabled_tools: [...disabled].sort() }, null, 2)}\n`, { mode: 0o600 });
        await fs.rename(tempPath, mcpToolConfigPath);
      } finally {
        await fs.rm(tempPath, { force: true });
      }
      if (mcpControlMode === "launchd" && await serviceLoaded(BRIDGE_LABEL)) {
        await execFileAsync("launchctl", ["kill", "SIGTERM", `${launchDomain}/${BRIDGE_LABEL}`], { timeout: 5_000 });
      }
    }
  });
  return getMcpTools();
}

async function setMcpEnabled(enabled) {
  if (mcpDryRun) {
    dryRunEnabled = enabled;
    return getMcpStatus();
  }
  if (mcpControlMode === "file") {
    throw new Error("The MCP process is managed by the agent host in this deployment.");
  }
  if (enabled) {
    for (const service of mcpServices) {
      if (!existsSync(service.plist)) throw new Error(`Missing launch agent: ${service.plist}`);
      if (!(await serviceLoaded(service.label))) {
        await execFileAsync("launchctl", ["bootstrap", launchDomain, service.plist], { timeout: 10_000 });
      }
    }
  } else {
    for (const service of [...mcpServices].reverse()) {
      if (await serviceLoaded(service.label)) {
        await execFileAsync("launchctl", ["bootout", `${launchDomain}/${service.label}`], { timeout: 10_000 });
      }
    }
  }
  return getMcpStatus();
}

async function auditToken() {
  if (auditTokenInline) return auditTokenInline;
  if (!auditTokenFile) return "";
  return (await fs.readFile(auditTokenFile, "utf8")).trim();
}

async function readAudit(req, res) {
  if (!auditUrl) {
    return json(res, 200, { enabled: false, reason: "HONCHO_MCP_AUDIT_URL is not set." });
  }
  const requested = new URL(req.url, "http://dashboard").searchParams;
  const params = new URLSearchParams();
  for (const name of auditFilters) {
    const value = requested.get(name);
    if (value) params.set(name, value);
  }
  try {
    const token = await auditToken();
    const upstream = await fetch(`${auditUrl}?${params}`, {
      headers: token ? { authorization: `Bearer ${token}` } : {},
      signal: AbortSignal.timeout(30_000),
    });
    const payload = await upstream.json().catch(() => ({ error: "Audit response was not JSON." }));
    return json(res, upstream.status, upstream.ok ? { enabled: true, ...payload } : payload);
  } catch (error) {
    return json(res, 502, { error: "Audit log is unreachable.", detail: error.message, audit_url: auditUrl });
  }
}

async function proxy(req, res) {
  const path = req.url.slice("/api".length);
  if (!path.startsWith("/v3/")) return json(res, 400, { error: "Only Honcho v3 routes are allowed." });

  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = chunks.length ? Buffer.concat(chunks) : undefined;
  const headers = { accept: req.headers.accept || "application/json" };
  if (body) headers["content-type"] = req.headers["content-type"] || "application/json";
  const apiKey = req.headers["x-honcho-api-key"] || serverApiKey;
  if (apiKey) headers.authorization = `Bearer ${apiKey}`;

  try {
    const upstream = await fetch(`${honchoUrl}${path}`, {
      method: req.method,
      headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : body,
      signal: AbortSignal.timeout(120_000),
    });
    const responseHeaders = { "content-type": upstream.headers.get("content-type") || "application/json" };
    res.writeHead(upstream.status, responseHeaders);
    if (upstream.body) {
      for await (const chunk of upstream.body) res.write(chunk);
    }
    res.end();
  } catch (error) {
    json(res, 502, { error: "Honcho server is unreachable.", detail: error.message, honcho_url: honchoUrl });
  }
}

function staticFile(req, res) {
  const requestPath = req.url.split("?")[0] === "/" ? "/index.html" : req.url.split("?")[0];
  const safePath = normalize(requestPath).replace(/^(\.\.[/\\])+/, "");
  let file = join(root, safePath);
  if (!file.startsWith(root) || !existsSync(file)) file = join(root, "index.html");
  res.writeHead(200, {
    "content-type": mime[extname(file)] || "application/octet-stream",
    "cache-control": "no-store",
  });
  createReadStream(file).pipe(res);
}

createServer(async (req, res) => {
  if (req.url.startsWith("/api/")) {
    const hostRejection = validateLocalApiHost(req);
    if (hostRejection) return json(res, hostRejection.status, { error: hostRejection.error });
    if (!["GET", "HEAD"].includes(req.method)) {
      const requestRejection = validateJsonApiRequest(req, {
        // POST is a CORS-safelisted method, so requiring JSON prevents form
        // CSRF even when it has an empty body. Bodyless DELETE/PUT/PATCH are
        // already preflighted by browsers and should remain valid Honcho API
        // proxy requests.
        requireJson: req.method === "POST" || requestHasBody(req),
      });
      if (requestRejection) return json(res, requestRejection.status, { error: requestRejection.error });
    }
  }
  if (req.url === "/api/dashboard/config") {
    return json(res, 200, { honcho_url: honchoUrl, has_server_api_key: Boolean(serverApiKey), has_audit_log: Boolean(auditUrl) });
  }
  if (req.url.startsWith("/api/dashboard/audit") && req.method === "GET") {
    return readAudit(req, res);
  }
  if (req.url === "/api/dashboard/mcp" && req.method === "GET") {
    return json(res, 200, await getMcpStatus());
  }
  if (req.url === "/api/dashboard/mcp/tools" && req.method === "GET") {
    try {
      return json(res, 200, await getMcpTools());
    } catch (error) {
      return json(res, 500, { error: "Failed to read MCP tool settings.", detail: error.message });
    }
  }
  if (req.url === "/api/dashboard/mcp/tools" && req.method === "POST") {
    const rejection = validateMcpControlRequest(req);
    if (rejection) return json(res, rejection.status, { error: rejection.error });
    try {
      const body = await readJson(req);
      if (typeof body.name !== "string" || typeof body.enabled !== "boolean") return json(res, 400, { error: "name and enabled are required." });
      return json(res, 200, await setMcpToolEnabled(body.name, body.enabled));
    } catch (error) {
      return json(res, 500, { error: "Failed to change MCP tool state.", detail: error.message });
    }
  }
  if (req.url === "/api/dashboard/mcp" && req.method === "POST") {
    const rejection = validateMcpControlRequest(req);
    if (rejection) return json(res, rejection.status, { error: rejection.error });
    try {
      const body = await readJson(req);
      if (typeof body.enabled !== "boolean") return json(res, 400, { error: "enabled must be a boolean." });
      return json(res, 200, await setMcpEnabled(body.enabled));
    } catch (error) {
      return json(res, 500, { error: "Failed to change MCP service state.", detail: error.message });
    }
  }
  if (req.url.startsWith("/api/v3/")) return proxy(req, res);
  return staticFile(req, res);
}).listen(port, host, () => {
  console.log(`Honcho dashboard: http://${host}:${port}`);
  console.log(`Honcho API: ${honchoUrl}`);
});
