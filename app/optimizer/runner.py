"""
Optimizer Runner — Standard and Iterative modes.
"""

from __future__ import annotations

import asyncio, json, logging, uuid
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


async def run_optimizer_async(user_id, session_id, channel="ui", criteria="", feedback="", skill_name=""):
    now = datetime.now(timezone.utc).isoformat()
    from app.optimizer.templates import seed_optimizer_templates
    seed_optimizer_templates()

    # Skip Worker test sessions to prevent recursive optimizer cascades
    if session_id.startswith('worker-'):
        return None

    if session_id in _recently_seen:
        if asyncio.get_event_loop().time() - _recently_seen[session_id] < _RECENT_WINDOW_SEC and not criteria:
            return None
    _recently_seen[session_id] = asyncio.get_event_loop().time()

    cfg = load_config()
    if cfg.get("mode") != "live" and "manual" not in session_id:
        return None

    from app.db import get_db
    ag = await get_db().get_agent_for_user(user_id)
    max_iter = 3
    if ag and ag.get("metadata"):
        try: max_iter = int(json.loads(ag["metadata"]).get("optimizer_max_iterations", 3))
        except: pass
    if max_iter <= 0: max_iter = cfg.get("max_iterations", 3)
    intensity = cfg.get("intensity", 3)
    thresholds = get_intensity_thresholds(intensity)
    intensity_targets = thresholds.get("targets", {})
    manual_targets = cfg.get("criteria_targets", {})
    target = manual_targets.get(criteria, intensity_targets.get(criteria, 0)) if criteria else 0
    trials_per_change = cfg.get("trials", {}).get("per_change", 2)

    opt_sid = f"optimizer-{session_id}"
    _ensure_session(user_id, opt_sid, session_id)
    run_id = str(uuid.uuid4())
    _log_start(run_id, cfg, opt_sid)

    try:
        pf = await prefilter(user_id, session_id)
        turns = pf.get("turns", 1)
        tokens_est = pf.get("tokens", 100)
        skill_state = []
        baseline = {"turns": turns, "tokens": tokens_est, "time_ms": pf.get("time_ms", 5000)}

        _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:prefilter",
                        f"Stats: {turns} turns, ~{tokens_est} tokens, {len(skill_state)} skills tracked.")


        iteration = 0
        optimizer_history = None
        while iteration < max_iter:
            iteration += 1
            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:system", f"Iteration {iteration}/{max_iter} starting.")

            # Planner — pass previous rejection feedback so it can adjust
            proposal = await propose_improvements(user_id, session_id, pf, mode="analyze", optimizer_history=optimizer_history)
            planner_msg = proposal.get('message') or f"Planner: {proposal.get('analysis','')[:200]}. Proposed {len(proposal.get('changes',[]))} changes."
            if not proposal or not proposal.get("changes"):
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:planner", planner_msg)
                if iteration == 1:
                    _log_complete(run_id, "success", cfg, opt_sid, summary=proposal.get("analysis","No changes needed."))
                break

            changes = proposal["changes"]
            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:planner", planner_msg)

            # Workers
            trials = await run_trials(user_id, changes, pf.get("transcript", []), trials_per_change)
            confident = [t for t in trials if t.get("averaged", {}).get("confidence", 0) >= 0.5]
            worker_msgs = []
            for t in trials:
                w_msg = t.get('message') or ''
                if w_msg:
                    worker_msgs.append(w_msg)
            # Build detailed rejection reasons regardless of confidence
            rejection_lines = []
            for t in trials:
                cr = t.get('confidence_reasoning') or t.get('message') or ''
                if cr:
                    rejection_lines.append(cr)
            rejection_text = '; '.join(rejection_lines[:5]) if rejection_lines else 'No specific reasoning given.'

            if not confident and trials:
                # Even low-confidence: pass to Finalizer with partial_results flag
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:worker",
                               f"Worker: {len(trials)} trials completed, all below 0.5 confidence. Passing to Finalizer for review.")
                review = await review_trials(user_id, trials, baseline, transcript=pf.get("transcript", []),
                                             criteria=criteria, target=target, skill_state=skill_state, partial_results=True)
                # If Finalizer says insufficient, build optimizer_history for Planner
                if review.get('insufficient_data'):
                    prev_element = changes[0].get('element', 'unknown') if changes else 'unknown'
                    prev_type = changes[0].get('change_type', 'unknown') if changes else 'unknown'
                    optimizer_history = (
                        f"Your previous proposal (element={prev_element}, type={prev_type}) was rejected. "
                        f"Workers: {rejection_text[:600]}. "
                        f"Adjust your approach — try a fundamentally different change."
                    )
                    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer",
                                    review.get('message', 'Finalizer: Insufficient data. Try a different approach.'))
                    if iteration == max_iter:
                        _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:system",
                                        "No noticeable improvements after max iterations. Stopping to await user feedback.")
                    continue
                # Finalizer found enough signal despite low confidence
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:worker", worker_text)
            else:
                worker_text = '\n\n---\n\n'.join(worker_msgs) if worker_msgs else f"Worker: {len(confident)}/{len(trials)} trials passed confidence threshold."
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:worker", worker_text)
                # Normal flow: pass confident trials to finalizer
                review = await review_trials(user_id, confident, baseline, transcript=pf.get("transcript", []),
                                         criteria=criteria, target=target, skill_state=skill_state)
            finalizer_msg = review.get('message') or f"Finalizer: {len(review.get('winners',[]))} winners, {len(review.get('losers',[]))} rejected. {review.get('summary','')}"
            user_feedback_setting = cfg.get("user_feedback", "always")
            if user_feedback_setting == "always":
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer", finalizer_msg)
                _log_complete(run_id, "pending_approval", cfg, opt_sid, proposals_generated=len(changes), summary=f"Pending approval. {review.get('summary', '')}")
                return opt_sid
            winners, losers = review.get("winners", []), review.get("losers", [])
            summary = review.get("summary", "")

            deployed = 0
            for w in winners:
                if isinstance(w, dict) and w.get("element"):
                    for ch in changes:
                        if ch.get("element") == w.get("element"):
                            _deploy_change(user_id, opt_sid, ch, w)
                            deployed += 1

            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer", finalizer_msg)

            if deployed > 0:
                old_total = cfg.get("state", {}).get("improvements_deployed", 0)
                _log_complete(run_id, "success", cfg, opt_sid,
                              skills_analyzed=len(skill_state), proposals_generated=len(changes),
                              proposals_deployed=deployed, summary=f"Deployed {deployed}. {summary}")
                update_state(last_run_at=now, last_run_status="success", improvements_deployed=old_total + deployed)
                return opt_sid
            else:
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:system",
                                "No deployments. Searching for new approaches next iteration.")
                if iteration == max_iter:
                    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:system",
                                    "No noticeable improvements after max iterations. Stopping to await user feedback.")

        if iteration <= max_iter and deployed == 0:
             _log_complete(run_id, "success", cfg, opt_sid, proposals_generated=len(changes) if 'changes' in locals() else 0, proposals_deployed=0)
        return opt_sid

    except Exception as e:
        logger.error("Optimizer crashed: %s", e, exc_info=True)
        _log_complete(run_id, "failed", cfg, opt_sid, errors=[str(e)])
        return opt_sid


# ── Helpers ──

def _retry_db_write(fn, max_attempts=5, delay=0.3):
    import time
    for a in range(max_attempts):
        try: return fn()
        except Exception as e:
            if "locked" in str(e).lower() and a < max_attempts-1: time.sleep(delay*(a+1))
            else: raise

def _ensure_session(uid, sid, orig):
    from app.db import get_db
    db = get_db(); raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            c = raw()
            c.execute("INSERT OR IGNORE INTO sessions (id,user_id,title,created_at,updated_at) VALUES (?,?,?,datetime('now'),datetime('now'))", (sid, uid, f"Optimizer — {orig[:12]}"))
            c.commit(); c.close()
        _retry_db_write(_do)

def _insert_opt_msg(uid, sid, role, source, content):
    from app.db import get_db
    db = get_db(); raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            c = raw()
            c.execute("INSERT INTO interactions (id,session_id,role,content,source,channel,created_at) VALUES (?,?,?,?,?,'optimizer',datetime('now'))", (str(uuid.uuid4()), sid, role, content, source))
            c.commit(); c.close()
        _retry_db_write(_do)

def _log_start(rid, cfg, sid): _log_run(rid, "running", cfg, sid)
def _log_complete(rid, st, cfg, sid, **kw): _log_run(rid, st, cfg, sid, **kw)

def _log_run(rid, status, config, sid, skills_analyzed=0, proposals_generated=0, proposals_deployed=0, summary="", errors=None):
    try:
        from app.db import get_db
        db = get_db(); raw = getattr(db, '_get_conn', None)
        if not raw: return
        now = datetime.now(timezone.utc).isoformat()
        completed = now if status in ("success","failed") else None
        errs = json.dumps(errors) if errors else None
        cfg_json = json.dumps(config)
        def _do():
            c = raw()
            c.execute("CREATE TABLE IF NOT EXISTS optimizer_runs (id TEXT PRIMARY KEY, status TEXT DEFAULT 'running', started_at TEXT, completed_at TEXT, skills_analyzed INTEGER DEFAULT 0, proposals_generated INTEGER DEFAULT 0, proposals_deployed INTEGER DEFAULT 0, errors TEXT, summary TEXT, config_snapshot TEXT, session_id TEXT)")
            ex = c.execute("SELECT id FROM optimizer_runs WHERE id=?",(rid,)).fetchone()
            if ex: c.execute("UPDATE optimizer_runs SET status=?,completed_at=?,skills_analyzed=?,proposals_generated=?,proposals_deployed=?,errors=?,summary=? WHERE id=?",(status,completed,skills_analyzed,proposals_generated,proposals_deployed,errs,summary,rid))
            else: c.execute("INSERT INTO optimizer_runs (id,status,started_at,completed_at,skills_analyzed,proposals_generated,proposals_deployed,errors,summary,config_snapshot,session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",(rid,status,now,completed,skills_analyzed,proposals_generated,proposals_deployed,errs,summary,cfg_json,sid))
            c.commit(); c.close()
        _retry_db_write(_do)
    except Exception as e: logger.warning("_log_run: %s", e)

def _deploy_change(uid, sid, change, winner):
    from app.db import get_db
    db = get_db(); raw = getattr(db, '_get_conn', None)
    if not raw: return
    el = change.get("element","")
    nc = change.get("new_content","")
    et = change.get("element_type","context_document")
    def _do():
        c = raw()
        if et == "context_document":
            ar = c.execute("SELECT id FROM agents WHERE user_id=? LIMIT 1",(uid,)).fetchone()
            if ar:
                ex = c.execute("SELECT id FROM context_documents WHERE agent_id=? AND title=? LIMIT 1",(ar[0],el)).fetchone()
                if ex: c.execute("UPDATE context_documents SET content=?,updated_at=datetime('now') WHERE id=?",(nc,ex[0]))
                else: c.execute("INSERT INTO context_documents (id,agent_id,context_type,title,content,tags,created_at,updated_at) VALUES (?,(SELECT id FROM agents WHERE user_id=? LIMIT 1),'skills',?,?,'[]',datetime('now'),datetime('now'))",(str(uuid.uuid4()),uid,el,nc))
        elif et == "system_prompt": c.execute("UPDATE agents SET system_prompt=?,updated_at=datetime('now') WHERE user_id=?",(nc,uid))
        c.execute("CREATE TABLE IF NOT EXISTS skill_improvements (id TEXT PRIMARY KEY, skill_id TEXT, old_version TEXT, new_version TEXT, opportunity_type TEXT, proposer_reasoning TEXT, deployed_at TEXT)")
        c.execute("INSERT INTO skill_improvements (id,skill_id,old_version,new_version,opportunity_type,proposer_reasoning,deployed_at) VALUES (?,?,?,?,?,?,datetime('now'))",(str(uuid.uuid4()),el,'1','2','optimizer',change.get('reasoning','')))
        c.commit(); c.close()
    try: _retry_db_write(_do)
    except Exception as e: logger.warning("_deploy_change: %s", e)
