/* WEBAGENT TUI — demo content + tool metadata.
   - TOOL_META : per-tool glyph + theme colour token + verb (used by ToolCard).
   - DEMO      : the seed transcript shown on a fresh session.
   - REPLIES   : canned agent turns used by the offline/demo transport
                 (app.jsx falls back to these when the real backend is absent;
                 see agent-bridge.js). Each may carry one tool card.
   Tool `data` shapes must match the renderers in components.jsx → ToolBody. */

window.TOOL_META = {
  read_file:     { glyph: "▤", color: "secondary", verb: "read" },
  write_file:    { glyph: "✚", color: "success",   verb: "write" },
  edit_file:     { glyph: "✎", color: "primary",   verb: "edit" },
  run_shell:     { glyph: "❯", color: "tool",      verb: "command" },
  web_search:    { glyph: "◍", color: "secondary", verb: "search" },
  grep:          { glyph: "⌕", color: "tool",      verb: "search" },
  glob:          { glyph: "✲", color: "secondary", verb: "match" },
  memory_search: { glyph: "❖", color: "primary",   verb: "recall" },
  memory_save:   { glyph: "❖", color: "success",   verb: "save" },
  todo:          { glyph: "☑", color: "tool",      verb: "plan" },
};

/* ── seed transcript ── */
window.DEMO = [
  { type: "user", text: "set up the repo and make sure the tests pass" },
  { type: "agent", text: "On it. Let me look at how tests are wired before I run anything." },
  {
    type: "tool", tool: "read_file", status: "done", expanded: false,
    summary: "package.json",
    data: {
      path: "package.json", start: 1,
      lines: [
        "{",
        '  "name": "webagent",',
        '  "type": "module",',
        '  "scripts": {',
        '    "dev": "vite",',
        '    "test": "vitest run",',
        '    "build": "vite build"',
        "  }",
        "}",
      ],
    },
  },
  { type: "agent", text: "Vitest. Running the full suite now." },
  {
    type: "tool", tool: "run_shell", status: "done", expanded: true,
    summary: "npm test",
    data: {
      cmd: "npm test",
      out: [
        { k: "dim", t: "> webagent@ test" },
        { k: "dim", t: "> vitest run" },
        { k: "out", t: "" },
        { k: "ok",  t: " ✓ src/lib/tools.spec.ts  (12)" },
        { k: "ok",  t: " ✓ src/lib/agent.spec.ts  (6)" },
        { k: "out", t: "" },
        { k: "ok",  t: " Test Files  2 passed (2)" },
        { k: "ok",  t: "      Tests  18 passed (18)" },
        { k: "dim", t: "   Duration  1.21s" },
      ],
      code: 0,
    },
  },
  { type: "agent", text: "All 18 tests pass. I noticed `dispatch()` has no coverage — want me to add a case? Here's the diff, your call:" },
  {
    type: "tool", tool: "edit_file", status: "pending", expanded: true,
    summary: "src/lib/dispatch.test.ts",
    data: {
      path: "src/lib/dispatch.test.ts",
      diff: [
        { t: " ", text: "import { expect, test } from 'vitest';" },
        { t: " ", text: "import { dispatch } from './dispatch';" },
        { t: " ", text: "" },
        { t: "+", text: "test('routes by tool name', () => {" },
        { t: "+", text: "  const r = dispatch('read_file', { path: 'a.ts' });" },
        { t: "+", text: "  expect(r.tool).toBe('read_file');" },
        { t: "+", text: "});" },
      ],
    },
  },
];

/* ── canned replies for the offline/demo transport ── */
window.REPLIES = [
  {
    match: ["test", "tests", "vitest", "spec"],
    text: "Running the test suite now.",
    tool: {
      tool: "run_shell", status: "done", summary: "npm test",
      data: {
        cmd: "npm test",
        out: [
          { k: "dim", t: "> vitest run" },
          { k: "out", t: "" },
          { k: "ok",  t: " ✓ tools.spec.ts  (12)" },
          { k: "ok",  t: " ✓ agent.spec.ts  (6)" },
          { k: "ok",  t: " Tests  18 passed (18)" },
        ],
        code: 0,
      },
    },
  },
  {
    match: ["search the docs", "docs", "search", "look up", "google"],
    text: "Searching the web for that.",
    tool: {
      tool: "web_search", status: "done", summary: "fastapi websocket auth",
      data: {
        query: "fastapi websocket auth",
        results: [
          { title: "WebSockets — FastAPI", url: "fastapi.tiangolo.com/advanced/websockets", snippet: "Use Depends() to authenticate the connection during the handshake." },
          { title: "Securing WS handshakes", url: "fastapi.tiangolo.com/advanced/security", snippet: "Pass a token as a query param and verify before accept()." },
        ],
      },
    },
  },
  {
    match: ["grep", "find", "where is", "usages of"],
    text: "Grepping the source tree.",
    tool: {
      tool: "grep", status: "done", summary: "/dispatch\\(/",
      data: {
        pattern: "dispatch\\(",
        matches: [
          { file: "src/lib/agent.ts", line: 88, text: "const out = dispatch(name, args);" },
          { file: "src/lib/tools.ts", line: 14, text: "export function dispatch(name, args) {" },
          { file: "src/lib/tools.ts", line: 41, text: "  return dispatch(fallback, args);" },
        ],
      },
    },
  },
  {
    match: ["plan", "todo", "break it down", "steps"],
    text: "Here's how I'd break that down:",
    tool: {
      tool: "todo", status: "done", summary: "4 steps",
      data: {
        items: [
          { done: true,  text: "Read the existing dispatch + tool registry" },
          { done: true,  text: "Run the suite to get a green baseline" },
          { done: false, text: "Add a type-safe dispatch table" },
          { done: false, text: "Backfill tests for each tool" },
        ],
      },
    },
  },
  {
    match: ["refactor", "edit", "change", "rename", "type-safe"],
    text: "Proposed edit — review before I apply it:",
    tool: {
      tool: "edit_file", status: "pending", summary: "src/lib/tools.ts",
      data: {
        path: "src/lib/tools.ts",
        diff: [
          { t: " ", text: "export function dispatch(name, args) {" },
          { t: "-", text: "  const fn = TABLE[name];" },
          { t: "-", text: "  return fn(args);" },
          { t: "+", text: "  const fn = TABLE[name];" },
          { t: "+", text: "  if (!fn) throw new Error(`unknown tool: ${name}`);" },
          { t: "+", text: "  return fn(args);" },
          { t: " ", text: "}" },
        ],
      },
    },
  },
  {
    match: ["write", "create a file", "new file", "scaffold"],
    text: "I'll scaffold that file — approve to write it:",
    tool: {
      tool: "write_file", status: "pending", summary: "src/lib/dispatch.ts",
      data: {
        path: "src/lib/dispatch.ts",
        content: [
          { t: "+", text: "import { TABLE } from './tools';" },
          { t: "+", text: "" },
          { t: "+", text: "export function dispatch(name: string, args: unknown) {" },
          { t: "+", text: "  const fn = TABLE[name];" },
          { t: "+", text: "  if (!fn) throw new Error(`unknown tool: ${name}`);" },
          { t: "+", text: "  return fn(args);" },
          { t: "+", text: "}" },
        ],
      },
    },
  },
  {
    match: ["remember", "memory", "note that", "save that"],
    text: "Noted — saving that to memory.",
    tool: {
      tool: "memory_save", status: "done", summary: "saved a note",
      data: { note: "Tests run with `npm test` (vitest); dispatch lives in src/lib/tools.ts." },
    },
  },
  {
    match: ["read", "open", "show me the file", "cat"],
    text: "Opening that file.",
    tool: {
      tool: "read_file", status: "done", summary: "src/lib/agent.ts",
      data: {
        path: "src/lib/agent.ts", start: 84,
        lines: [
          "  // run one tool call",
          "  const name = call.function.name;",
          "  const args = JSON.parse(call.function.arguments);",
          "  const out = dispatch(name, args);",
          "  messages.push(toolResult(call.id, out));",
        ],
      },
    },
  },
  {
    match: ["error", "bug", "failing", "broken", "stack trace"],
    text: "Let me reproduce it.",
    tool: {
      tool: "run_shell", status: "done", summary: "npm run build",
      data: {
        cmd: "npm run build",
        out: [
          { k: "dim", t: "> vite build" },
          { k: "err", t: "src/lib/tools.ts:14:3 - error TS7006:" },
          { k: "err", t: "  Parameter 'args' implicitly has an 'any' type." },
        ],
        code: 1,
      },
    },
  },
  {
    match: ["hello", "hi", "hey", "what can you"],
    text: "Hey — I'm a coding agent. Ask me to run the tests, search the codebase, plan a change, or edit a file. Tool calls show up as cards you can expand and approve.",
  },
];
