// The dashboard does not hold a database driver. It reads the audit log through
// the bridge's guarded /audit route, so these tests stand in a fake bridge and
// check what the dashboard forwards, hides and reports.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import { request as httpRequest, createServer as createHttpServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";

const dashboardRoot = dirname(fileURLToPath(import.meta.url));
const TOKEN = "bridge-token-for-tests";

let dashboard;
let output = "";
let port;
let bridge;
let bridgePort;
let tempRoot;
let tokenPath;
let seen = [];
let bridgeReply = { status: 200, body: { rows: [], summary: {}, schema: "honcho_audit" } };

async function availablePort() {
  const server = createHttpServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const selected = server.address().port;
  await new Promise((resolve, reject) => server.close(error => (error ? reject(error) : resolve())));
  return selected;
}

function get(path) {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      { hostname: "127.0.0.1", port, path, method: "GET", headers: { host: `127.0.0.1:${port}` } },
      res => {
        const chunks = [];
        res.on("data", chunk => chunks.push(chunk));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          resolve({ status: res.statusCode, body: text ? JSON.parse(text) : null });
        });
      },
    );
    req.on("error", reject);
    req.end();
  });
}

async function startDashboard(extraEnv) {
  port = await availablePort();
  output = "";
  dashboard = spawn(process.execPath, ["server.mjs"], {
    cwd: dashboardRoot,
    env: {
      ...process.env,
      DASHBOARD_HOST: "127.0.0.1",
      DASHBOARD_PORT: String(port),
      HONCHO_URL: "http://127.0.0.1:1",
      HONCHO_MCP_TOOL_CONFIG: join(tempRoot, "tool-config.json"),
      MCP_CONTROL_MODE: "file",
      HONCHO_MCP_BEARER_TOKEN: "",
      HONCHO_MCP_AUDIT_URL: "",
      HONCHO_MCP_BEARER_TOKEN_FILE: "",
      ...extraEnv,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  dashboard.stdout.on("data", chunk => (output += chunk));
  dashboard.stderr.on("data", chunk => (output += chunk));
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Dashboard did not start.\n${output}`)), 5_000);
    const check = chunk => {
      if (!chunk.toString().includes("Honcho dashboard:")) return;
      clearTimeout(timeout);
      dashboard.stdout.off("data", check);
      resolve();
    };
    dashboard.stdout.on("data", check);
  });
}

async function stopDashboard() {
  if (!dashboard || dashboard.exitCode !== null) return;
  dashboard.kill("SIGTERM");
  await new Promise(resolve => {
    const timeout = setTimeout(resolve, 2_000);
    dashboard.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

before(async () => {
  tempRoot = await fs.mkdtemp(join(tmpdir(), "honcho-audit-test-"));
  tokenPath = join(tempRoot, "bearer-token");
  await fs.writeFile(tokenPath, `${TOKEN}\n`);

  bridgePort = await availablePort();
  bridge = createHttpServer((req, res) => {
    seen.push({ url: req.url, authorization: req.headers.authorization || "" });
    const payload = JSON.stringify(bridgeReply.body);
    res.writeHead(bridgeReply.status, {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(payload),
    });
    res.end(payload);
  });
  await new Promise((resolve, reject) => {
    bridge.once("error", reject);
    bridge.listen(bridgePort, "127.0.0.1", resolve);
  });
});

after(async () => {
  await stopDashboard();
  if (bridge) await new Promise(resolve => bridge.close(() => resolve()));
  if (tempRoot) await fs.rm(tempRoot, { recursive: true, force: true });
});

test("without an audit URL the dashboard says so instead of failing", async () => {
  await startDashboard({});
  try {
    const config = await get("/api/dashboard/config");
    assert.equal(config.body.has_audit_log, false, "the UI hides the tab when there is no log");

    const audit = await get("/api/dashboard/audit");
    assert.equal(audit.status, 200);
    assert.equal(audit.body.enabled, false);
    assert.match(audit.body.reason, /HONCHO_MCP_AUDIT_URL/);
  } finally {
    await stopDashboard();
  }
});

test("the audit read is forwarded with the bearer token and only the known filters", async () => {
  await startDashboard({
    HONCHO_MCP_AUDIT_URL: `http://127.0.0.1:${bridgePort}/audit`,
    HONCHO_MCP_BEARER_TOKEN_FILE: tokenPath,
  });
  try {
    seen = [];
    bridgeReply = {
      status: 200,
      body: {
        schema: "honcho_audit",
        summary: { ok: 4, denied: 1 },
        rows: [{ id: 9, tool: "chat", caller: "mate@example.com", status: "denied", query_text: "집 주소" }],
      },
    };

    const config = await get("/api/dashboard/config");
    assert.equal(config.body.has_audit_log, true);

    const audit = await get("/api/dashboard/audit?hours=24&status=denied&tool=chat&limit=50&nonsense=drop");
    assert.equal(audit.status, 200);
    assert.equal(audit.body.enabled, true);
    assert.equal(audit.body.rows[0].query_text, "집 주소");
    assert.deepEqual(audit.body.summary, { ok: 4, denied: 1 });

    assert.equal(seen.length, 1);
    assert.equal(seen[0].authorization, `Bearer ${TOKEN}`, "the token file is read and sent");
    const forwarded = new URL(seen[0].url, "http://bridge").searchParams;
    assert.equal(forwarded.get("hours"), "24");
    assert.equal(forwarded.get("status"), "denied");
    assert.equal(forwarded.get("tool"), "chat");
    assert.equal(forwarded.get("limit"), "50");
    assert.equal(forwarded.get("nonsense"), null, "unknown parameters are not passed through");
  } finally {
    await stopDashboard();
  }
});

test("a bridge error reaches the dashboard as an error, not as an empty log", async () => {
  await startDashboard({
    HONCHO_MCP_AUDIT_URL: `http://127.0.0.1:${bridgePort}/audit`,
    HONCHO_MCP_BEARER_TOKEN_FILE: tokenPath,
  });
  try {
    seen = [];
    bridgeReply = { status: 500, body: { error: "audit read failed", detail: "connection refused" } };

    const audit = await get("/api/dashboard/audit");
    assert.equal(audit.status, 500);
    assert.equal(audit.body.error, "audit read failed");
    assert.equal(audit.body.enabled, undefined, "a failed read is never reported as an enabled empty log");
  } finally {
    await stopDashboard();
  }
});

test("an unreachable bridge is reported as unreachable", async () => {
  const deadPort = await availablePort();
  await startDashboard({
    HONCHO_MCP_AUDIT_URL: `http://127.0.0.1:${deadPort}/audit`,
    HONCHO_MCP_BEARER_TOKEN_FILE: tokenPath,
  });
  try {
    const audit = await get("/api/dashboard/audit");
    assert.equal(audit.status, 502);
    assert.match(audit.body.error, /unreachable/);
  } finally {
    await stopDashboard();
  }
});
