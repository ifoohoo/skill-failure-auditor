#!/usr/bin/env node
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  chmod,
  cp,
  lstat,
  mkdir,
  readFile,
  realpath,
  readdir,
  rmdir,
  unlink,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const PROFILE_PREFIX = "packages/skill-failure-auditor/plugin-src/core/foundation/quickstart-profile";
const CORE_PIN_PATH = "packages/skill-failure-auditor/plugin-src/core/foundation/foundation-pin.json";
const SPEC_PIN_PATH = "packages/skill-failure-auditor/spec/foundation-integration/foundation-pin.json";
const MIGRATION_PATH = "packages/skill-failure-auditor/skill-family.migration.json";
const HARNESS_RECEIPT_PATH = "packages/skill-failure-auditor/migration/harness-surface-receipt.json";
const OWNER_ADJUDICATIONS_PATH = "packages/skill-failure-auditor/migration/harness-owner-adjudications.json";
const OWNER = Object.freeze({ kind: "managed", id: "skill-failure-auditor.foundation-adoption" });
const HANDWRITTEN_PATTERNS = Object.freeze([
  "AGENTS.md", "CLAUDE.md", "README.md", "control/**", "evidence/**", "scripts/**",
  "packages/skill-failure-auditor/README.md", "packages/skill-failure-auditor/control/**",
  "packages/skill-failure-auditor/evidence/**", "packages/skill-failure-auditor/tests/**",
  "packages/skill-failure-auditor/plugin-src/core/scripts/**",
  "packages/skill-failure-auditor/plugin-src/core/references/**",
  "packages/skill-failure-auditor/plugin-src/platforms/**",
]);

function fail(message) {
  throw new Error(`foundation projection refused: ${message}`);
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function parseArgs(argv) {
  const values = { tarballs: [] };
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!value) fail(`missing value for ${flag}`);
    if (flag === "--mode") values.mode = value;
    else if (flag === "--install-root") values.installRoot = value;
    else if (flag === "--package-root") values.packageRoot = value;
    else if (flag === "--target-root") values.targetRoot = value;
    else if (flag === "--candidate-root") values.candidateRoot = value;
    else if (flag === "--prepared-output") values.preparedOutput = value;
    else if (flag === "--prepared") values.prepared = value;
    else if (flag === "--tarball") values.tarballs.push(value);
    else if (flag === "--python") values.python = value;
    else fail(`unknown argument: ${flag}`);
  }
  if (!new Set(["prepare", "apply"]).has(values.mode)) fail("--mode must be prepare or apply");
  for (const field of ["targetRoot", "candidateRoot"]) {
    if (!path.isAbsolute(values[field] ?? "")) fail(`--${field.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)} must be absolute`);
  }
  if (values.mode === "prepare") {
    for (const field of ["installRoot", "packageRoot", "preparedOutput", "python"]) {
      if (!path.isAbsolute(values[field] ?? "")) fail(`prepare requires absolute --${field.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`);
    }
    if (values.tarballs.length !== 3 || values.tarballs.some((item) => !path.isAbsolute(item))) {
      fail("prepare requires exactly three absolute --tarball arguments");
    }
  } else if (!path.isAbsolute(values.prepared ?? "")) {
    fail("apply requires absolute --prepared");
  }
  return values;
}

async function readJson(file) {
  return JSON.parse(await readFile(file, "utf8"));
}

async function assertNewExternalCandidate(candidateRoot, targetRoot) {
  const targetReal = await realpath(targetRoot);
  const candidateParentReal = await realpath(path.dirname(candidateRoot));
  const candidateAbs = path.resolve(candidateRoot);
  if (candidateAbs === targetReal || candidateAbs.startsWith(`${targetReal}${path.sep}`)) {
    fail("candidate root must be repository-external");
  }
  if (path.dirname(candidateAbs) !== candidateParentReal) fail("candidate root parent must be canonical");
  try {
    await lstat(candidateAbs);
    fail("candidate root must not already exist");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  await mkdir(candidateAbs, { recursive: false });
}

async function writeCandidate(candidateRoot, relativePath, bytes, mode = 0o644) {
  const destination = path.join(candidateRoot, relativePath);
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, bytes, { flag: "wx", mode });
  await chmod(destination, mode);
}

async function collectFiles(root, relative = "") {
  const records = [];
  async function walk(directory, prefix) {
    for (const name of (await readdir(directory)).sort()) {
      const absolute = path.join(directory, name);
      const rel = prefix === "" ? name : `${prefix}/${name}`;
      const stats = await lstat(absolute);
      if (stats.isDirectory()) await walk(absolute, rel);
      else if (stats.isFile()) records.push({
        path: rel,
        type: "file",
        sha256: sha256(await readFile(absolute)),
        mode: stats.mode & 0o7777,
      });
      else fail(`candidate contains non-regular entry: ${rel}`);
    }
  }
  await walk(root, relative);
  return records;
}

async function removeTemporaryTree(root) {
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) await removeTemporaryTree(target);
    else await unlink(target);
  }
  await rmdir(root);
}

function closure(resources) {
  const sorted = [...resources].sort((left, right) => left.path.localeCompare(right.path));
  return { digestAlgorithm: "sha256", digest: sha256(Buffer.from(JSON.stringify(sorted))), resources: sorted };
}

function migrationFromPin(current, pin) {
  return {
    ...current,
    foundationPackages: pin.packages.map(({ name, version, sha256: digest }) => ({
      name,
      version,
      digest: `sha256:${digest}`,
    })),
  };
}

async function validateInstall({ installRoot, packageRoot, tarballs, pin }) {
  if (process.version !== pin.runtime.node) fail(`Node ${process.version} does not match ${pin.runtime.node}`);
  const installManifest = await readJson(path.join(installRoot, "package.json"));
  if (installManifest.packageManager !== `pnpm@${pin.runtime.pnpm}`) fail("isolated pnpm pin mismatch");
  const pinnedByTarball = new Map(pin.packages.map((item) => [item.tarball, item]));
  if (pinnedByTarball.size !== pin.packages.length ||
      new Set(tarballs.map((item) => path.basename(item))).size !== pin.packages.length ||
      tarballs.some((item) => !pinnedByTarball.has(path.basename(item)))) {
    fail("tarballs must be the exact pinned three-package set");
  }
  for (const tarball of tarballs) {
    const pinned = pinnedByTarball.get(path.basename(tarball));
    if (!pinned || sha256(await readFile(tarball)) !== pinned.sha256) fail(`tarball mismatch: ${path.basename(tarball)}`);
    const dependency = installManifest.dependencies?.[pinned.name];
    if (typeof dependency !== "string" || !dependency.startsWith("file:") ||
        path.resolve(installRoot, dependency.slice("file:".length)) !== path.resolve(tarball)) {
      fail(`isolated dependency is not bound to tarball: ${pinned.name}`);
    }
  }
  for (const pinned of pin.packages) {
    const installed = await readJson(path.join(installRoot, "node_modules", pinned.name, "package.json"));
    if (installed.name !== pinned.name || installed.version !== pinned.version) fail(`installed package mismatch: ${pinned.name}`);
  }
  const schemaRoot = packageRoot;
  for (const schema of pin.consumerSchemas) {
    const bytes = await readFile(path.join(schemaRoot, schema.sourcePath));
    const document = JSON.parse(bytes);
    if (sha256(bytes) !== schema.sha256 || document.$id !== schema.$id) fail(`consumer schema mismatch: ${schema.path}`);
  }
}

async function prepare(values) {
  await assertNewExternalCandidate(values.candidateRoot, values.targetRoot);
  const pinPath = path.join(values.packageRoot, "spec/foundation-integration/foundation-pin.json");
  const pinBytes = await readFile(pinPath);
  const pin = JSON.parse(pinBytes);
  await validateInstall({ ...values, pin });
  const consumerSchemas = pin.consumerSchemas.map((item) => ({
    ...item,
    sourcePath: path.join(values.packageRoot, item.sourcePath),
  }));
  const kitRoot = path.join(values.installRoot, "node_modules/skill-family-engineering-kit");
  const candidateApi = await import(pathToFileURL(path.join(kitRoot, "candidate/index.mjs")).href);
  const stableApi = await import(pathToFileURL(path.join(kitRoot, "src/index.mjs")).href);
  const profileSpi = await import(pathToFileURL(path.join(kitRoot, "profile-spi/index.mjs")).href);
  const profileResult = await profileSpi.verifyProjectProfile({ projectRoot: values.packageRoot });
  if (profileResult?.code !== "SPE0000") {
    fail(`project profile rejected by Foundation verifyProjectProfile: ${profileResult?.code ?? "UNKNOWN"}`);
  }
  const schemaRoot = path.join(values.candidateRoot, ".consumer-schema-inputs");
  await mkdir(schemaRoot);
  for (const schema of consumerSchemas) {
    await writeFile(
      path.join(schemaRoot, schema.path),
      await readFile(schema.sourcePath),
      { flag: "wx" },
    );
  }
  const built = await candidateApi.buildQuickstartProfileProjection({
    targetPrefix: PROFILE_PREFIX,
    consumerSchemaRoot: schemaRoot,
    consumerSchemaPaths: consumerSchemas.map((item) => item.path),
    sourceRepository: pin.source.repository,
    sourceBaseCommit: pin.source.baseCommit,
  });
  for (const schema of consumerSchemas) await unlink(path.join(schemaRoot, schema.path));
  await rmdir(schemaRoot);
  if (pin.bundle.payloadSha256 !== built.provenance.payload.digest) {
    fail(`bundle payload digest mismatch: expected ${pin.bundle.payloadSha256}, actual ${built.provenance.payload.digest}`);
  }
  for (const entry of built.manifest.entries) {
    await writeCandidate(values.candidateRoot, entry.path, Buffer.from(entry.content.text));
  }
  await writeCandidate(values.candidateRoot, CORE_PIN_PATH, pinBytes);
  await writeCandidate(values.candidateRoot, SPEC_PIN_PATH, pinBytes);
  const currentMigration = await readJson(path.join(values.packageRoot, "skill-family.migration.json"));
  const migrationBytes = Buffer.from(`${JSON.stringify(migrationFromPin(currentMigration, pin), null, 2)}\n`);
  await writeCandidate(values.candidateRoot, MIGRATION_PATH, migrationBytes);

  const platform = spawnSync(values.python, [
    path.join(values.packageRoot, "scripts/build/build_platforms.py"),
    "--source-package-root", values.packageRoot,
    "--candidate-root", values.candidateRoot,
    "--node", process.execPath,
  ], { encoding: "utf8" });
  if (platform.status !== 0) fail(`platform candidate failed: ${platform.stderr || platform.stdout}`);

  const inventorySourceRoot = path.join(values.candidateRoot, ".inventory-source");
  const inventoryCoreRoot = path.join(inventorySourceRoot, "plugin-src/core");
  await mkdir(inventoryCoreRoot, { recursive: true });
  for (const name of ["references", "scripts"]) {
    await cp(
      path.join(values.packageRoot, "plugin-src/core", name),
      path.join(inventoryCoreRoot, name),
      { recursive: true },
    );
  }
  const inventory = spawnSync(process.execPath, [
    path.join(values.packageRoot, ".skill-family-audit/generate_harness_inventory.mjs"),
    "--harness-entry", path.join(
      values.installRoot,
      "node_modules/skill-family-harness-node/candidate/quickstart-profile.mjs",
    ),
    "--package-root", inventorySourceRoot,
    "--owner-source", path.join(values.packageRoot, "migration/harness-owner-adjudications.json"),
    "--receipt-output", path.join(values.candidateRoot, HARNESS_RECEIPT_PATH),
    "--owner-output", path.join(values.candidateRoot, OWNER_ADJUDICATIONS_PATH),
  ], { encoding: "utf8" });
  await removeTemporaryTree(inventorySourceRoot);
  if (inventory.status !== 0) {
    fail(`harness inventory candidate failed: ${inventory.stderr || inventory.stdout}`);
  }

  const candidateResources = await collectFiles(values.candidateRoot);
  if (candidateResources.length === 0) fail("candidate closure must not be empty");
  const candidatePaths = new Set(candidateResources.map((item) => item.path));
  const previousManagedPaths = new Set(candidatePaths);
  const priorCoreProvenancePath = path.join(
    values.targetRoot,
    PROFILE_PREFIX,
    "foundation-projection.json",
  );
  try {
    const priorCoreProvenance = await readJson(priorCoreProvenancePath);
    previousManagedPaths.add(`${PROFILE_PREFIX}/foundation-projection.json`);
    for (const file of priorCoreProvenance.payload?.files ?? []) {
      if (typeof file.path !== "string" || file.path.length === 0) fail("prior Bundle provenance has an invalid path");
      previousManagedPaths.add(`${PROFILE_PREFIX}/${file.path}`);
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const livePlatformRoot = path.join(
    values.targetRoot,
    "packages/skill-failure-auditor/generated/platforms",
  );
  try {
    for (const resource of await collectFiles(livePlatformRoot)) {
      previousManagedPaths.add(`packages/skill-failure-auditor/generated/platforms/${resource.path}`);
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const priorBuildManifestPath = path.join(
    values.targetRoot,
    "packages/skill-failure-auditor/generated/platforms/build-manifest.json",
  );
  try {
    const priorBuildManifest = await readJson(priorBuildManifestPath);
    previousManagedPaths.add("packages/skill-failure-auditor/generated/platforms/build-manifest.json");
    for (const [platformId, unit] of Object.entries(priorBuildManifest.units ?? {})) {
      for (const file of unit.files ?? []) {
        if (typeof file.path !== "string" || file.path.length === 0) fail("prior platform manifest has an invalid path");
        previousManagedPaths.add(`packages/skill-failure-auditor/generated/platforms/${platformId}/${file.path}`);
      }
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const targetResources = [];
  const ownership = [];
  for (const candidate of candidateResources) {
    const target = path.join(values.targetRoot, candidate.path);
    let expect = { state: "absent" };
    try {
      const stats = await lstat(target);
      if (!stats.isFile()) fail(`managed target is not a regular file: ${candidate.path}`);
      const bytes = await readFile(target);
      expect = { state: "sha256", value: sha256(bytes), type: "file", mode: stats.mode & 0o7777 };
      targetResources.push({ path: candidate.path, type: "file", sha256: expect.value, mode: expect.mode });
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    const platformOwned = candidate.path.startsWith("packages/skill-failure-auditor/generated/platforms/");
    ownership.push({
      path: candidate.path,
      source: candidate.path,
      authoritySource: platformOwned ? "platform-projection-entry" : "bundle-adoption-entry",
      owner: OWNER,
      expect,
      candidate: { type: "file", mode: candidate.mode },
      rootOutput: candidate.path === SPEC_PIN_PATH || candidate.path.endsWith("skill-family.migration.json"),
    });
  }
  for (const relativePath of [...previousManagedPaths].sort()) {
    if (candidatePaths.has(relativePath)) continue;
    const target = path.join(values.targetRoot, relativePath);
    let stats;
    try {
      stats = await lstat(target);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }
    if (!stats.isFile()) fail(`previous managed target is not a regular file: ${relativePath}`);
    const bytes = await readFile(target);
    const expect = { state: "sha256", value: sha256(bytes), type: "file", mode: stats.mode & 0o7777 };
    targetResources.push({ path: relativePath, type: "file", sha256: expect.value, mode: expect.mode });
    ownership.push({
      path: relativePath,
      authoritySource: "platform-projection-entry",
      owner: OWNER,
      expect,
      delete: true,
      rootOutput: false,
    });
  }
  if (candidatePaths.size !== candidateResources.length) fail("candidate paths are not unique");
  const authoritySources = [];
  for (const [id, relativePath] of [
    ["bundle-adoption-entry", "packages/skill-failure-auditor/scripts/build/build_foundation_bundle.mjs"],
    ["platform-projection-entry", "packages/skill-failure-auditor/scripts/build/build_platforms.py"],
  ]) {
    const absolute = path.join(values.targetRoot, relativePath);
    const stats = await lstat(absolute);
    authoritySources.push({ id, path: relativePath, type: "file", sha256: sha256(await readFile(absolute)), mode: stats.mode & 0o7777 });
  }
  const prepared = stableApi.compileProjectionPlan({
    rootBinding: await realpath(values.targetRoot),
    authoritySources,
    handwrittenPolicy: { authoritySource: "bundle-adoption-entry", patterns: HANDWRITTEN_PATTERNS },
    ownership,
    previousOwnedClosure: closure(targetResources),
    externalCandidateClosure: closure(candidateResources),
  });
  const roundTripped = JSON.parse(JSON.stringify(prepared));
  await writeFile(values.preparedOutput, `${JSON.stringify(roundTripped, null, 2)}\n`, { flag: "wx" });
  process.stdout.write(`${JSON.stringify({
    status: "PREPARED_EXTERNAL",
    candidateCount: candidateResources.length,
    previousOwnedCount: targetResources.length,
    payloadSha256: built.provenance.payload.digest,
    preparedDigest: roundTripped.digest,
    candidateClosureDigest: roundTripped.externalCandidateClosure.digest,
    platform: JSON.parse(platform.stdout),
    liveProjectionInvoked: false,
  }, null, 2)}\n`);
}

async function applyPrepared(values) {
  const prepared = JSON.parse(await readFile(values.prepared, "utf8"));
  if (!path.isAbsolute(values.installRoot ?? "")) fail("apply requires absolute --install-root");
  const kitPath = pathToFileURL(path.join(values.installRoot, "node_modules/skill-family-engineering-kit/src/index.mjs"));
  let stableApi;
  try {
    stableApi = await import(kitPath.href);
  } catch {
    fail("apply requires the explicit isolated --install-root; no sibling or environment fallback is allowed");
  }
  const receipt = await stableApi.runProjection({
    root: values.targetRoot,
    manifest: prepared.manifest,
    candidateRoot: values.candidateRoot,
    preparedProjection: prepared,
  });
  process.stdout.write(`${JSON.stringify(receipt, null, 2)}\n`);
}

const values = parseArgs(process.argv.slice(2));
if (values.mode === "prepare") await prepare(values);
else await applyPrepared(values);
