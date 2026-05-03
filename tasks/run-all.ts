#!/usr/bin/env tsx
/**
 * webAgent - Sub-Agent Orchestrator
 *
 * Spawns pi sub-agents to implement tool-loop improvements.
 * Run: npx tsx python_agent/tasks/run-all.ts [01|02|03|all]
 */

import { createAgentSession, createReadTool, createBashTool, createEditTool, createWriteTool } from "@mariozechner/pi-coding-agent";
import { readFileSync, existsSync, statSync } from "fs";
import path from "path";

const cwd = process.cwd();
const PYTHON_AGENT_DIR = path.resolve("python_agent");
const pythonVerifCmd = "python -c \"import ast; ast.parse(open('python_agent/app/agent/streaming_loop.py').read()); print('OK')\"";

function loadTask(name: string): string {
  const p = path.join(PYTHON_AGENT_DIR, "tasks", name);
  if (!existsSync(p)) throw new Error("Task file not found: " + p);
  return readFileSync(p, "utf-8");
}

function loadSource(files: string[]): string {
  return files
    .map((f) => {
      const p = path.join(PYTHON_AGENT_DIR, f);
      if (!existsSync(p)) return "=== " + f + " (not found) ===\n";
      return "=== " + f + " ===\n" + readFileSync(p, "utf-8");
    })
    .join("\n\n");
}

function snapshotFiles(files: string[]): Record<string, number> {
  const snap: Record<string, number> = {};
  for (const f of files) {
    const p = path.join(PYTHON_AGENT_DIR, f);
    try { snap[f] = statSync(p).mtimeMs; } catch { snap[f] = 0; }
  }
  return snap;
}

function findChangedFiles(before: Record<string, number>): string[] {
  const changed: string[] = [];
  for (const [f, mtime] of Object.entries(before)) {
    const p = path.join(PYTHON_AGENT_DIR, f);
    try {
      if (statSync(p).mtimeMs !== mtime) changed.push(f);
    } catch { changed.push(f); }
  }
  return changed;
}

const TASKS = [
  {
    name: "01-tool-validation",
    file: "SUBAGENT-01-tool-validation.md",
    sources: ["app/agent/streaming_loop.py", "app/agent/loop.py", "app/tools/loader.py", "app/tools/registry.py", "app/models/schemas.py"],
    verifyFiles: ["app/tools/loader.py", "app/agent/streaming_loop.py", "app/agent/loop.py"],
  },
  {
    name: "02-parallel-execution",
    file: "SUBAGENT-02-parallel-tool-execution.md",
    sources: ["app/agent/streaming_loop.py", "app/agent/loop.py"],
    verifyFiles: ["app/agent/streaming_loop.py", "app/agent/loop.py"],
  },
  {
    name: "03-structured-errors",
    file: "SUBAGENT-03-structured-error-handling.md",
    sources: ["app/agent/streaming_loop.py", "app/agent/loop.py", "app/tools/tracker.py"],
    verifyFiles: ["app/agent/streaming_loop.py", "app/agent/loop.py", "app/tools/tracker.py"],
  },
];

function buildSystemPrompt(name: string, taskSpec: string, sourceCode: string): string {
  return [
    "You are a senior Python developer working on the webAgent project.",
    "",
    "## Your Task",
    "",
    "You MUST implement the following specification by actually editing the source files.",
    "Do NOT just describe what you would do. Actually make the changes.",
    "",
    taskSpec,
    "",
    "## Current Source Files",
    "",
    sourceCode,
    "",
    "## Instructions",
    "",
    "1. You have access to tools: read, bash, edit, write",
    "2. Read each source file first to see its current content.",
    "3. Use edit to make changes to the files. All paths are relative to " + cwd,
    "4. After each edit, verify syntax: " + pythonVerifCmd,
    "5. Keep working until the full spec is implemented.",
    "6. Report a summary of every change you made.",
    "",
    "START NOW: read the first source file.",
  ].join("\n");
}

async function runSubAgent(
  name: string,
  taskSpec: string,
  sourceCode: string,
  verifyFiles: string[],
): Promise<{ success: boolean; changed: string[]; error?: string }> {
  console.log("\n" + "=".repeat(60));
  console.log(">>> SUB-AGENT: " + name);
  console.log("=".repeat(60) + "\n");

  const beforeSnap = snapshotFiles(verifyFiles);
  const systemPrompt = buildSystemPrompt(name, taskSpec, sourceCode);

  try {
    const { SessionManager } = await import("@mariozechner/pi-coding-agent");
    const { session } = await createAgentSession({
      cwd,
      sessionManager: SessionManager.inMemory(cwd),
    });

    // Bind tools to the session's agent
    const readTool = createReadTool(cwd);
    const bashTool = createBashTool(cwd);
    const editTool = createEditTool(cwd);
    const writeTool = createWriteTool(cwd);
    session.agent.state.tools = [readTool, bashTool, editTool, writeTool];

    // Track events
    let agentDone = false;
    let toolCallCount = 0;

    session.subscribe((event: any) => {
      if (event.type === "message_update" && event.assistantMessageEvent?.type === "text_delta") {
        process.stdout.write(event.assistantMessageEvent.delta);
      }
      if (event.type === "tool_execution_start") {
        toolCallCount++;
        const input = event.toolInput ? JSON.stringify(event.toolInput).slice(0, 300) : "{}";
        console.log("\n[TOOL] " + event.toolName + " " + input + "\n");
      }
      if (event.type === "tool_execution_end") {
        console.log("\n[TOOL-END] " + (event.isError ? "FAIL" : "OK") + "\n");
      }
      if (event.type === "agent_end") {
        agentDone = true;
      }
    });

    await session.prompt(systemPrompt);

    // Wait for agent to fully finish
    while (!agentDone) {
      await new Promise((r) => setTimeout(r, 1000));
      try { await session.agent.waitForIdle(); } catch { /* ignore */ }
    }

    const changedFiles = findChangedFiles(beforeSnap);
    console.log("\n--- Agent made " + toolCallCount + " tool calls, changed: " +
      (changedFiles.length > 0 ? changedFiles.join(", ") : "NONE"));

    return {
      success: changedFiles.length > 0 || toolCallCount > 3,
      changed: changedFiles,
    };
  } catch (err: any) {
    console.error("\n[" + name + "] Failed: " + err.message);
    return { success: false, changed: [], error: err.message };
  }
}

async function main() {
  const specificTask = process.argv[2];
  const tasksToRun = specificTask && specificTask !== "all"
    ? TASKS.filter((t) => t.name.startsWith(specificTask))
    : TASKS;

  if (tasksToRun.length === 0) {
    console.error("Unknown task: " + specificTask + ". Use: 01, 02, 03, or all (default)");
    process.exit(1);
  }

  const results: Array<{ name: string; success: boolean; changed: string[]; error?: string }> = [];

  for (const task of tasksToRun) {
    const taskSpec = loadTask(task.file);
    const sourceCode = loadSource(task.sources);
    const result = await runSubAgent(task.name, taskSpec, sourceCode, task.verifyFiles);
    results.push(result);
  }

  console.log("\n" + "=".repeat(60));
  console.log("SUB-AGENT RESULTS");
  console.log("=".repeat(60));
  for (const r of results) {
    const icon = r.success ? "OK" : "FAIL";
    const detail = r.changed.length > 0
      ? " (changed: " + r.changed.join(", ") + ")"
      : (r.error ? " -- " + r.error : " -- no changes made");
    console.log("  " + icon + " " + r.name + detail);
  }
  console.log("=".repeat(60) + "\n");
}

main().catch((err) => {
  console.error("Orchestrator failed:", err);
  process.exit(1);
});
