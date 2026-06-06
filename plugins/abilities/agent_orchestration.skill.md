# Agent Orchestration — spawning, conversing with, and overseeing helper agents

You have the **Agent Orchestration** ability. It lets you act as an *orchestrator*:
create purpose-built helper agents on demand, hold a real back-and-forth with them,
run them blocking or in the background, and supervise the background ones with
durable timers. It also lets you hand a session off entirely (`delegate_to_agent`)
or run the prompt optimizer (`run_optimizer`). Load this guide before you spawn or
delegate.

## The mental model

- A **spawn** is a *helper agent in its own saved session*. You sit in that
  session's "user" seat, so your whole conversation with the helper is recorded
  like any chat and is visible to the human in the sidebar (badged as a spawn).
- You stay in charge. Unlike delegating, spawning does **not** replace you — you
  keep running and the helper works alongside you.
- Work is **event-driven, not a blocking wait**. You choose per call whether to
  block for the result or fork the helper and be *re-woken* later when it finishes
  or when a timer fires.

## The tools

**Spawn & run**
- `spawn_agent(task, name?, system_prompt?, from_agent?, wait?, check_back_minutes?)`
  — create a new helper and set it to work. Give it an identity **one** of two ways:
  - write `system_prompt` — a plain directive describing who the helper is and what
    to do (you write this yourself, on the spot); **or**
  - pass `from_agent` — a template id to clone an existing agent (get ids from
    `list_delegatable_agents`).
  - `wait=true` → block and get the helper's reply back now.
  - `wait=false` (default) → **fork**: you get a `spawn_id` immediately and carry on;
    you'll be re-woken with the result when the helper finishes.
  - `check_back_minutes>0` → also set a follow-up timer (see oversight below).
  Returns the `spawn_id` you use for every other tool.

**Converse & inspect**
- `message_spawn(spawn_id, message, wait?)` — send another message into a helper's
  session to continue the conversation. `wait` works the same as on spawn.
- `read_spawn(spawn_id, limit?)` — read a helper's transcript so far. Use this before
  you reply to a forked helper or report back, so you know where it got to.
- `list_spawns()` — every helper you've spawned in this session, with status
  (`pending` / `running` / `done` / `error` / `stopped`), last result summary, and any
  pending timer.

**Oversee & stop**
- `schedule_spawn_check(spawn_id, minutes, note?)` — set a **durable** follow-up
  timer. When it elapses you're re-woken — *even if the helper is still running* —
  with your `note`. Survives a server restart; fires once; setting a new one
  replaces any pending timer for that spawn.
- `stop_spawn(spawn_id)` — interrupt a helper's current run and mark it stopped
  (its session and transcript are kept).

**Hand off / optimize (the original orchestration tools)**
- `list_delegatable_agents()` — the agents you can delegate to or clone, with each
  one's trigger description. Use it to pick a `from_agent` for spawning, or a target
  for delegation.
- `delegate_to_agent(agent_template_id, context?)` — hand the **whole current
  session** to another agent. This **replaces you** — you stop and the other agent
  takes over the conversation. Use it when another agent should own the rest of the
  chat, not when you want a helper.
- `run_optimizer(feedback?, ...)` — kick off the prompt-optimizer flow for this
  session.

## wait vs. fork — choosing

- **wait** — for a single, bounded sub-task whose answer you need *right now* before
  you can continue ("summarise this document", "compute X"). Simple and synchronous.
- **fork** — for long-running or parallel work, or when you want to keep making
  progress while the helper works ("research these 5 vendors", "watch this build").
  You'll get an `[ORCHESTRATION EVENT]` message when each one finishes. Fork several
  helpers to run them in parallel.

## Reacting to orchestration events

When a forked helper finishes — or a follow-up timer fires — you are re-woken with a
message that starts with **`[ORCHESTRATION EVENT]`**. Treat these as signals to act,
not as the human talking:
- *Spawn finished* → read the result; decide to reply to it (`message_spawn`), read
  its full transcript (`read_spawn`), spawn a follow-on helper, or report back to the
  human.
- *Follow-up timer* → check the helper's status; let it keep running, nudge or
  redirect it, set another timer (`schedule_spawn_check`), stop it, or report in.

## Overseeing forked work — the pattern

1. Fork the helpers you need: `spawn_agent(..., wait=false)` (optionally with
   `check_back_minutes` for a safety check-in).
2. Carry on with your own work, or tell the human what you've set in motion.
3. When an `[ORCHESTRATION EVENT]` arrives, handle it as above.
4. For a helper you forked without a notify-on-finish expectation, or one taking a
   long time, set `schedule_spawn_check(spawn_id, minutes, note)` so you don't forget it.
5. Use `list_spawns()` anytime to see the whole fleet at a glance.

## Good practice

- **Write self-contained tasks.** The helper doesn't see your conversation — put
  everything it needs in the `task` / `system_prompt`.
- **Name your spawns** so `list_spawns` and the sidebar are readable.
- **Don't fork-and-forget.** If you fork something you depend on, either rely on the
  finish event or set a `schedule_spawn_check` — never leave work untracked.
- **Prefer cloning (`from_agent`) when a fitting agent exists** — it inherits that
  agent's tools and configuration. Write a fresh `system_prompt` for one-off helpers.
- **spawn vs delegate:** spawn = a helper you supervise and stay above; delegate =
  you step aside and another agent owns the session. Pick deliberately.
- **Stop cleanly.** If a helper is off track or no longer needed, `stop_spawn` it
  rather than leaving it running.
