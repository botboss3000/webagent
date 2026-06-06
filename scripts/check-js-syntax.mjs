#!/usr/bin/env node
/*
 * Front-end JS syntax checker — the cheap guard that stops a parse error (e.g.
 * a duplicate `const` declaration) from ever reaching a browser, where it would
 * silently kill the boot chain and leave the app stuck on a spinner.
 *
 * How it works: every front-end .js file is parsed by Node as an ES module
 * (`node --input-type=module --check`). That is a pure SYNTAX check — nothing
 * runs, no DOM/browser globals are needed, so it is safe for every file and
 * takes a few milliseconds each. A duplicate declaration, an unbalanced brace,
 * a stray keyword, etc. all fail here with the exact file + line.
 *
 * Usage:
 *   node scripts/check-js-syntax.mjs            # check all front-end JS
 *   node scripts/check-js-syntax.mjs a.js b.js  # check specific files
 *
 * Exit code 0 = all clean, 1 = at least one file failed (prints the errors).
 * Wired into .git/hooks/pre-commit so a broken file can't be committed.
 */

import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve, relative } from "node:path";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Directories/files scanned by default. Add more roots here if the UI grows.
const DEFAULT_ROOTS = ["ui", "sw.js"];

function walk(path, out) {
  let st;
  try { st = statSync(path); } catch { return; }
  if (st.isDirectory()) {
    for (const name of readdirSync(path)) {
      if (name === "node_modules" || name.startsWith(".")) continue;
      walk(join(path, name), out);
    }
  } else if (path.endsWith(".js")) {
    out.push(path);
  }
}

function collectDefault() {
  const out = [];
  for (const r of DEFAULT_ROOTS) {
    const p = join(ROOT, r);
    if (existsSync(p)) walk(p, out);
  }
  return out;
}

function checkFile(file) {
  let code;
  try { code = readFileSync(file, "utf8"); } catch (e) {
    return { ok: false, error: `cannot read file: ${e.message}` };
  }
  const res = spawnSync(process.execPath, ["--input-type=module", "--check"], {
    input: code,
    encoding: "utf8",
  });
  if (res.status === 0) return { ok: true };
  // Node prints the parse error (with the line) to stderr.
  return { ok: false, error: (res.stderr || res.stdout || "syntax error").trim() };
}

const args = process.argv.slice(2);
const files = (args.length ? args.map((a) => resolve(a)) : collectDefault());

let failures = 0;
for (const file of files) {
  const { ok, error } = checkFile(file);
  if (!ok) {
    failures++;
    console.error(`\n✗ ${relative(ROOT, file)}`);
    console.error(error.split("\n").map((l) => "    " + l).join("\n"));
  }
}

if (failures) {
  console.error(`\n${failures} file(s) failed the JS syntax check. Commit blocked.`);
  process.exit(1);
}
console.log(`✓ JS syntax OK (${files.length} files checked)`);
