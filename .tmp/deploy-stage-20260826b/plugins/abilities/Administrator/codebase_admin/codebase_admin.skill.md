# Codebase Admin — Execute, Verify, Finish

Load this skill before reading, changing, debugging, or extending the WebAgent repository.

## Operating contract

- Treat requests to fix, implement, change, investigate, or verify as work to complete in the active execution mode. Do not replace requested work with a proposal.
- Give a plan only when it materially helps: one sentence or at most three bullets, once. Start the first inspection or implementation immediately afterward.
- Never repeat, reword, or elaborate a plan while waiting. If blocked, state the exact failed action, missing fact, or approval needed and the smallest next action.
- Prefer checked evidence to narration. Do not claim a cause, test result, or completion without verifying it.
- Stop when the requested result is complete. Do not extend scope with speculative refactors or optional work.

## Work loop

1. Read `CLAUDE.md`, then only the routed guide relevant to the area being changed.
2. Locate the narrowest target with `search_source`, `search_comments`, a symbol lookup, or a targeted read. Do not scan whole trees or large files without a reason.
3. Form one testable hypothesis. Batch independent reads and searches.
4. Make the smallest coherent change that solves the request and preserves surrounding conventions and unrelated user changes.
5. Verify proportionally: inspect the diff, then run the narrowest relevant import, test, lint, API check, or UI check.
6. Report outcome, files changed, verification, and any genuine limitation concisely.

## Progress and recovery

- On a failed tool call, read the error and retry once only when the correction is clear. After two failed attempts, change approach or report the blocker; never loop.
- If two searches do not narrow the target, inspect the nearest owning module or ask one focused question.
- Trace caller, callee, data flow, and relevant tests before changing a multi-file path. Avoid speculative rewrites.
- For a diagnosis, report: observed evidence → likely cause → confidence → next action.

## Safety and tools

- Read/search/list/status/diff/log and non-mutating verification commands are inspection. Run them without asking for approval.
- Classify an operation by its effect, not its name. A command is not destructive merely because it is a command.
- When a persistent mutation requires approval under the current mode or tool policy, describe the concrete effect once and wait. When approved, execute it rather than replanning it.
- Never delete, overwrite, or reset files merely because backups may exist. Verify the exact target and need first.
- Use targeted patches for small edits. If a patch fails twice, reread the exact region and use a smaller patch or a precise replacement.

## WebAgent conventions

- New integrations, channels, abilities, schedulers, and similar capabilities are drop-in files under `plugins/` with the repository’s `FEATURE` metadata pattern; do not add central registries or dispatch branches for them.
- Use `search_comments` as an index, then read surrounding source before relying on a hit.
- For UI work, read `docs/claude/ui-guidance.md`; support both themes with design-system variables, preserve breadcrumb comments, and search applicable consistency markers before editing repeated UI.
- Search these markers when relevant: `CHAT-PILL-SYNC`, `PILL-PANEL-LAYOUT`, `PILL-PANEL-ALIGN`, `SISTER-PANEL`, `RENAME-FIELD PATTERN`, `CAROUSEL-WIRING PATTERN`, and `PREVENTIVE-COMMENTS`.
- Reuse `ui/shared/js/dom-utils.js` utilities instead of copying helpers. For intentional duplication, document and synchronize every marked copy.
- Every frontend diagnostic log must start with `DEBUG-TAG:` and be removed when no longer useful.
- **Chat footer / widget / panel edits are config-first.** When the user wants to edit the chat footer, chat widget, or chat panel, open `data/config/chat_ui.json` FIRST and make the change there — that file governs those surfaces (header, footer, chat pill, sizing/layout), and hard-coded CSS/JS only provides structural defaults. Only touch hard code when the requested change genuinely cannot be expressed in `chat_ui.json`.
- **`plugins/engines/` is special and off-limits by default.** It hosts the Claude Code and Codex CLI engine adapters (`claude_code/`, `codex/`, plus `terminal_chat/`) and is NOT part of the normal WebAgent repo. Unless the user explicitly asks to work on the codex or Claude agents, do not read, search, or modify anything under `plugins/engines/`.

## Repository map

- Backend and agent loop: `app/`; API routes: `app/api/`; prompt/agent logic: `app/agent/`; storage: `app/db/`.
- Plugin abilities and their tools: `plugins/abilities/`.
- Chat UI: `ui/chat/`; shared UI: `ui/shared/`; admin tools: `ui/admin-tools/`.
- Default agent templates: `app/defaults/agents/`.

## Completion

- Review the final diff; run the relevant verification; do not claim success on an unrun or failing check.
- Update repository documentation only when the change affects documented behavior, structure, configuration, or usage.
- Do not create scratch reports or helper artifacts in the repository.
