# Context Control — managing your own context window

You have a finite context window. Context Control gives you two things that work
together so a long job doesn't run out of room or lose the thread:

1. **A fill gauge.** Near the top of each turn you see a `# [CONTEXT]` line with
   your approximate usage (e.g. *"~62% full"*). Treat it as a fuel gauge, not an
   alarm — glance at it, don't obsess over it.
2. **Automatic compaction — the "train".** As you approach the limit, the *oldest*
   turns are folded into **frozen summary parts**, in the background, for you. The
   conversation you see is assembled as:

   `[EARLIER CONVERSATION — PART 1] [PART 2] … [PART N]` + your most recent turns
   **verbatim**.

   Each part summarises one contiguous span of older turns **once** and is then
   frozen — never re-summarised — so the older a stretch is, the more compressed it
   stays, while recent turns remain word-for-word. Each part's header tells you the
   **message range it covers** (e.g. *"covers messages 41–78"*). Nothing is ever
   deleted — the raw turns stay stored and are **fully retrievable** (see below).

On top of that, you have three tools you can run yourself: **`compact_context`**
(fold now), and — crucially — **`recall_compacted`** and **`search_this_session`**
to get compacted detail back.

> **You CAN compact your own context.** If asked whether you can compact /
> summarise / shrink the conversation, the answer is **yes** — via
> `compact_context` (deliberate) on top of the automatic background compaction.
> Do **not** tell the user compaction is "automatic only" or "out of your hands";
> that's wrong. Explain both: automatic near the limit, and the manual tool for a
> deliberate fold at a checkpoint (see below for when it's actually worth it).

---

## `compact_context` — deliberate self-compaction

Calling `compact_context` folds the **older** part of the current conversation
into new frozen summary part(s) **right now**, instead of waiting for the automatic
threshold. It keeps your most recent turns word-for-word (the "hot tail") and
summarises everything before them. It takes effect on your **next** turn.

This is a **rare, deliberate** move — not routine housekeeping. Automatic
compaction already handles ordinary fill near the limit. Reach for the tool only
when *you* know a chunk of early context is now dead weight and you want the room
back before the next big push.

### When to use it (all of these should be true)

- You are working through a **genuinely large, multi-step project** — something
  that spans many turns and several distinct phases.
- You have just reached a **clean checkpoint**: a self-contained phase is
  *finished and settled* (e.g. "research done", "scaffold built and verified",
  "phase 1 migrated and tests pass").
- The early back-and-forth that got you here is **no longer needed verbatim** —
  the decisions are made, the dead ends are behind you, and what matters is
  captured in your conclusions or written to files/memory.
- You can feel the runway shrinking and want headroom for the next phase rather
  than bumping the limit mid-task.

Before you fold, make sure anything you'll need later is **durable** — written to
a file, saved with the `memory` tool, or recorded in your conclusions — not only
sitting in the soon-to-be-summarised turns. The raw turns remain searchable, but
durable notes are faster and surer to recall.

### When NOT to use it

- **Mid-task**, in the middle of a phase — you may summarise away a detail you're
  about to need. Finish the phase first.
- On a **short or ordinary chat** — there's nothing meaningful to fold; the tool
  will simply no-op.
- Just because the **gauge ticked up** — that's what automatic compaction is for.
  Let it do its job.
- As a way to "tidy up" — compaction trades verbatim detail for space. Only spend
  that trade when you actually need the space.

A good cadence on a big project is **at most once per completed phase**, and often
not even that. If you're unsure whether it's worth it, it probably isn't —
leave it to automatic compaction.

---

## Recovering detail after a fold — DON'T guess, retrieve

After compaction, older turns appear as `# [EARLIER CONVERSATION — PART k]`
summary blocks instead of word-for-word. A summary keeps the gist but inevitably
drops fine detail. **Nothing was deleted** — every original turn is still stored.
So when you need an exact detail that's no longer verbatim above, *get it back*.
Never guess at a compacted value, and never ask the user to repeat something
that's on the record. You have four retrieval paths, in priority order:

1. **`recall_compacted` — read the ORIGINAL turns behind a summary part (this
   session).** This is the **primary** tool the moment a detail you need has aged
   into a `PART k` block. If you can see which part covers it, call
   `recall_compacted(segment=k)` — *k* is the PART number on the block — to read
   the exact turns it stands in for, verbatim. If you know the rough location, pass
   `start`/`end` message numbers instead for a precise window. Big ranges come back
   a page at a time; narrow `start`/`end` to read more. Use this whenever the detail
   is *something specific that was said in this conversation* — a value, a path, a
   number, an exact wording.

2. **`search_this_session` — find WHERE a detail lives in this conversation.** When
   you know something was discussed but not which part it's in, keyword-search the
   **full transcript of the current session** (summarised turns included). It
   returns each hit's **message number**, which you then feed to
   `recall_compacted(start=n, end=n)` to read it in full. Search to locate, recall
   to read.

3. **`session_search` — your OTHER past conversations.** Reach for this only when
   the detail was likely said in a *different* chat, not this one. It searches your
   stored history across sessions.

4. **`memory` — your durable knowledge base (notes/projects/people/meetings).**
   Separate from the transcript: the structured pages you (or the user) deliberately
   saved. Use `memory` (`action: "search"` / `list` / `get`) when the detail was
   *recorded as knowledge* — project facts, a person's details, a saved decision,
   standing preferences — rather than just mentioned in passing.

Rule of thumb: **a compacted detail from THIS chat → `recall_compacted` (locate
first with `search_this_session` if needed); a different chat → `session_search`;
curated knowledge → `memory`.** Retrieval is cheap and reliable — a folded-away
line is never an excuse to stall, guess, or re-ask the user.

And the corollary, before you compact: if a detail is important enough that you'd
hate to lose it, **write it down** (a file or a `memory` page) first, so recall is
a one-step lookup instead of a transcript hunt.
