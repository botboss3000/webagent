# Automation — scheduling, reminders & event triggers

This ability lets you run work **later** or **when something happens**, instead of only right now. Two trigger types:

- **Time** — a one-shot (fire once, in N minutes or at a set time) or a recurring schedule (a cron pattern that repeats).
- **Event** — fire when an external source does something (an email arrives, a file is added). Events only work for sources an admin has actually connected.

Everything you create is owned by the current user and shows up in their **Automation tab**, where a human can pause, edit, or delete it too.

---

## Pick the right tool

| The user wants… | Use | Notes |
|---|---|---|
| "Remind me to X in/at …" | `remind_me` | One-shot text ping into THIS chat. Still confirm that's where they want it — if they want it anywhere else, use `schedule_task` with `delivery` instead. |
| "Do this task in 10 min / at 3pm" | `schedule_task` with `in_minutes` or `at` | One-shot job that runs a prompt, not just a text ping. |
| "Every morning / weekday / hour, do X" | `schedule_task` with `schedule_cron` | **Recurring — gated** (see Limits). If the gate refuses, fall back to a one-shot and tell the user. |
| "When an email/file/message arrives, do X" | `event_subscribe` | Only if the source is enabled — check first (see Events). |
| "What do I have running?" | `list_automations` / `list_event_subscriptions` | |
| Change / pause / resume / delete one | `update_automation`, `pause_automation`, `resume_automation`, `cancel_automation` | All need the `automation_id` from a list call first. |
| "Run it now" | `run_automation_now` | Fires once immediately, on top of its schedule. |

**Always list before you act on an existing automation.** The mutate tools take an `automation_id` you can only get from `list_automations`. Never guess an id.

---

## Creating a scheduled task

`schedule_task` needs a `prompt` (what to do when it fires) and exactly one timing:

- `in_minutes` — fire once, this many minutes from now.
- `at` — fire once, at an ISO timestamp (e.g. `2026-06-10T15:00:00`). Naive timestamps are treated as UTC.
- `schedule_cron` — repeat on a cron pattern (e.g. `0 9 * * 1-5` = weekdays at 09:00). Set `timezone` (IANA, e.g. `America/New_York`) so the user's "9am" means their 9am.

Write the `prompt` as an instruction to your future self at fire time — it has no memory of this chat. Say what to produce and, if relevant, where it came from. Keep it self-contained.

**`remind_me` is the shortcut for plain reminders.** It builds a one-shot for you and writes the firing prompt so that, when it goes off, you simply deliver the note to the user — you do **not** create another reminder. Never chain reminders by hand; that loops.

---

## Delivery — ALWAYS ask where the result goes

An automation's result has to land somewhere, and the right place depends on the user — a 7am wake-up call is useless in a chat they aren't looking at. **So whenever you set up an automation, ask the user where they want the result delivered before you create it.** Don't silently default to the current chat.

1. **Call `list_delivery_channels` first** so you know which external channels are actually wired up on this agent.
2. **Then ask, offering the real options** — e.g. *"When this fires, where should it go — here in this chat, a brand-new chat, [any channels that came back, e.g. Telegram], saved to a file, or done silently?"* Only name channels `list_delivery_channels` returned.
3. **Set `delivery` to what they choose.** It's a list of targets; each is a channel name or an object:
   - `{mode:"here"}` — reply in the current chat.
   - `{mode:"new_session"}` — a fresh sidebar session.
   - `{mode:"headless"}` — do the work, surface nothing.
   - `{mode:"channel", channel:"telegram", recipient:"…"}` — an external comms channel (only one `list_delivery_channels` returned).
   - `{mode:"webhook", url:"…"}` or `{mode:"file", filename:"…"}`.

You can fan out to several targets at once (e.g. act silently **and** ping Telegram). The per-agent limit caps how many — extra targets are trimmed.

**What works vs. what doesn't:** the current chat, a new chat, silent, a file, and a webhook are **always** available. External messaging channels (Telegram, Slack, SMS, Discord) work **only** if `list_delivery_channels` returned them. **Email is not available yet** — there is no outbound mail channel — so if the user asks for email, tell them it isn't set up and offer an alternative (a messaging channel that *is* connected, or save-to-file). Never promise a channel that didn't come back from `list_delivery_channels`.

Whatever you pick is shown to the user on their **Automations page** under the "Output" column, so set it deliberately — the destination is visible, not hidden.

---

## Run mode — who executes it

`run_mode` is usually `inline` (you run it yourself — the default). The clone modes (`fresh_clone`, `dedicated_clone`) need the **Agent Orchestration** ability enabled on this agent; if it isn't, the tool silently falls back to `inline` and returns a `warning`. Relay that fallback to the user rather than pretending a clone ran.

---

## Events — only what's actually connected

Event triggers fire from external sources, and a source only works if an admin has wired up its provider config (OAuth, push topic, etc.). So:

1. **Call `list_event_sources` first.** Use only a source whose `enabled` is true, and only an `event_type` it lists.
2. If a source is discovered but not enabled, `event_subscribe` refuses with `source_not_enabled` — tell the user real-time push isn't available for that source and stop. Do **not** create the subscription anyway.
3. Don't invent sources or event types. If Gmail push isn't on, don't promise "I'll watch your inbox."

Ask where an event trigger's result should go too (same as **Delivery** above) — it's often *not* the chat the user is sitting in when an email lands at 2am, so confirm the destination rather than defaulting.

---

## Recurring guardrails (per fire)

When you do create a recurring or event automation, these optional knobs keep it from running away:

- `max_per_day` — cap fires per day.
- `expires_at` — auto-stop after an ISO timestamp.
- `disable_after_failures` — auto-disable after N consecutive failures.
- `retry_max` / `retry_backoff_seconds` — retry a failed run before giving up that occurrence.

Offer these when a user sets up something open-ended ("every minute forever" deserves an expiry or a daily cap).

---

## Automation memory — state across runs

A recurring automation can remember things between fires. Inside a run, call `remember_automation_state` with the state to keep (an object or string); it **replaces** the prior memory for that automation. On the next fire, that saved state is shown back to you. Use it for counters, "last seen" markers, or anything the next run needs. It only works *during* an automation run, not in normal chat.

---

## `run_automation_now`

Fires an existing automation once, immediately, in addition to its schedule. The result (including a short excerpt of the output) comes back in the tool response and is **already delivered** per the automation's delivery config. It ran exactly once — do **not** call it again for the same request.

---

## Per-agent limits (why a call may be refused)

This ability is configured per agent. A request can be refused or trimmed by these settings — when that happens, relay the limit honestly instead of retrying:

- **Allow Recurring Schedules** — ON by default. If an admin has turned it off, `schedule_cron` is refused; use a one-shot instead.
- **Allow Event Triggers** — if off, `event_subscribe` is refused; use timed tasks.
- **Max Automations per User** — once hit, new schedule/subscribe calls are refused until the user cancels some.
- **Max Fires per Day** — caps an event trigger's daily fires (you may ask for a lower cap, never higher).
- **Max Delivery Targets** — extra `delivery` targets beyond the cap are trimmed.
- **Require Human Approval** — if on, every automation you create starts **paused**; tell the user to enable it from the Automation tab.

When a tool returns an error or a `note`/`warning`, read it and pass its meaning to the user in plain language — don't silently retry the same call.
