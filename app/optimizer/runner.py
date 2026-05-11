"""
Optimizer Runner — orchestrates the full pipeline.
Two modes:
  - Standard: prefilter → proposer → executor → reviewer → deployer
  - Iterative: loop proposer+executor+reviewer up to max_iterations
    until target criteria is met or budget exhausted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.optimizer.config import load_config, update_state, get_intensity_thresholds
from app.optimizer.prefilter import prefilter
from app.optimizer.planner import propose_improvements
from app.optimizer.worker import run_trials
from app.optimizer.finalizer import review_trials

logger = logging.getLogger(__name__)

_recently_seen: Dict[str, float] = {}
_RECENT_WINDOW_SEC = 60


async def run_optimizer_async(
    user_id: str,
    session_id: str,
    channel: str = "ui",
    criteria: str = "",
    feedback: str = "",
    skill_name: str = "",
) -> Optional[str]:
    """
    If criteria set: ITERATIVE mode — loop until target met or budget exhausted.
    Otherwise: STANDARD single-pass mode.
    """
    now = datetime.now(timezone.utc).isoformat()
    from app.optimizer.templates import seed_optimizer_templates
    seed_optimizer_templates()

    if session_id in _recently_seen:
        elapsed = asyncio.get_event_loop().time() - _recently_seen[session_id]
        if elapsed < _RECENT_WINDOW_SEC and not criteria:
            return None
    _recently_seen[session_id] = asyncio.get_event_loop().time()

    cfg = load_config()
    if cfg.get("mode") != "live" and "manual" not in session_id:
        return None

    max_iter = cfg.get("max_iterations", 3)
    max_turns = cfg.get("max_turns", 10)
    intensity = cfg.get("intensity", 3)
    thresholds = get_intensity_thresholds(intensity)
    intensity_targets = thresholds.get("targets", {})
    
    # Use intensity-mapped target, overridden by manual criteria_targets if set
    manual_targets = cfg.get("criteria_targets", {})
    target = manual_targets.get(criteria, intensity_targets.get(criteria, 0)) if criteria else 0
    target_metric = criteria or cfg.get("target_metric", "failures")
    trials_per_change = 2

    optimizer_session_id = f"optimizer-{session_id}"
    _ensure_session(user_id, optimizer_session_id, session_id)

    run_id = str(uuid.uuid4())
    _log_start(run_id, cfg, optimizer_session_id)

    try:
        pf = await prefilter(user_id, session_id)
        transcript = pf.get("transcript", [])
        baseline = {
            "turns": max(len([t for t in transcript if "[assistant]" in t]), 1),
        }
        if not criteria:
            baseline["tokens"] = max(estimate_tokens(transcript), 100)
            baseline["time_ms"] = max(estimate_time_ms(pf), 1000)

        opportunities = pf.get("opportunities", [])
        _insert_opt_msg(user_id, optimizer_session_id, "assistant", "optimizer:prefilter",
                        f"Prefilter: {len(opportunities)} issues found across session tools. "
                        f"Baseline: {baseline}")

        if criteria and target > 0:
            # ── ITERATIVE MODE ──
            return await _run_iterative(
                user_id, session_id, optimizer_session_id, run_id, cfg,
                pf, baseline, target, criteria, feedback, skill_name,
                max_iter, max_turns, trials_per_change, now,
            )
        else:
            # ── STANDARD MODE ──
            return await _run_standard(
                user_id, session_id, optimizer_session_id, run_id, cfg,
                pf, opportunities, baseline, trials_per_change, now,
            )

    except Exception as e:
        logger.error("Optimizer: pipeline crashed: %s", e, exc_info=True)
        _log_complete(run_id, "failed", cfg, optimizer_session_id, errors=[str(e)])
        return optimizer_session_id


# ── ITERATIVE MODE ──

async def _run_iterative(
    user_id, session_id, opt_sid, run_id, cfg, pf, baseline, target, criteria,
    feedback, skill_name, max_iter, max_turns, trials_per_change, now,
) -> Optional[str]:
    """Loop: propose → test → check target → retry (up to max_iterations)."""
    all_proposals = []
    best_result = None
    best_metric = float("inf")
    current_criteria = criteria or "turns"

    for iteration in range(1, max_iter + 1):
        if iteration > max_turns:
            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:runner",
                            f"Turn budget ({max_turns}) exhausted at iteration {iteration-1}.")
            break

        _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:runner",
                        f"Iteration {iteration}/{max_iter} — targeting {current_criteria} ≤ {target}. "
                        f"Baseline: {baseline.get(current_criteria)}.")

        # Build context from previous failures
        prev_context = ""
        if all_proposals:
            prev_context = f"Previous attempts failed to reach target {current_criteria}≤{target}. "
            prev_context += f"Last attempt: {all_proposals[-1].get('reasoning','')[:200]}"

        # Proposer
        proposal = await propose_improvements(
            user_id=user_id, session_id=session_id, prefilter_data=pf, mode="analyze",
        )
        if not proposal or not proposal.get("changes"):
            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:planner",
                            f"Iteration {iteration}: no changes proposed.")
            continue

        changes = proposal["changes"]
        _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:planner",
                        f"Iteration {iteration}: proposed {len(changes)} change(s). "
                        f"{proposal.get('analysis','')[:200]}")

        # Executor (parallel)
        trials = await run_trials(user_id, changes, pf.get("transcript", []), trials_per_change)
        confident = [t for t in trials if t.get("averaged", {}).get("confidence", 0) >= 0.5]
        if not confident:
            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:worker",
                            f"Iteration {iteration}: all trials low confidence.")
            continue

        # Reviewer — check against target
        review = await review_trials(user_id, confident, baseline, transcript=pf.get("transcript", []), criteria=criteria, target=target, skill_state=pf.get("skill_state", []))
        winners = review.get("winners", [])
        
        _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer",
                        f"Iteration {iteration}: {len(winners)} winners reviewed. "
                        f"{review.get('summary','')[:200]}")

        # Check if any winner meets the target
        target_met = False
        for w in winners:
            delta = w.get("delta", {})
            current_val = baseline.get(current_criteria, 0)
            pct = delta.get(f"{current_criteria}_pct", 0)
            estimated = current_val * (1 + pct / 100)  # pct is negative for improvement
            
            if estimated <= target:
                target_met = True
                if estimated < best_metric:
                    best_metric = estimated
                    best_result = w
                
                # Deploy the winner
                for ch in changes:
                    if ch.get("element") == w.get("element"):
                        _deploy_change(user_id, opt_sid, ch, w)
                
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer",
                                f"Target met! {current_criteria}: {baseline.get(current_criteria)}→{estimated:.0f} "
                                f"(target ≤{target}). Deployed.")
                break
        
        if target_met:
            break
        
        all_proposals.extend(changes)
        _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:runner",
                        f"Iteration {iteration}: target not met. Best: {best_metric:.0f} (need ≤{target}). Retrying...")

    # Final summary
    old_total = cfg.get("state", {}).get("improvements_deployed", 0)
    total_deployed = 1 if best_result else 0
    summary = (f"Target {'met' if best_result else 'not met'} after {iteration} iterations. "
               f"Best {current_criteria}: {best_metric:.0f}" if best_result else
               f"Could not reach target {current_criteria}≤{target} in {iteration} iterations.")

    _log_complete(run_id, "success", cfg, opt_sid,
                  skills_analyzed=len(pf.get("stats", [])),
                  proposals_generated=len(all_proposals),
                  proposals_deployed=total_deployed,
                  summary=summary)
    update_state(last_run_at=now, last_run_status="success",
                improvements_deployed=old_total + total_deployed)
    return opt_sid


# ── STANDARD MODE ──

async def _run_standard(
    user_id, session_id, opt_sid, run_id, cfg, pf, opportunities,
    baseline, trials_per_change, now,
) -> Optional[str]:
    """Single-pass: prefilter → proposer → executor → reviewer → deployer."""
    
    if not opportunities:
        _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:analyzer",
                        "No optimization opportunities found. All skills are healthy.")
        _log_complete(run_id, "success", cfg, opt_sid,
                     summary="No optimization opportunities found.",
                     skills_analyzed=len(pf.get("stats", [])))
        return opt_sid

    proposal = await propose_improvements(user_id=user_id, session_id=session_id,
                                          prefilter_data=pf, mode="analyze")
    changes = proposal.get("changes", []) if proposal else []
    analysis = proposal.get("analysis", "No analysis") if proposal else "Proposer failed"

    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:planner",
                    f"Analyzed interaction. Proposed {len(changes)} changes.\n\n{analysis}")

    if not changes:
        _log_complete(run_id, "success", cfg, opt_sid,
                     summary="Proposer found no changes to make.", proposals_generated=0)
        return opt_sid

    trials = await run_trials(user_id, changes, pf.get("transcript", []), trials_per_change)
    confident = [t for t in trials if t.get("averaged", {}).get("confidence", 0) >= 0.5]

    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:worker",
                    f"Ran {len(confident)}/{len(trials)} trials with confidence >= 0.5.")

    if not confident:
        _log_complete(run_id, "success", cfg, opt_sid,
                     proposals_generated=len(changes), proposals_deployed=0,
                     summary="All trials had low confidence. No changes deployed.")
        return opt_sid

    review = await review_trials(user_id, confident, baseline, transcript=pf.get("transcript", []), skill_state=pf.get("skill_state", []))
    winners, losers = review.get("winners", []), review.get("losers", [])
    summary = review.get("summary", "No summary")

    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer",
                    f"Compared trials. {len(winners)} winners, {len(losers)} rejected.\n\n{summary}")

    deployed = 0
    for w in winners:
        for ch in changes:
            if ch.get("element") == w.get("element"):
                try:
                    _deploy_change(user_id, opt_sid, ch, w)
                    deployed += 1
                except Exception as e:
                    logger.warning("Deployer: failed for %s: %s", w.get("element"), e)

    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer",
                    f"Deployed {deployed} winning changes.")

    old_total = cfg.get("state", {}).get("improvements_deployed", 0)
    _log_complete(run_id, "success", cfg, opt_sid,
                 skills_analyzed=len(pf.get("stats", [])),
                 proposals_generated=len(changes), proposals_deployed=deployed,
                 summary=f"Deployed {deployed} improvements. {summary}")
    update_state(last_run_at=now, last_run_status="success",
                improvements_deployed=old_total + deployed)
    return opt_sid


# ── Helpers (unchanged) ──

def _retry_db_write(fn, max_attempts=5, delay=0.3):
    import time
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if "database is locked" in str(e).lower() and attempt < max_attempts - 1:
                time.sleep(delay * (attempt + 1))
                continue
            raise

def estimate_tokens(transcript):
    return sum(len(t) for t in transcript) // 3

def estimate_time_ms(pf):
    return 5000  # default estimate — no all-time stats anymore

def _ensure_session(user_id, opt_sid, orig_sid):
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            conn = raw()
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,datetime('now'),datetime('now'))",
                (opt_sid, user_id, f"Optimizer — {orig_sid[:12]}"),
            )
            conn.commit()
            conn.close()
        _retry_db_write(_do)

def _insert_opt_msg(user_id, session_id, role, source, content):
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            conn = raw()
            conn.execute(
                "INSERT INTO interactions (id, session_id, role, content, source, channel, created_at) VALUES (?,?,?,?,?,'optimizer',datetime('now'))",
                (str(uuid.uuid4()), session_id, role, content, source),
            )
            conn.commit()
            conn.close()
        _retry_db_write(_do)

def _log_start(run_id, cfg, session_id):
    _log_run(run_id, "running", cfg, session_id)

def _log_complete(run_id, status, cfg, session_id, **kwargs):
    _log_run(run_id, status, cfg, session_id, **kwargs)

def _log_run(run_id, status, config, session_id,
             skills_analyzed=0, proposals_generated=0, proposals_deployed=0,
             summary="", errors=None):
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if not raw:
            return
        now = datetime.now(timezone.utc).isoformat()
        completed = now if status in ("success","failed") else None
        err_json = json.dumps(errors) if errors else None

        def _do():
            conn = raw()
            conn.execute("""CREATE TABLE IF NOT EXISTS optimizer_runs (
                id TEXT PRIMARY KEY, status TEXT DEFAULT 'running',
                started_at TEXT, completed_at TEXT,
                skills_analyzed INTEGER DEFAULT 0,
                proposals_generated INTEGER DEFAULT 0,
                proposals_deployed INTEGER DEFAULT 0,
                errors TEXT, summary TEXT, config_snapshot TEXT, session_id TEXT
            )""")
            existing = conn.execute("SELECT id FROM optimizer_runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE optimizer_runs SET status=?,completed_at=?,skills_analyzed=?,proposals_generated=?,proposals_deployed=?,errors=?,summary=? WHERE id=?",
                    (status, completed, skills_analyzed, proposals_generated, proposals_deployed, err_json, summary, run_id),
                )
            else:
                conn.execute(
                    "INSERT INTO optimizer_runs (id,status,started_at,completed_at,skills_analyzed,proposals_generated,proposals_deployed,errors,summary,config_snapshot,session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, status, now, completed, skills_analyzed, proposals_generated, proposals_deployed, err_json, summary, json.dumps(config), session_id),
                )
            conn.commit()
            conn.close()
        _retry_db_write(_do)
    except Exception as e:
        logger.warning("_log_run: %s", e)

def _deploy_change(user_id, session_id, change, winner):
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if not raw:
        return
    element = change.get("element", "")
    new_content = change.get("new_content", "")
    element_type = change.get("element_type", "context_document")

    def _do():
        conn = raw()
        if element_type == "context_document":
            agent_row = conn.execute("SELECT id FROM agents WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
            if agent_row:
                agent_id = agent_row[0]
                existing = conn.execute(
                    "SELECT id FROM context_documents WHERE agent_id=? AND title=? LIMIT 1",
                    (agent_id, element),
                ).fetchone()
                if existing:
                    conn.execute("UPDATE context_documents SET content=?, updated_at=datetime('now') WHERE id=?", (new_content, existing[0]))
                else:
                    conn.execute(
                        "INSERT INTO context_documents (id,agent_id,context_type,title,content,tags,created_at,updated_at) VALUES (?,?,'skills',?,?,'[]',datetime('now'),datetime('now'))",
                        (str(uuid.uuid4()), agent_id, element, new_content),
                    )
        elif element_type == "system_prompt":
            conn.execute("UPDATE agents SET system_prompt=?, updated_at=datetime('now') WHERE user_id=?", (new_content, user_id))
        conn.commit()
        _log_improvement(conn, element, change, winner.get("delta", {}))
        conn.close()
    try:
        _retry_db_write(_do)
    except Exception as e:
        logger.warning("_deploy_change: %s", e)

def _log_improvement(conn, element, change, delta):
    imp_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""CREATE TABLE IF NOT EXISTS skill_improvements (
        id TEXT PRIMARY KEY, skill_id TEXT, old_version TEXT, new_version TEXT,
        opportunity_type TEXT, proposer_reasoning TEXT, deployed_at TEXT
    )""")
    conn.execute(
        "INSERT INTO skill_improvements (id,skill_id,old_version,new_version,opportunity_type,proposer_reasoning,deployed_at) VALUES (?,?,?,?,?,?,?)",
        (imp_id, element, '1', '2', 'optimizer', change.get('reasoning',''), now),
    )
    conn.commit()
