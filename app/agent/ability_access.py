"""Per-ability caller-access gating.

Each agent may mark any of its enabled abilities with a required **caller-access
level** — ``everyone`` (default), ``registered``, or ``admin`` — stored per agent
in ``agents.metadata.ability_access`` (see ``db.get_agent_ability_access``). This
module turns that map plus the live caller's identity into the subset of
abilities the caller is actually allowed to trigger.

It is enforced at **tool-assembly time** (``app/tools/loader.load_tools``) so the
gated ability's tools are never materialized for a caller below the rung — a real
boundary, not a prompt instruction the model could ignore — and again on the
prompt's ability-bundled skills (``app/agent/prompts.append_skills_section``) so
the agent isn't even told about tools it doesn't have this turn.

The default level is ``everyone``, and the access map only ever stores genuine
restrictions, so an agent with no level set behaves exactly as before.

Fully guarded throughout:
  * caller ranking fails **down** (any error → the lowest, anonymous, rank) so a
    glitch can never escalate privileges; and
  * the filter fails **open** (any error reading the per-agent map → no
    restriction applied) which is the same as the default state, so a transient
    read error never bricks an agent's tools.
"""

from __future__ import annotations

import logging
from typing import Optional, Set

from app.tools.tool_modes import (
    ACCESS_RANK,
    resolve_ability_access,
)

logger = logging.getLogger(__name__)


def _is_anonymous_id(user_id: Optional[str]) -> bool:
    """A not-signed-in guest. The canonical marker is the ``anon_`` id prefix
    minted by the guest-login path (open registration)."""
    return (not user_id) or str(user_id).startswith("anon_")


async def caller_access_rank(db, user_id: Optional[str]) -> int:
    """Rank the live caller on the access ladder: 3 = admin, 2 = registered
    (signed-in, non-anonymous), 1 = everyone (anonymous guest / unknown).

    Mirrors how ``app/api/chat._enforce_agent_access_policy`` distinguishes the
    tiers (global-admin check, then the channel identity's ``user_tier``). Fails
    **down** — any error yields the lowest rank, never an escalation."""
    # Admin is the top rung and is checked robustly first.
    try:
        if user_id and await db.is_user_admin(user_id):
            return ACCESS_RANK["admin"]
    except Exception as e:
        logger.debug("caller_access_rank admin check failed (treating as non-admin): %s", e)

    # An anonymous guest is the lowest rung.
    if _is_anonymous_id(user_id):
        return ACCESS_RANK["everyone"]

    # Otherwise consult the channel identity's tier as a secondary signal; a
    # missing/unknown tier on a non-anon id is treated as a registered account.
    try:
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT user_tier FROM channel_identities WHERE user_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        tier = (row["user_tier"] if row else None) or ""
        if str(tier).strip().lower() == "anonymous":
            return ACCESS_RANK["everyone"]
    except Exception as e:
        logger.debug("caller_access_rank tier check failed (treating as registered): %s", e)

    return ACCESS_RANK["registered"]


async def filter_abilities_for_caller(
    agent_id: str,
    enabled: Set[str],
    user_id: Optional[str],
    db=None,
) -> Set[str]:
    """Drop from ``enabled`` any ability whose required caller-access level the
    live caller doesn't meet. Returns the kept subset (a new set).

    No-op fast paths: empty input, no agent, or the agent has no restrictions
    configured. Fails **open** — any error returns ``enabled`` unchanged, which
    matches the everyone-by-default behaviour."""
    if not enabled or not agent_id:
        return enabled
    try:
        if db is None:
            from app.db import get_db
            db = get_db()
        access_map = await db.get_agent_ability_access(agent_id)
        if not access_map:
            return enabled  # nothing restricted → every caller keeps everything
        rank = await caller_access_rank(db, user_id)
        kept = {
            a for a in enabled
            if ACCESS_RANK.get(resolve_ability_access(a, access_map), 1) <= rank
        }
        if len(kept) != len(enabled):
            logger.debug(
                "ability access gate: caller rank %s on agent %s dropped %s",
                rank, agent_id, sorted(set(enabled) - kept),
            )
        return kept
    except Exception as e:
        logger.warning("filter_abilities_for_caller failed (allowing all): %s", e)
        return enabled
