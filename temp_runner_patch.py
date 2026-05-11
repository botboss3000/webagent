import re

with open("app/optimizer/runner.py", "r") as f:
    content = f.read()

# Make it a loop based on max_iterations
new_code = """
        iteration = 0
        while iteration < max_iter:
            iteration += 1
            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:system", f"Iteration {iteration}/{max_iter} starting.")

            # Planner
            proposal = await propose_improvements(user_id, session_id, pf, mode="analyze", optimizer_history=None)
            if not proposal or not proposal.get("changes"):
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:planner",
                                f"Planner: {proposal.get('analysis','No issues found.')}")
                if iteration == 1:
                    _log_complete(run_id, "success", cfg, opt_sid, summary=proposal.get("analysis","No changes needed."))
                break

            changes = proposal["changes"]
            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:planner",
                            f"Planner: {proposal.get('analysis','')[:200]}. Proposed {len(changes)} changes.")

            # Workers
            trials = await run_trials(user_id, changes, pf.get("transcript", []), trials_per_change)
            confident = [t for t in trials if t.get("averaged", {}).get("confidence", 0) >= 0.5]
            if not confident:
                _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:worker",
                                "Worker: All trials low confidence. Trying different approach next iteration.")
                if iteration == max_iter:
                    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:system",
                                    "No noticeable improvements after max iterations. Stopping to await user feedback.")
                continue

            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:worker",
                            f"Worker: {len(confident)}/{len(trials)} trials passed confidence threshold.")

            # Finalizer
            review = await review_trials(user_id, confident, baseline, transcript=pf.get("transcript", []),
                                         criteria=criteria, target=target, skill_state=skill_state)
            winners, losers = review.get("winners", []), review.get("losers", [])
            summary = review.get("summary", "")

            deployed = 0
            for w in winners:
                if isinstance(w, dict) and w.get("element"):
                    for ch in changes:
                        if ch.get("element") == w.get("element"):
                            _deploy_change(user_id, opt_sid, ch, w)
                            deployed += 1

            _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:finalizer",
                            f"Finalizer: {len(winners)} winners, {len(losers)} rejected. Deployed {deployed}. {summary}")

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
"""

# Replace the block from # Planner to the end of the try block
target_block = re.search(r"        # Planner.*?(?=    except Exception as e)", content, re.DOTALL)
if target_block:
    new_content = content.replace(target_block.group(0), new_code + "\n")
    with open("app/optimizer/runner_new.py", "w") as f:
        f.write(new_content)
    print("Success")
else:
    print("Failed to find target block")

