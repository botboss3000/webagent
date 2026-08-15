# Changing your own model and thinking depth

You can right-size **this conversation** — both *which* model runs it and *how hard*
that model thinks (its reasoning effort) — to fit the task in front of you. Every
change applies to this chat only, persists until changed, and takes effect on the
**next** turn.

## Rate the task first

Before a demanding step, quickly judge how much real thinking it needs:

- **Light / mechanical** (look something up, fetch a value, simple edit, small talk)
  → the cheap default model at low/minimal effort is right. Don't burn a stronger
  model or high effort on trivia.
- **Heavy reasoning** (tricky code, multi-step planning, careful analysis, a hard
  decision) → raise the effort, and/or move onto a stronger model, for that stretch.

You can't read a true probability dial — this is a judgement call, the same way the
posture setting (Conservative / Balanced / Eager) nudges you. Make the call, act,
and **revert when the hard part is done.**

## See what's on the menu first — `list_models`

You don't need to memorise model ids. **`list_models()`** returns the menu of models
you can switch onto, each badged with what it can do — **sees images**, **makes
images**, **premium** tier — plus which model is **running now**, which is the
**default**, and the **effort levels** you can set. It's read-only and changes
nothing.

Use it to switch by **capability, not by name**:

- The user **named a model** → you have the id already; pass it to `set_model('<id>')`
  (the menu also lets you confirm it's actually available).
- You're deciding **on your own** → read the menu and pick by need: something with
  **makes_images** for image generation, **sees_images** for sustained image work, or
  just call `use_premium_model()` for the premium tier. You never have to know the id.

If a capability isn't on the menu (`premium_available` / `image_out_available` is
false), it isn't configured — tell the user plainly rather than guessing or pretending.

## The two dials

### 1. Which model — `set_model` / `use_premium_model`

- **`set_model('<model id>')`** switches onto a specific model (the user named one, or
  you want a vision/image-output model for image work). It must be an **enabled,
  tool-capable** model; if it isn't, the tool tells you which you can switch to —
  relay that, don't guess.
- **`use_premium_model()`** upgrades onto the configured **premium** model — the
  row an admin assigned the *Premium* role to in App Config → Models (stored
  per-model in the user's DB llm config and resolved through the same role-slot
  mechanism the chat footer picker uses, with the legacy `high_effort_targets`
  resolver as a fallback). A premium-role model qualifies even when it is NOT
  enabled as an everyday brain (the "premium-only" pattern) — that is the normal
  way a dedicated premium model is set up. No-op if you're already on one. If
  none is configured, the tool says so — relay that an admin can assign the
  Premium role to a capable model; don't pretend you switched.

### 2. How hard it thinks — `set_effort`

- **`set_effort('<level>')`** sets the reasoning effort for the model this chat is
  running on. Levels: **minimal · low · medium · high** (and **default** to clear the
  hint and use the model's own default). Optionally pass a model id to set a specific
  model's level; each model remembers its own.
- Raise it (`'high'`) for deep reasoning; lower it (`'minimal'`/`'low'`) for speed on
  routine work. Models that don't support reasoning simply ignore it.

These compose. Two common moves:

- **Keep the default model, just think harder:** `set_effort('high')`.
- **Switch model *and* set its depth:** `set_model('<stronger id>')`, then
  `set_effort('high')`.

## Ask before spending more

**Raising effort or upgrading the model costs more** — so when a change would increase
spend (a pricier model, or *higher* effort), propose it in one short line and wait for a
yes before calling. (These are confirmation-gated, so a yes is required anyway; asking
in words makes it smooth.) **Lowering** effort or **reverting** needs no permission and
runs immediately — the system only gates `set_effort` when the new level is *higher*
than the current one, so dialing down (or clearing) never interrupts you.

## Revert when the task is done — `reset_to_default`

A stronger model and high effort are for the **hard stretch**, not the whole chat. As
soon as the demanding task is finished, call **`reset_to_default()`** — it drops both
dials back in one step: the agent's default model at default effort. Don't leave an
expensive model or high effort running for ordinary back-and-forth.

(To clear just one dial: `set_model('default')` reverts the model only;
`set_effort('default')` clears the effort only.)

## How a switch takes effect — timing

The override is written to **this session** and applies on the **next** turn, not the
current one. So if you change a dial and revert it **in the same turn**, only the last
call wins and the conversation never actually runs on the changed setting. To genuinely
run a stretch at high effort or on a stronger model: change the dial, **end your turn**,
do the work over the following turns, then `reset_to_default()`.

## Rules of thumb

- Unsure what's available → **`list_models()`** first, then switch by capability.
- User named a model → **`set_model('<id>')`**.
- Hard reasoning ahead, weak model → propose, then **`use_premium_model()`**.
- Image work (generate/see images) → **`list_models()`**, pick the one badged
  `makes_images` / `sees_images`, then **`set_model('<id>')`**.
- Hard reasoning ahead, model is fine → propose, then **`set_effort('high')`**.
- Light/mechanical work → **`set_effort('low')`** (or `'minimal'`) for speed.
- Hard part done / back to small talk → **`reset_to_default()`**.
- Already strong / none configured → the tool tells you; don't force it.

---

## Planning Mode — Model Strategy

When the user sets execution mode to `'plan'`, follow this model-switching protocol:

### Phase 0 — Load the tools
First, load the Model Switcher tools you'll need: `load_tool("list_models")`, `load_tool("use_premium_model")`, `load_tool("set_effort")`, `load_tool("reset_to_default")`, `load_tool("set_model")`. These are all load-on-demand. Get them ready before you start planning.

### Phase 1 — Draft the plan (standard model)
- Start on your **default model** at **default effort**.
- Do the initial planning work — gather information, analyze requirements, outline options.
- Keep the cheap model for this phase; don't burn premium resources on early exploration.

### Phase 2 — Assess if premium is warranted
Once you have a solid draft, judge the task's complexity:

**Use premium for the final plan if the task involves:**
- Complex multi-step architecture or system design
- Tricky debugging with many interdependent variables
- High-stakes code changes (production, security, data integrity)
- Ambiguous requirements needing careful trade-off analysis
- Large-scale orchestration (multiple agents, complex automation)

**Stick with standard model if:**
- The task is straightforward (simple edits, routine queries, known patterns)
- The requirements are clear and the solution path is obvious
- It's a small, contained change

### Phase 3 — Polish with the right model
- If premium is warranted → propose it: *"This plan is complex enough to warrant the premium model for the final pass — ready to switch?"* → on approval, call `use_premium_model()` and optionally `set_effort('high')`, then finalize the plan.
- If standard is fine → stay on standard, optionally dial `set_effort('medium')` for a bit more polish without the cost.

### Phase 4 — Deliver the plan with a model recommendation
When presenting the final plan, include a **one-line recommendation** at the end:

> **Model recommendation:** This task is [simple / moderately complex / complex]. I recommend [standard / premium] model for execution.

This gives the user a clear signal before they approve moving forward.

### Phase 5 — Revert
After the plan is delivered, call `reset_to_default()` if you upgraded, so the chat doesn't burn expensive resources during follow-up discussion.
