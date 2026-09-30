import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const MARKER = ".honcho-source.json";
const KIND = "honcho-selfhost-source";

function git(directory, args, options = {}) {
  return execFileSync("git", ["-C", directory, ...args], {
    encoding: "utf8", stdio: "pipe", maxBuffer: 16 * 1024 * 1024, ...options,
  }).trim();
}

async function exists(target) {
  try { await fs.lstat(target); return true; } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

function inside(parent, target) {
  const relative = path.relative(parent, target);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function sourcePath(root, relative) {
  if (typeof relative !== "string" || !relative || path.isAbsolute(relative)) throw new Error("Source paths must be relative");
  const target = path.resolve(root, relative);
  if (target === root || !inside(root, target)) throw new Error(`Source path escapes the checkout: ${relative}`);
  return target;
}

async function checkedUpstream(root, manifest) {
  const upstream = sourcePath(root, manifest.upstream.path);
  if (!/^[a-f0-9]{40}$/.test(manifest.upstream.commit || "")) throw new Error("Upstream must be pinned to a full commit hash");
  if (!(await exists(path.join(upstream, ".git")))) {
    git(root, ["submodule", "update", "--init", "--", manifest.upstream.path], { timeout: 900_000 });
  }
  const commit = git(upstream, ["rev-parse", "HEAD"]);
  if (commit !== manifest.upstream.commit) throw new Error(`Upstream checkout is ${commit}, expected ${manifest.upstream.commit}`);
  const entry = git(root, ["ls-files", "--stage", "--", manifest.upstream.path]);
  if (!entry.startsWith(`160000 ${commit} 0\t`)) throw new Error("Upstream submodule and selfhost-source.json disagree; stage the matching gitlink");
  if (git(upstream, ["status", "--porcelain", "--untracked-files=no"])) {
    throw new Error("Upstream has local changes; keep custom changes in patches instead");
  }
  const recordedVersion = (await fs.readFile(path.join(root, ".honcho-upstream-version"), "utf8")).trim();
  if (recordedVersion !== manifest.upstream.ref) throw new Error("The upstream version file and source manifest disagree");
  return upstream;
}

async function copyLocalPaths(root, destination, localPaths) {
  const prefixes = localPaths.map(relative => {
    sourcePath(root, relative);
    return relative.replaceAll(path.sep, "/").replace(/\/$/, "");
  });
  const files = git(root, ["ls-files", "-z", "--cached"]).split("\0").filter(Boolean);
  const copied = [];
  for (const relative of files) {
    if (!prefixes.some(prefix => relative === prefix || relative.startsWith(`${prefix}/`))) continue;
    const input = sourcePath(root, relative);
    const output = sourcePath(destination, relative);
    const stat = await fs.lstat(input);
    if (!stat.isFile() && !stat.isSymbolicLink()) continue;
    await fs.mkdir(path.dirname(output), { recursive: true });
    if (stat.isSymbolicLink()) await fs.symlink(await fs.readlink(input), output);
    else {
      await fs.copyFile(input, output);
      await fs.chmod(output, stat.mode & 0o777);
    }
    copied.push(relative);
  }
  return copied;
}

async function replaceOutput(candidate, output) {
  const previous = `${output}.previous-${crypto.randomUUID()}`;
  let saved = false;
  if (await exists(output)) {
    const stat = await fs.lstat(output);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`Refusing to replace non-directory output: ${output}`);
    const marker = JSON.parse(await fs.readFile(path.join(output, MARKER), "utf8").catch(() => "null"));
    if (marker?.kind !== KIND) throw new Error(`Refusing to replace output not created by this preparer: ${output}`);
    await fs.rename(output, previous);
    saved = true;
  }
  try { await fs.rename(candidate, output); }
  catch (error) {
    if (saved) await fs.rename(previous, output);
    throw error;
  }
  if (saved) await fs.rm(previous, { recursive: true, force: true });
}

function upstreamLinks(upstream, commit) {
  return git(upstream, ["ls-tree", "-r", "-z", commit]).split("\0").filter(Boolean).flatMap(entry => {
    const separator = entry.indexOf("\t");
    const [mode] = entry.slice(0, separator).split(" ");
    if (mode !== "120000") return [];
    const relative = entry.slice(separator + 1);
    return [{ path: relative, target: git(upstream, ["show", `${commit}:${relative}`]) }];
  });
}

async function materializeLinks(directory, links) {
  // Honcho's .agents/skills and .claude/skills aliases must work on Windows
  // without Developer Mode or administrator symlink privileges.
  for (const link of links) {
    const destination = sourcePath(directory, link.path);
    const target = path.resolve(path.dirname(destination), link.target);
    if (!inside(directory, target) || inside(target, destination)) throw new Error(`Unsupported upstream symlink: ${link.path}`);
    await fs.mkdir(path.dirname(destination), { recursive: true });
    await fs.cp(target, destination, { recursive: true, dereference: true });
  }
}

export async function prepareSource({ root = ROOT, output = path.join(root, ".build", "honcho") } = {}) {
  root = path.resolve(root);
  output = path.resolve(output);
  const manifest = JSON.parse(await fs.readFile(path.join(root, "selfhost-source.json"), "utf8"));
  if (manifest.format !== 1 || !Array.isArray(manifest.patches) || !Array.isArray(manifest.localPaths)) {
    throw new Error("Unsupported selfhost-source.json format");
  }
  const upstreamPath = sourcePath(root, manifest.upstream.path);
  if (inside(output, root) || inside(upstreamPath, output) || inside(output, upstreamPath)) {
    throw new Error("Output must be outside the upstream submodule and cannot replace the wrapper checkout");
  }
  const upstream = await checkedUpstream(root, manifest);
  await fs.mkdir(path.dirname(output), { recursive: true });
  const temporary = await fs.mkdtemp(path.join(path.dirname(output), ".honcho-source-"));
  const candidate = path.join(temporary, "source");
  await fs.mkdir(candidate);
  try {
    const archive = path.join(temporary, "upstream.tar");
    const links = upstreamLinks(upstream, manifest.upstream.commit);
    git(upstream, ["archive", "--format=tar", `--output=${archive}`, manifest.upstream.commit]);
    const tar = process.platform === "win32"
      ? path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "tar";
    execFileSync(tar, ["-xf", archive, "-C", candidate, ...links.map(link => `--exclude=${link.path}`)], { stdio: "pipe" });
    // An isolated index prevents git apply from discovering the wrapper's Git
    // repository when the generated source lives beneath .build/.
    git(candidate, ["init", "--quiet"]);
    const patches = [];
    for (const relative of manifest.patches) {
      const file = sourcePath(root, relative);
      const bytes = await fs.readFile(file);
      git(candidate, ["apply", "--check", file]);
      git(candidate, ["apply", file]);
      patches.push({ path: relative, sha256: crypto.createHash("sha256").update(bytes).digest("hex") });
    }
    await fs.rm(path.join(candidate, ".git"), { recursive: true, force: true });
    await materializeLinks(candidate, links);
    const localFiles = await copyLocalPaths(root, candidate, manifest.localPaths);
    const provenance = {
      format: 1, kind: KIND,
      wrapperCommit: git(root, ["rev-parse", "HEAD"]),
      wrapperDirty: Boolean(git(root, ["status", "--porcelain"])),
      upstream: manifest.upstream,
      patches, localFiles, materializedSymlinks: links,
    };
    await fs.writeFile(path.join(candidate, MARKER), `${JSON.stringify(provenance, null, 2)}\n`);
    await fs.writeFile(path.join(candidate, ".honcho-upstream-version"), `${manifest.upstream.ref}\n`);
    await replaceOutput(candidate, output);
    return { ok: true, output, upstreamCommit: manifest.upstream.commit, patches: patches.length, localFiles: localFiles.length };
  } finally {
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

async function main(args) {
  if (args.includes("--help") || args.includes("-h")) {
    return { usage: "node scripts/prepare-source.mjs [--output <directory>]" };
  }
  if (args.length && (args.length !== 2 || args[0] !== "--output" || !args[1])) throw new Error("Use --output <directory>");
  return prepareSource({ output: args[1] ? path.resolve(args[1]) : undefined });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).then(result => console.log(JSON.stringify(result, null, 2))).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
