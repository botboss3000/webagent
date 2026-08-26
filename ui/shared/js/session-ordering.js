'use strict';

// Recent sessions should feel recent without letting every individual message
// reshuffle the picker. Time advances in broad buckets; activity only changes
// tiers when the number of user turns doubles. The activity bonus is capped
// below one recency bucket, so a genuinely newer bucket always wins.
const RECENCY_BUCKET_MS = 6 * 60 * 60 * 1000;
const ACTIVITY_TIER_MS = 30 * 60 * 1000;
const MAX_ACTIVITY_TIER = 8;

function _sessionActivityTime(session) {
  return Date.parse(
    session?.activity_at || session?.last_active
      || session?.updated_at || session?.created_at || 0,
  ) || 0;
}

function _sessionActivityCount(session) {
  const value = Number(
    session?.activity_count ?? session?.turn_count ?? session?.interaction_count ?? 0,
  );
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/**
 * Deterministic engagement-adjusted recency rank for an unpinned session.
 *
 * Exact timestamps are bucketed to keep the top of the list stable. A session
 * gets a small bonus for repeated use, based on log2(user turns + 1), so one
 * extra message rarely changes its position. The bonus can never outweigh a
 * session from a newer six-hour bucket.
 */
export function sessionRecentActivityRank(session) {
  const recencyBucket = Math.floor(_sessionActivityTime(session) / RECENCY_BUCKET_MS)
    * RECENCY_BUCKET_MS;
  const tier = Math.min(
    MAX_ACTIVITY_TIER,
    Math.floor(Math.log2(_sessionActivityCount(session) + 1)),
  );
  return recencyBucket + tier * ACTIVITY_TIER_MS;
}

export function compareSessionsByRecentActivity(a, b) {
  return sessionRecentActivityRank(b) - sessionRecentActivityRank(a)
    || String(a?.id || '').localeCompare(String(b?.id || ''));
}
