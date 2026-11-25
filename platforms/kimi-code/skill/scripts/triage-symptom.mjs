#!/usr/bin/env node
// 跨仓故障归因只读诊断 CLI 的 Node 入口包装器，转发至同目录 triage_symptom.py。
import { spawnSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const pythonEntry = path.join(scriptDir, "triage_symptom.py");
const env = { ...process.env, PYTHONDONTWRITEBYTECODE: "1" };

const result = spawnSync("python3", ["-B", pythonEntry, ...process.argv.slice(2)], {
  cwd: process.cwd(),
  env,
  stdio: "inherit",
});

if (result.error) {
  console.error(`ERROR [triage-symptom]: 无法启动 python3 运行诊断脚本（${result.error.message}）`);
  process.exit(2);
}
process.exit(result.status ?? 2);
