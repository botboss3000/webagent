# Agent Manager — building and configuring other agents

You have the **Agent Management** ability: a set of in-process tools for inspecting,
creating, and fully configuring the agents this user owns. This guide is how to use
them well. Load it before you create or edit any agent.

## Golden rules

1. **Read before you write.** Always `get_agent` (and `list_agent_tools` when tools
   are involved) to see the current state before changing anything.
2. **Confirm before writing.** Show the user the current value vs the proposed
   change, and wait for approval before any create/update/delete.
3. **Ownership is enforced.** You can only edit agents the user owns; reads need the
   agent to be visible. The tools reject anything else — don't try to work around it.
4. **You set policy, not danger.** You can require confirmation on a tool or deny it,
   but a tool's built-in "destructive" label is fixed in code — you can never mark a
   dangerous tool as safe.

## The tools

**Read**
- `list_agent_templates(template_id?)` — the templates you can clone from. Pass a
  template_id to also see its starting prompt slots.
- `list_my_agents()` — the user's agents (source `custom` = editable, `template` =
  read-only system agent).
- `get_agent(agent_id)` — one agent's full config (model, limits, trigger, loop),
  its enabled abilities, a tool summary (counts + only the customised tools), and its
  prompt slots.
- `list_agent_tools(agent_id, ability?, query?)` — every tool the agent has, each with
  its availability, permission, whether it's locked, and its built-in danger label.

**Write (owned agents only)**
- `create_agent(name, template_id, description)` — clone a new agent from a template.
- `update_agent(agent_id, …)` — change identity, model settings, the guardrail limits,
  and the trigger (see the field model below).
- `set_agent_tool(agent_id, tool, availability?, permission?)` — set ONE tool's
  availability and/or permission.
- `set_agent_ability(agent_id, ability, enabled)` — turn an ability (a tool bundle)
  on or off.
- `edit_agent_prompt(action, agent_id, …)` — list/get/insert/update/delete prompt slots.
- `manage_agent_skills(action, agent_id, …)` — list/set/remove the agent's knowledge packs.

## The field model — what you can configure

`update_agent` covers the scalar config:

| Group | Fields |
|---|---|
| Identity | `name`, `description` |
| Model | `model`, `temperature`, `max_tokens` |
| Limits (guardrails) | `max_turn_count` (0 = unlimited), `max_wall_seconds`, `max_identical_tool_calls` (0 = off), `max_stall_strikes` (0 = off) |
| Trigger | `trigger_type` (`user_input` / `slash_command` / `tool_call` / `schedule` / `webhook` / `background`), `trigger_key` |

Only the fields you pass change; everything else is left alone.

## Abilities vs skills vs tools — three different things

This is the distinction that matters most. Don't confuse them.

- **Ability** (`set_agent_ability`) — unlocks a *bundle of tools*. Enabling "web_access"
  makes its tools available; disabling it removes them. This is the coarse on/off switch.
- **Tool option** (`set_agent_tool`) — fine control over a *single* tool that an ability
  has already unlocked: how it's surfaced (availability) and whether it can run
  (permission). See below.
- **Skill** (`manage_agent_skills`) — a *knowledge pack*, not a tool. Written how-to that
  guides the agent. `always_on` = the body is in the prompt every turn; `selectable` =
  the agent only sees the name + description and pulls the body in with `load_skill` when
  a task matches. Use `selectable` for niche/occasional know-how so it never bloats every
  prompt.

Example — "an assistant that emails and knows our accounting software":
`set_agent_ability(email, on)` for the tools, **plus** `manage_agent_skills(set,
mode='selectable', name='accounting-software', description='when to use it', body='steps')`
for the niche know-how.

## Per-tool control with `set_agent_tool`

Each tool has two independent knobs:

- **availability** — `sent` (full schema in context every turn) or `discover` (name only;
  the agent loads it on demand with `load_tool`, saving context). Move rarely-used or
  heavy tools to `discover` to keep the agent lean.
- **permission** — `auto` (runs unattended), `ask` (requires confirmation first), or
  `deny` (the agent cannot use it at all).

Notes:
- **Core tools are locked** — the meta-tools the agent can't function without. `set_agent_tool`
  refuses to change them.
- **Danger labels are read-only.** `list_agent_tools` shows each tool's `destructive` flag.
  You can set a destructive tool to `ask` or `deny`, but you can't relabel it as safe.
- Check the exact tool name with `list_agent_tools` first — `set_agent_tool` rejects a name
  the agent doesn't have.

## Workflow — create a tailored agent

1. `list_agent_templates()` → pick the closest starting template.
2. Confirm the name + template + purpose with the user.
3. `create_agent(name, template_id, description)`.
4. Enable the abilities it needs: `set_agent_ability(agent_id, <ability>, true)`.
5. `list_agent_tools(agent_id)` → review what those abilities unlocked.
6. Tighten per-tool: `set_agent_tool` to `deny` anything it shouldn't touch and `ask` on the
   risky ones; move rarely-used tools to `discover`.
7. Set its prompts with `edit_agent_prompt` and any niche know-how with `manage_agent_skills`.
8. Adjust limits/trigger with `update_agent`.
9. Summarise to the user what you built.

## Workflow — lock down an existing agent

1. `get_agent` + `list_agent_tools` to see the current surface.
2. For each dangerous tool: `set_agent_tool(agent_id, tool, permission='deny')` (block) or
   `'ask'` (confirm-first).
3. Consider disabling whole abilities it doesn't need with `set_agent_ability(..., false)`.
4. Re-read with `list_agent_tools` and confirm the result to the user.

## Workflow — inspect / diagnose

1. `list_my_agents()` → find the agent by name.
2. `get_agent(agent_id)` → config, abilities, tool summary, prompts.
3. `list_agent_tools(agent_id, query=…)` → drill into specific tools.
4. Report findings before proposing any change.
