"""Context compaction — the "compaction train" of frozen summary cars.

Part 2 of the Context Control ability. The conversation is assembled as an ordered
list of FROZEN summary "cars" followed by a verbatim "hot tail":

    [car 1][car 2] … [car N]  +  [verbatim most-recent turns]

Each car summarises ONE contiguous span of raw turns exactly once (~a segment's
worth of source tokens compressed to a small target) and is then NEVER
re-summarised. As the chat grows past the limit, the oldest turns that have aged
past the hot-tail budget are peeled off and turned into one or more new cars — the
train gains a car, it never rewrites the existing ones. This avoids the old
single-rolling-summary failure mode (re-compressing already-summarised text every
pass: lossy drift, repeated cost, and a summary prefix that changed every turn and
broke prompt-caching).

The ONE exception to "never re-summarise" is the **far-back merge**: once the train
has more than ``max_cars`` cars, the oldest few are folded into a single coarser
``tier=1`` block so the train's own size stays bounded over very long sessions.

How it fits together:
  * **Active side (here):** ``maybe_compact`` measures the current context and, if
    over threshold, peels the aged span into new frozen cars (and merges the far
    back if needed), persisting the whole train to ``session_summary_segments``.
  * **Passive side (session_history.build_openai_history_from_session):** on every
    turn it reads the train and assembles ``[car 1]…[car N] + [verbatim tail]``.

Each car records the half-open ``[start_index, end_index)`` range of raw turns it
covers (in ``created_at`` order, the same order ``fetch_interactions`` returns).
The raw turns are NEVER deleted — they stay in ``interactions`` and stay fully
retrievable verbatim by range (``recall_compacted``) or keyword (``search_this_session``).
Cuts only ever land on a **user-turn boundary**, so the verbatim tail and every car
start cleanly (never an orphan tool result).

Failure-safe: any error (LLM down, fetch error) makes this a no-op — the turn
proceeds with whatever context it already had.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from app.agent.context_control import estimate_tokens
from app.agent.session_history import (
    interactions_to_openai_messages,
    INTERNAL_TOOL_NAMES,
    TUNNEL_SOURCE,
    strip_think_blocks,
)

logger = logging.getLogger(__name__)

# Cap the summariser's input so a giant aged-out span doesn't blow up one call.
# Sized to hold a full ~50k-token segment (≈4 chars/token) with headroom, since a
# car now summarises one whole segment rather than a small tail slice.
_MAX_TRANSCRIPT_CHARS = 240_000
_MAX_MSG_CHARS = 1_500

# Segment-train defaults (overridable via Context Control settings).
_DEFAULT_SEGMENT_SOURCE_TOKENS = 50_000   # raw turns peeled into one car
_DEFAULT_SEGMENT_TARGET_TOKENS = 10_000   # compressed size of one car
_DEFAULT_MAX_CARS = 12                     # far-back merge trigger
_CHARS_PER_TOKEN = 4                       # rough text→token ratio for car sizes

def render_segment_message(seg: Dict[str, Any], part: int, total: int) -> Dict[str, Any]:
    """Render one frozen car as a system message for the assembled context.

    ``part`` is the 1-based position shown to the agent (and the handle it passes
    to ``recall_compacted``). The block names the message range it covers and tells
    the agent exactly how to pull the originals back if it needs a missing detail.
    """
    topic = (seg.get("topic") or "").strip()
    start = int(seg.get("start_index") or 0)
    end = int(seg.get("end_index") or 0)
    coarse = " (coarse — oldest history)" if int(seg.get("tier") or 0) == 1 else ""
    head = f"# [EARLIER CONVERSATION — PART {part} OF {total}{coarse}]"
    meta = f"covers messages {start + 1}–{end}"
    if topic:
        meta += f"; topic: {topic}"
    hint = (
        f"Nothing here was deleted. If you need a detail this summary does not "
        f"capture, call recall_compacted(segment={part}) to read messages "
        f"{start + 1}–{end} verbatim, or search_this_session(query=…) to keyword-"
        f"search the full transcript of THIS conversation."
    )
    return {
        "role": "system",
        "content": f"{head}\n({meta})\n\n{(seg.get('summary') or '').strip()}\n\n{hint}",
    }


def _text_tokens(text: str) -> int:
    """Rough token estimate for a piece of summary text."""
    return max(0, len(text or "") // _CHARS_PER_TOKEN)


def _span_tokens(rows_slice: List[Any]) -> int:
    """Token estimate of a slice of raw interaction rows as the model would see it."""
    return estimate_tokens(interactions_to_openai_messages(rows_slice))


# Reasoning effort for the summariser call. Compaction is a cheap background
# squeeze, so it always runs at a FAST/low effort regardless of the chat's effort —
# overridable via COMPACT_EFFORT (e.g. "minimal"/"low"/""=none). A model that does
# not support the hint just has it dropped and the call retried (see _summarise_json).
_COMPACT_EFFORT = (os.environ.get("COMPACT_EFFORT", "low") or "").strip().lower() or None


def _summary_client(cfg: Dict[str, Any]):
    """Lazy LLM client for summarisation, pointed at the resolved provider."""
    try:
        from openai import AsyncOpenAI
    except ImportError:  # pragma: no cover
        from app.openai_compat import AsyncOpenAI
    return AsyncOpenAI(
        base_url=cfg.get("base_url") or "",
        api_key=cfg.get("api_key") or "",
        timeout=60.0,
    )


async def _resolve_summariser(
    user_id: str, session_id: str, settings: Dict[str, Any]
) -> Dict[str, Any]:
    """Decide which model + provider compaction calls, and at what effort.

    Model order: an explicit Context Control "Summary Model" override, else the app's
    DEFAULT text model — resolved exactly as a run would with NO agent/session
    override, so compaction always rides the configured default brain and never a
    session's switched (possibly expensive) model. The provider key + base URL come
    from the same resolved user config. A fast reasoning-effort hint is attached so
    compaction stays cheap. Env vars are the last-resort fallback if admin resolution
    is unavailable (e.g. app/admin/ removed)."""
    model = (settings.get("summary_model") or "").strip()
    base_url = ""
    api_key = ""
    provider = ""
    try:
        from app.admin.settings import resolve_active_model, _resolve_user_config
        ucfg = await _resolve_user_config(user_id) if user_id else {}
        api_key = (ucfg or {}).get("api_key") or ""
        base_url = (ucfg or {}).get("base_url") or ""
        provider = (ucfg or {}).get("provider") or ""
        if not model and user_id:
            active = await resolve_active_model(user_id)  # default text model
            model = (active or {}).get("model") or ""
            base_url = (active or {}).get("base_url") or base_url
            provider = (active or {}).get("provider") or provider
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("compaction: default-model resolve failed: %s", e)
    model = (model
             or os.environ.get("COMPACT_MODEL") or os.environ.get("LLM_MODEL")
             or os.environ.get("OPENROUTER_MODEL") or "")
    base_url = (base_url
                or os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL"))
    api_key = (api_key
               or os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "")
    # Fast mode "if the model provider allows it": only attach the reasoning-effort
    # hint when the catalog says this model supports reasoning. A model explicitly
    # marked non-reasoning gets no hint (skips a guaranteed reject+retry); an unknown
    # model still gets it, with _summarise_json's drop-and-retry as the safety net.
    effort = _COMPACT_EFFORT
    if effort:
        try:
            from app import model_catalog
            entry = model_catalog.lookup(model, provider or "") or {}
            if entry.get("reasoning") is False:
                effort = None
        except Exception:
            pass
    return {"model": model, "base_url": base_url, "api_key": api_key, "effort": effort}


def _render_transcript(rows: List[Any]) -> str:
    """Render aged-out interaction rows as plain text for the summariser.

    Skips internal pipeline tools and terminal-tunnel traffic (already excluded
    from agent context); truncates each message and the whole transcript.
    """
    parts: List[str] = []
    for r in rows:
        if r.role == "tool" and r.tool_name in INTERNAL_TOOL_NAMES:
            continue
        if getattr(r, "source", None) == TUNNEL_SOURCE:
            continue
        content = strip_think_blocks(r.content or "")
        if not content.strip() and r.role != "tool":
            continue
        if len(content) > _MAX_MSG_CHARS:
            content = content[:_MAX_MSG_CHARS] + " …[truncated]"
        if r.role == "user":
            parts.append(f"USER: {content}")
        elif r.role == "assistant":
            parts.append(f"ASSISTANT: {content}")
        elif r.role == "tool":
            parts.append(f"TOOL[{r.tool_name or '?'}]: {content}")
    text = "\n".join(parts)
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        # Keep the most recent portion of the aged span (older context is the
        # part most safely lossy; the raw rows remain in the DB regardless).
        text = "…[earlier turns truncated]\n" + text[-_MAX_TRANSCRIPT_CHARS:]
    return text


async def _record_usage(resp: Any, cfg: Dict[str, Any]) -> None:
    """Book the summariser's tokens against background usage (best-effort)."""
    try:
        _u = getattr(resp, "usage", None)
        if _u:
            from plugins.billing.usage import record_background_usage
            await record_background_usage(
                model=cfg.get("model") or "",
                input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                label="compact",
            )
    except Exception:
        pass


def _parse_summary_json(raw: str) -> tuple:
    """Pull ``(summary, topic)`` out of the summariser's reply.

    The model is asked for ``{"topic": …, "summary": …}`` but providers wrap it in
    prose or code fences, so we try the whole string and the outermost {...} span,
    then fall back to treating the entire reply as the summary with no topic."""
    if not raw:
        return (None, "")
    candidates = [raw]
    a, b = raw.find("{"), raw.rfind("}")
    if a != -1 and b > a:
        candidates.append(raw[a:b + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if isinstance(obj, dict) and (obj.get("summary") or "").strip():
            return ((obj.get("summary") or "").strip(), (obj.get("topic") or "").strip())
    return (raw.strip() or None, "")


async def _summarise_json(sys_prompt: str, user_prompt: str, cfg: Dict[str, Any]) -> tuple:
    """Call the summariser and return ``(summary, topic)`` — ``(None, "")`` on failure.

    Sends the fast reasoning-effort hint via ``extra_body.reasoning.effort``; a model
    that rejects it (or the level) gets the call retried once without it, mirroring the
    main loop, so an unsupported effort never breaks compaction."""
    try:
        client = _summary_client(cfg)
        kwargs = dict(
            model=cfg.get("model") or "",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        if cfg.get("effort"):
            kwargs["extra_body"] = {"reasoning": {"effort": cfg["effort"]}}
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e_eff:
            if "extra_body" in kwargs:
                logger.info("compaction: effort '%s' rejected (%s) — retrying without it",
                            cfg.get("effort"), e_eff)
                kwargs.pop("extra_body", None)
                resp = await client.chat.completions.create(**kwargs)
            else:
                raise
        await _record_usage(resp, cfg)
        return _parse_summary_json((resp.choices[0].message.content or "").strip())
    except Exception as e:
        logger.warning("compaction: summariser call failed: %s", e)
        return (None, "")


async def _summarise_segment(
    transcript: str, settings: Dict[str, Any], cfg: Dict[str, Any],
    target_tokens: Optional[int] = None,
) -> tuple:
    """Compress ONE contiguous span of raw turns into a frozen car (summary, topic).

    ``target_tokens`` overrides the per-car size budget. Automatic compaction leaves
    it None (uses the window-proportional ``segment_target_tokens``); the manual
    ``/compact`` path passes a size derived from the source span so the configured
    source→target *ratio* (e.g. 0.25→0.05 = 5:1) is honoured even on a small session,
    where the absolute window fraction would be larger than the content itself."""
    target = int(target_tokens if target_tokens else (
        settings.get("segment_target_tokens") or _DEFAULT_SEGMENT_TARGET_TOKENS))
    sys_prompt = (
        "You are compressing ONE contiguous excerpt from the middle of a long "
        "agent/user conversation into a faithful, self-contained synopsis that will "
        "REPLACE those exact turns in the model's context window. Preserve everything "
        "a continuation might need: decisions made, concrete facts and values, file "
        "paths, identifiers, numbers, user preferences and constraints, unresolved or "
        "open threads, and the outcomes of any tool calls. Drop only chit-chat and "
        "redundancy. Never invent anything not in the excerpt. Write compact prose or "
        f"bullet points, roughly {target} tokens or fewer. Respond with ONLY a JSON "
        'object: {"topic": "<=8 word label of what this span was about", "summary": "…"}.'
    )
    return await _summarise_json(sys_prompt, f"EXCERPT:\n{transcript}\n\nJSON:", cfg)


async def _merge_segments_text(
    batch: List[Dict[str, Any]], settings: Dict[str, Any], cfg: Dict[str, Any]
) -> tuple:
    """Fold several already-summarised cars into one coarser (tier=1) block."""
    target = int(settings.get("segment_target_tokens") or _DEFAULT_SEGMENT_TARGET_TOKENS)
    joined = "\n\n".join(
        f"[messages {int(c.get('start_index') or 0) + 1}-{int(c.get('end_index') or 0)}"
        + (f", topic: {c.get('topic')}" if c.get('topic') else "") + "]\n"
        + (c.get("summary") or "")
        for c in batch
    )
    sys_prompt = (
        "Combine these already-condensed earlier sections of ONE conversation into a "
        "single coarser summary. Keep the durable, still-relevant facts, decisions, "
        "and open threads; it is acceptable to lose fine detail here (the raw "
        "transcript stays retrievable). Do not invent. Write roughly "
        f"{target} tokens or fewer. Respond with ONLY a JSON object: "
        '{"topic": "<=8 word label", "summary": "…"}.'
    )
    return await _summarise_json(sys_prompt, f"SECTIONS:\n{joined}\n\nJSON:", cfg)


async def load_segments(
    db: Any, user_id: str, session_id: str,
    rows: Optional[List[Any]] = None, *, total_count: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return the session's frozen train as a list of car dicts (seq asc).

    Back-compat shim: a session compacted under the OLD single-rolling-summary
    scheme has no segment rows — surface its one summary as a single tier=1 car so
    the read/write sides see a uniform train and never lose that history."""
    try:
        segs = await db.get_session_segments(user_id, session_id)
    except Exception as e:
        logger.debug("compaction: get_session_segments failed: %s", e)
        segs = []
    if segs:
        return [dict(s) for s in segs]
    # Legacy single-summary → one merged-style car covering [0, covered_count).
    try:
        legacy = await db.get_session_summary(user_id, session_id)
    except Exception:
        legacy = None
    if legacy:
        if total_count is None:
            total_count = len(rows or [])
        cov = max(0, min(int(legacy.get("covered_count") or 0), total_count))
        txt = (legacy.get("summary") or "").strip()
        if cov > 0 and txt:
            return [{
                "seq": 0, "start_index": 0, "end_index": cov,
                "summary": txt, "token_estimate": _text_tokens(txt),
                "topic": "Earlier conversation", "tier": 1,
            }]
    return []


async def _maybe_merge_far_back(
    train: List[Dict[str, Any]], settings: Dict[str, Any], max_cars: int,
    cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Bound the train: once it exceeds ``max_cars`` cars, keep the newest
    ``max_cars - 1`` verbatim and fold ALL older cars into one coarser tier=1
    block — a single call that guarantees the result is at most ``max_cars`` cars,
    even when one compaction added several cars at once. Failure-safe (leaves the
    train unchanged if the merge summariser fails)."""
    if max_cars <= 1 or len(train) <= max_cars:
        return train
    keep = max_cars - 1
    batch = train[:len(train) - keep]   # the oldest excess, >= 2 cars
    rest = train[len(train) - keep:]    # newest cars, kept as-is
    if len(batch) < 2:
        return train
    summary, topic = await _merge_segments_text(batch, settings, cfg)
    if not summary:
        return train  # failure-safe
    merged = {
        "seq": 0,
        "start_index": int(batch[0].get("start_index") or 0),
        "end_index": int(batch[-1].get("end_index") or 0),
        "summary": summary,
        "token_estimate": _text_tokens(summary),
        "topic": topic or "Ancient history",
        "tier": 1,
    }
    return [merged] + rest


async def maybe_compact(
    db: Any, user_id: str, session_id: str, settings: Dict[str, Any],
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """Grow the compaction train if context exceeds the threshold. No-op otherwise.

    Peels the oldest turns that have aged past the verbatim hot-tail budget and
    turns them into one or more new FROZEN cars (existing cars are never rewritten,
    except the far-back merge when the train gets too long). Returns a small info
    dict when the train changed, else None.

    ``force=True`` is the agent-initiated path (the ``compact_context`` tool): it
    peels now regardless of how full the context is, and even when automatic
    background compaction is switched off. The ability must still be enabled, and
    the same safe boundary rules apply — a verbatim hot tail is always preserved and
    cuts only land on a user-turn boundary. When the conversation is already small
    enough that there is nothing older than the tail to fold, even a forced call is a
    no-op and returns None.
    """
    if not settings.get("enabled"):
        return None
    if not force and not settings.get("compaction_enabled", True):
        return None
    limit = int(settings.get("token_limit") or 0)
    if limit <= 0:
        return None
    threshold = float(settings.get("compact_threshold", 0.85))
    tail_fraction = float(settings.get("tail_fraction", 0.30))
    seg_source = int(settings.get("segment_source_tokens") or _DEFAULT_SEGMENT_SOURCE_TOKENS)
    max_cars = int(settings.get("max_cars") or _DEFAULT_MAX_CARS)

    # Existing summary cars define an absolute row boundary. Read only the hot
    # suffix beyond it; the raw summarized prefix may contain millions of tokens
    # and must not re-enter Python on every automatic-compaction check.
    try:
        n = await db.count_interactions(user_id, session_id)
    except Exception as e:
        logger.warning("compaction: count_interactions failed: %s", e)
        return None
    if n <= 0:
        return None

    segments = await load_segments(
        db, user_id, session_id, total_count=n)
    covered = max((int(s.get("end_index") or 0) for s in segments), default=0)
    covered = max(0, min(covered, n))
    try:
        rows = await db.fetch_interactions_from_offset(
            user_id, session_id, covered)
    except Exception as e:
        logger.warning("compaction: fetch_interactions_from_offset failed: %s", e)
        return None
    if not rows:
        return None

    # Current effective context = frozen cars + verbatim tail.
    cars_tokens = sum(int(s.get("token_estimate") or 0) for s in segments)
    tail_tokens = _span_tokens(rows)
    cur_tokens = cars_tokens + tail_tokens
    if not force and cur_tokens <= int(threshold * limit):
        return None  # not full enough (forced calls skip this gate)

    # The verbatim "hot tail" budget. Two different bases:
    #   * Automatic compaction (force=False): a fraction of the model WINDOW — the
    #     point is to keep the window from overflowing, so 0.30 means "keep the most
    #     recent ~30% of the window hot".
    #   * Manual /compact + the agent's compact_context tool (force=True): a fraction
    #     of the CURRENT (not-yet-compacted) conversation, NOT the window — so a
    #     deliberate compaction always folds the older part even on a small session.
    #     0.30 then means "keep the most recent 30% of this conversation verbatim,
    #     fold the older 70%", which is what a user expects from /compact.
    if force:
        tail_budget = int(tail_fraction * tail_tokens)
    else:
        tail_budget = int(tail_fraction * limit)
    # Ratio used to size each manual car's summary from its source span (honours the
    # configured source→target compression even when the window fraction wouldn't).
    _src_tok = int(settings.get("segment_source_tokens") or _DEFAULT_SEGMENT_SOURCE_TOKENS)
    _tgt_tok = int(settings.get("segment_target_tokens") or _DEFAULT_SEGMENT_TARGET_TOKENS)
    _compress_ratio = (_tgt_tok / _src_tok) if _src_tok > 0 else 0.2
    # New high-water cut: the largest verbatim tail that still fits the tail budget,
    # snapped to a user-turn boundary beyond what's already covered.
    # Boundaries remain absolute offsets in persisted segment metadata, while
    # ``rows`` is a zero-based view of the uncovered suffix.
    boundaries = [
        covered + i for i, row in enumerate(rows)
        if i > 0 and row.role == "user"
    ]
    if not boundaries:
        return None  # no new user-turn boundary to fold to
    cut: Optional[int] = None
    for idx in reversed(boundaries):  # newest -> oldest
        if _span_tokens(rows[idx - covered:]) <= tail_budget:
            cut = idx
        else:
            break
    if cut is None:
        cut = boundaries[-1]  # even the latest turn exceeds the tail budget
    if cut <= covered:
        return None

    # Resolve the summariser (default text model + provider key + fast effort) once
    # for every car/merge call this pass makes.
    summariser = await _resolve_summariser(user_id, session_id, settings)

    # Split [covered, cut) into ~seg_source chunks on user boundaries — one car each.
    inner = [b for b in boundaries if covered < b < cut]
    new_cars: List[Dict[str, Any]] = []
    seg_seq = len(segments)
    acc_start = covered
    while acc_start < cut:
        cend = cut
        for b in inner:
            if b <= acc_start:
                continue
            if _span_tokens(rows[acc_start - covered:b - covered]) >= seg_source:
                cend = b
                break
        span = rows[acc_start - covered:cend - covered]
        if not span:
            break
        transcript = _render_transcript(span)
        if not transcript.strip():
            acc_start = cend
            continue
        # Manual compaction sizes each car from its source span (ratio-based) so the
        # configured compression holds on any session size; automatic uses the
        # window-proportional default (target_tokens=None).
        car_target = None
        if force:
            car_target = max(200, int(_compress_ratio * _span_tokens(span)))
        summary, topic = await _summarise_segment(
            transcript, settings, summariser, target_tokens=car_target)
        if not summary:
            break  # failure-safe: keep cars made so far, leave the rest verbatim
        new_cars.append({
            "seq": seg_seq, "start_index": acc_start, "end_index": cend,
            "summary": summary, "token_estimate": _text_tokens(summary),
            "topic": topic, "tier": 0,
        })
        seg_seq += 1
        acc_start = cend

    if not new_cars:
        return None  # nothing summarised (e.g. summariser down) — context unchanged

    train = list(segments) + new_cars
    train = await _maybe_merge_far_back(train, settings, max_cars, summariser)
    for k, s in enumerate(train):  # renumber seq contiguous after any merge
        s["seq"] = k

    try:
        await db.replace_session_segments(user_id, session_id, train)
    except Exception as e:
        logger.warning("compaction: replace_session_segments failed: %s", e)
        return None

    new_covered = max(int(s.get("end_index") or 0) for s in train)
    cars_after = sum(int(s.get("token_estimate") or 0) for s in train)
    tail_after = _span_tokens(rows[max(0, new_covered - covered):])
    tokens_after = cars_after + tail_after
    info = {
        "covered_count": new_covered,
        "segments": len(train),
        "new_cars": len(new_cars),
        "summarised_rows": new_covered - covered,
        "tokens_before": cur_tokens,
        "tokens_after": tokens_after,
        "limit": limit,
    }
    logger.info(
        "compaction(train): session %s +%d car(s) → %d total (covered=%d, ~%d tok before)",
        session_id, len(new_cars), len(train), new_covered, cur_tokens,
    )
    return info
