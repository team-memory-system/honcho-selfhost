import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { prepareSource } from "../scripts/prepare-source.mjs";

function git(cwd, ...args) {
  return execFileSync("git", ["-C", cwd, ...args], { encoding: "utf8", stdio: "pipe" }).trim();
}

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "honcho-source-test-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const upstream = path.join(root, "upstream", "honcho");
  await fs.mkdir(path.join(upstream, "src"), { recursive: true });
  git(upstream, "init", "--quiet");
  git(upstream, "config", "core.autocrlf", "false");
  git(upstream, "config", "core.symlinks", "false");
  git(upstream, "config", "user.name", "Test");
  git(upstream, "config", "user.email", "test@example.invalid");
  await fs.writeFile(path.join(upstream, "src", "core.py"), "value = 1\n");
  git(upstream, "add", ".");
  // Record a real Git symlink using a plain worktree file, as Git for Windows
  // does when symlink privileges are unavailable.
  await fs.writeFile(path.join(upstream, "source-alias"), "src");
  const linkBlob = execFileSync("git", ["-C", upstream, "hash-object", "-w", "--stdin"], { input: "src", encoding: "utf8" }).trim();
  git(upstream, "update-index", "--add", "--cacheinfo", `120000,${linkBlob},source-alias`);
  git(upstream, "commit", "--quiet", "-m", "official source");
  const commit = git(upstream, "rev-parse", "HEAD");
  await fs.writeFile(path.join(upstream, "src", "core.py"), "value = 2\n");
  const patch = `${git(upstream, "diff", "--binary", "HEAD")}\n`;
  git(upstream, "restore", "src/core.py");
  await fs.writeFile(path.join(upstream, ".env"), "UNTRACKED_SECRET=never-export\n");
  await fs.mkdir(path.join(root, "patches"));
  await fs.writeFile(path.join(root, "patches", "core.patch"), patch);
  await fs.mkdir(path.join(root, "local-dashboard"));
  await fs.writeFile(path.join(root, "local-dashboard", "index.html"), "companion UI\n");
  await fs.writeFile(path.join(root, "local-dashboard", "token.txt"), "UNTRACKED_SECRET\n");
  await fs.writeFile(path.join(root, ".env"), "PARENT_SECRET=never-export\n");
  const manifest = {
    format: 1,
    upstream: { path: "upstream/honcho", repo: "https://example.invalid/honcho", ref: "v1", commit },
    patches: ["patches/core.patch"], localPaths: ["local-dashboard"],
  };
  await fs.writeFile(path.join(root, "selfhost-source.json"), JSON.stringify(manifest));
  await fs.writeFile(path.join(root, ".honcho-upstream-version"), "v1\n");
  git(root, "init", "--quiet");
  git(root, "config", "core.autocrlf", "false");
  git(root, "config", "user.name", "Test");
  git(root, "config", "user.email", "test@example.invalid");
  git(root, "add", "selfhost-source.json", ".honcho-upstream-version", "patches/core.patch", "local-dashboard/index.html");
  git(root, "update-index", "--add", "--cacheinfo", `160000,${commit},upstream/honcho`);
  git(root, "commit", "--quiet", "-m", "wrapper");
  return { root, upstream, manifest, patch, output: path.join(root, ".build", "honcho") };
}

test("preparation exports the pinned source, applies patches, and leaves upstream and private files alone", async t => {
  const f = await fixture(t);
  const result = await prepareSource({ root: f.root });
  assert.equal(result.upstreamCommit, f.manifest.upstream.commit);
  assert.equal(await fs.readFile(path.join(f.output, "src", "core.py"), "utf8"), "value = 2\n");
  assert.equal((await fs.lstat(path.join(f.output, "source-alias"))).isDirectory(), true);
  assert.equal(await fs.readFile(path.join(f.output, "source-alias", "core.py"), "utf8"), "value = 2\n");
  assert.equal(await fs.readFile(path.join(f.upstream, "src", "core.py"), "utf8"), "value = 1\n");
  assert.equal(await fs.readFile(path.join(f.output, "local-dashboard", "index.html"), "utf8"), "companion UI\n");
  for (const file of [".git", ".env", "local-dashboard/token.txt"]) await assert.rejects(fs.access(path.join(f.output, file)));
  const metadata = JSON.parse(await fs.readFile(path.join(f.output, ".honcho-source.json"), "utf8"));
  assert.equal(metadata.kind, "honcho-selfhost-source");
  assert.match(metadata.patches[0].sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual(metadata.localFiles, ["local-dashboard/index.html"]);
});

test("a conflicting patch preserves the last prepared source and the original checkout", async t => {
  const f = await fixture(t);
  await prepareSource({ root: f.root });
  const metadata = await fs.readFile(path.join(f.output, ".honcho-source.json"));
  await fs.writeFile(path.join(f.root, "patches", "core.patch"), f.patch.replace("-value = 1", "-missing = 1"));
  await assert.rejects(prepareSource({ root: f.root }), /patch does not apply|patch failed/);
  assert.deepEqual(await fs.readFile(path.join(f.output, ".honcho-source.json")), metadata);
  assert.equal(await fs.readFile(path.join(f.output, "src", "core.py"), "utf8"), "value = 2\n");
  assert.equal(await fs.readFile(path.join(f.upstream, "src", "core.py"), "utf8"), "value = 1\n");
  assert.deepEqual(await fs.readdir(path.dirname(f.output)), ["honcho"]);
});

test("mismatched or edited upstream source is refused before creating an output", async t => {
  const f = await fixture(t);
  const config = path.join(f.root, "selfhost-source.json");
  await fs.writeFile(config, JSON.stringify({ ...f.manifest, upstream: { ...f.manifest.upstream, commit: "0".repeat(40) } }));
  await assert.rejects(prepareSource({ root: f.root }), /expected 000000/);
  await fs.writeFile(config, JSON.stringify(f.manifest));
  await fs.writeFile(path.join(f.upstream, "src", "core.py"), "uncommitted user change\n");
  await assert.rejects(prepareSource({ root: f.root }), /Upstream has local changes/);
  await assert.rejects(fs.access(f.output));
  assert.equal(await fs.readFile(path.join(f.upstream, "src", "core.py"), "utf8"), "uncommitted user change\n");
});

test("preparation will not replace an unrelated output directory", async t => {
  const f = await fixture(t);
  await fs.mkdir(f.output, { recursive: true });
  await fs.writeFile(path.join(f.output, "keep.txt"), "user data\n");
  await assert.rejects(prepareSource({ root: f.root }), /not created by this preparer/);
  assert.deepEqual(await fs.readdir(f.output), ["keep.txt"]);
});
