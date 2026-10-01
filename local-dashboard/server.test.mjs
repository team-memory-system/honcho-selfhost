import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import { request as httpRequest, createServer as createHttpServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";

const dashboardRoot = dirname(fileURLToPath(import.meta.url));
let dashboard;
let dashboardOutput = "";
let port;
let upstream;
let upstreamPort;
let tempRoot;
let toolConfigPath;

async function availablePort() {
  const server = createHttpServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const selectedPort = server.address().port;
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return selectedPort;
}

function request(path, { body, contentType = "application/json", host, method, origin } = {}) {
  const payload = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
  const headers = {
    host: host || `127.0.0.1:${port}`,
    ...(payload ? { "content-length": Buffer.byteLength(payload), "content-type": contentType } : {}),
    ...(origin === undefined ? {} : { origin }),
  };
  return new Promise((resolve, reject) => {
    const req = httpRequest({ hostname: "127.0.0.1", port, path, method: method || (body === undefined ? "GET" : "POST"), headers }, res => {
      const chunks = [];
      res.on("data", chunk => chunks.push(chunk));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        resolve({ status: res.statusCode, body: text ? JSON.parse(text) : null });
      });
    });
    req.on("error", reject);
    req.end(payload);
  });
}

before(async () => {
  tempRoot = await fs.mkdtemp(join(tmpdir(), "honcho-dashboard-test-"));
  toolConfigPath = join(tempRoot, "nested", "tool-config.json");
  upstreamPort = await availablePort();
  upstream = createHttpServer((req, res) => {
    const payload = JSON.stringify({ method: req.method, url: req.url });
    res.writeHead(200, { "content-type": "application/json", "content-length": Buffer.byteLength(payload) });
    res.end(payload);
  });
  await new Promise((resolve, reject) => {
    upstream.once("error", reject);
    upstream.listen(upstreamPort, "127.0.0.1", resolve);
  });
  port = await availablePort();
  dashboard = spawn(process.execPath, ["server.mjs"], {
    cwd: dashboardRoot,
    env: {
      ...process.env,
      DASHBOARD_HOST: "127.0.0.1",
      DASHBOARD_PORT: String(port),
      HONCHO_URL: `http://127.0.0.1:${upstreamPort}`,
      HONCHO_MCP_TOOL_CONFIG: toolConfigPath,
      MCP_CONTROL_MODE: "file",
      MCP_CONTROL_ALLOW_REMOTE: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  dashboard.stdout.on("data", chunk => { dashboardOutput += chunk; });
  dashboard.stderr.on("data", chunk => { dashboardOutput += chunk; });

  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Dashboard did not start.\n${dashboardOutput}`)), 5_000);
    const check = chunk => {
      if (!chunk.toString().includes("Honcho dashboard:")) return;
      clearTimeout(timeout);
      dashboard.stdout.off("data", check);
      resolve();
    };
    dashboard.stdout.on("data", check);
    dashboard.once("exit", code => {
      clearTimeout(timeout);
      reject(new Error(`Dashboard exited with ${code}.\n${dashboardOutput}`));
    });
  });
});

after(async () => {
  if (dashboard && dashboard.exitCode === null) {
    dashboard.kill("SIGTERM");
    await new Promise(resolve => {
      const timeout = setTimeout(resolve, 2_000);
      dashboard.once("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
    });
  }
  if (upstream) {
    await new Promise(resolve => upstream.close(() => resolve()));
  }
  if (tempRoot) await fs.rm(tempRoot, { recursive: true, force: true });
});

test("MCP control accepts localhost clients but rejects CSRF and DNS-rebinding requests", async () => {
  const noOrigin = await request("/api/dashboard/mcp/tools", {
    body: { name: "server_info", enabled: false },
  });
  assert.equal(noOrigin.status, 200, "localhost CLI requests without Origin remain supported");

  const sameOrigin = await request("/api/dashboard/mcp/tools", {
    body: { name: "server_info", enabled: true },
    host: `localhost:${port}`,
    origin: `http://localhost:${port}`,
  });
  assert.equal(sameOrigin.status, 200, "same-origin Docker localhost browser requests remain supported");

  const csrf = await request("/api/dashboard/mcp/tools", {
    body: { name: "server_info", enabled: false },
    origin: "https://attacker.example",
  });
  assert.equal(csrf.status, 403);

  const wrongScheme = await request("/api/dashboard/mcp/tools", {
    body: { name: "server_info", enabled: false },
    host: `localhost:${port}`,
    origin: `https://localhost:${port}`,
  });
  assert.equal(wrongScheme.status, 403);

  const rebinding = await request("/api/dashboard/mcp", {
    body: { enabled: false },
    host: "attacker.example",
    origin: "http://attacker.example",
  });
  assert.equal(rebinding.status, 403);

  const formPost = await request("/api/dashboard/mcp/tools", {
    body: "name=server_info&enabled=false",
    contentType: "application/x-www-form-urlencoded",
  });
  assert.equal(formPost.status, 415);

  const reboundRead = await request("/api/dashboard/config", { host: "attacker.example" });
  assert.equal(reboundRead.status, 403);

  const proxiedCsrf = await request("/api/v3/workspaces/list", {
    body: "{}",
    contentType: "text/plain",
    origin: "https://attacker.example",
  });
  assert.equal(proxiedCsrf.status, 403);

  const bodylessDelete = await request("/api/v3/workspaces/example", { method: "DELETE" });
  assert.equal(bodylessDelete.status, 200);
  assert.equal(bodylessDelete.body.method, "DELETE");

  const deleteCsrf = await request("/api/v3/workspaces/example", {
    method: "DELETE",
    origin: "https://attacker.example",
  });
  assert.equal(deleteCsrf.status, 403);

  const deleteWithNonJsonBody = await request("/api/v3/workspaces/example", {
    method: "DELETE",
    body: "unexpected",
    contentType: "text/plain",
  });
  assert.equal(deleteWithNonJsonBody.status, 415);
});

test("parallel tool toggles preserve every update and leave no colliding temp files", async () => {
  await fs.mkdir(dirname(toolConfigPath), { recursive: true });
  await fs.writeFile(toolConfigPath, '{"version":1,"disabled_tools":[]}\n');
  const listing = await request("/api/dashboard/mcp/tools");
  assert.equal(listing.status, 200);
  const toolNames = listing.body.tools.map(tool => tool.name);

  const responses = await Promise.all(toolNames.map(name => request("/api/dashboard/mcp/tools", {
    body: { name, enabled: false },
  })));
  assert.ok(responses.every(response => response.status === 200));

  const saved = JSON.parse(await fs.readFile(toolConfigPath, "utf8"));
  assert.deepEqual(saved.disabled_tools, [...toolNames].sort());
  const tempFiles = (await fs.readdir(dirname(toolConfigPath))).filter(name => name.endsWith(".tmp"));
  assert.deepEqual(tempFiles, []);

  const current = await request("/api/dashboard/mcp/tools");
  assert.equal(current.body.enabled_count, 0);
});

test("with DASHBOARD_APP_URL the screen goes to the app and the API stays", async () => {
  const appPort = await availablePort();
  const child = spawn(process.execPath, ["server.mjs"], {
    cwd: dashboardRoot,
    env: {
      ...process.env,
      DASHBOARD_HOST: "127.0.0.1",
      DASHBOARD_PORT: String(appPort),
      HONCHO_URL: `http://127.0.0.1:${upstreamPort}`,
      DASHBOARD_APP_URL: "http://127.0.0.1:4180/",
      MCP_CONTROL_MODE: "file",
      HONCHO_MCP_TOOL_CONFIG: toolConfigPath,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  try {
    await new Promise((resolve, reject) => {
      child.stdout.on("data", chunk => { if (String(chunk).includes("Honcho dashboard:")) resolve(); });
      child.once("exit", code => reject(new Error(`dashboard exited ${code}`)));
    });
    const screen = await fetch(`http://127.0.0.1:${appPort}/`, { redirect: "manual" });
    assert.equal(screen.status, 302);
    assert.equal(screen.headers.get("location"), "http://127.0.0.1:4180/");
    const deep = await fetch(`http://127.0.0.1:${appPort}/app.js`, { redirect: "manual" });
    assert.equal(deep.status, 302);
    const config = await fetch(`http://127.0.0.1:${appPort}/api/dashboard/config`);
    assert.equal(config.status, 200);
  } finally {
    child.kill();
  }
});
