"""
Optimizer tools for the Planner and Finalizer subagents.
Loaded into the webAgent's toolset when the user is chatting in an optimizer session.
"""

from __future__ import annotations

import asyncio, inspect, json, logging, os, sqlite3, sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Simulation helpers ────────────────────────────────────────────────────────

async def _execute_sim_tool(name: str, args_str: str, tools: dict, test_uid: str, test_session_id: str) -> str:
    """Execute a tool handler in the simulation context.
    Tries calling with just the provided args first, then with user/session context if needed."""
    try:
        args = json.loads(args_str)
        if not isinstance(args, dict):
            args = {}
    except Exception:
        args = {}

    if name not in tools:
        return f"[Tool '{name}' not available in simulation]"

    info = tools[name]
    handler = getattr(info, 'handler', None)
    if handler is None:
        return f"[Tool '{name}' has no handler]"

    for extra in [{}, {"user_id": test_uid}, {"user_id": test_uid, "session_id": test_session_id}]:
        try:
            call_args = {**args, **extra}
            if inspect.iscoroutinefunction(handler):
                result = await handler(**call_args)
            else:
                result = await asyncio.get_event_loop().run_in_executor(None, lambda: handler(**call_args))
            return str(result)[:1500] if result is not None else "[empty result]"
        except TypeError:
            continue
        except Exception as e:
            return f"[Tool error: {e}]"

    return f"[Could not execute tool '{name}']"


async def _run_simulated_conversation(
    worker_system_prompt: str,
    sim_user_prompt: str,
    tool_definitions: list,
    tools: dict,
    llm_client,
    llm_model: str,
    max_turns: int = 3,
    test_uid: str = "",
    test_session_id: str = "",
) -> tuple:
    """
    Run a multi-turn conversation between a sim_user agent and a worker agent.

    Returns (transcript, db_rows):
    - transcript: list of entries for planner/finalizer viewing
      (roles: sim_user | worker | tool_call | tool_result)
    - db_rows: list of dicts matching the real session interaction format,
      with full input/output/metadata fields identical to normal local.db sessions.
      Caller should INSERT these directly into the temp DB interactions table.
    """
    import time as _time
    import uuid as _uuid_sim

    def _msgs_json(msgs):
        """Serialise the message list for storage in the input column."""
        safe = []
        for m in msgs:
            entry = {"role": m.get("role"), "content": m.get("content")}
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            safe.append(entry)
        return json.dumps(safe, ensure_ascii=False)

    worker_messages = [{"role": "system", "content": worker_system_prompt}]
    sim_messages = [{"role": "system", "content": sim_user_prompt}]
    transcript = []
    db_rows = []
    parent_id = None
    turn_num = 0

    # ── Sim user opens the conversation ──────────────────────────────────────
    try:
        sim_resp = await llm_client.chat.completions.create(
            model=llm_model, messages=sim_messages, temperature=0.7, max_tokens=256,
        )
        sim_text = (sim_resp.choices[0].message.content or "").strip()
    except Exception as e:
        sim_text = "Hello, I need your help with something."
        logger.warning(f"sim_user opening failed: {e}")

    sim_messages.append({"role": "assistant", "content": sim_text})
    transcript.append({"role": "sim_user", "content": sim_text})
    worker_messages.append({"role": "user", "content": sim_text})

    _open_id = str(_uuid_sim.uuid4())
    db_rows.append({
        "id": _open_id, "role": "user", "content": sim_text,
        "tool_name": None, "tool_call_id": None, "parent_id": None,
        "input": json.dumps({"user_id": test_uid, "session_id": test_session_id, "message": sim_text}),
        "output": "",
        "metadata": json.dumps({"sim_role": "sim_user"}),
    })
    parent_id = _open_id

    for turn in range(max_turns):
        turn_num += 1
        worker_tool_calls = []

        try:
            # ── Worker responds ───────────────────────────────────────────────
            _input_snap = _msgs_json(worker_messages)
            _w_start = _time.time()
            w_resp = await llm_client.chat.completions.create(
                model=llm_model,
                messages=worker_messages,
                tools=tool_definitions if tool_definitions else None,
                tool_choice="auto" if tool_definitions else None,
                temperature=0.0,
                max_tokens=2048,
            )
            _w_dur = int((_time.time() - _w_start) * 1000)
            w_msg = w_resp.choices[0].message
            worker_text = w_msg.content or ""

            # ── Tool-call loop ────────────────────────────────────────────────
            tool_iterations = 0
            while w_msg.tool_calls and tool_iterations < 6:
                tool_iterations += 1

                _tc_list = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in w_msg.tool_calls
                ]
                # Append assistant tool-call message to history
                worker_messages.append({"role": "assistant", "content": w_msg.content, "tool_calls": _tc_list})

                # DB row: assistant with tool calls (uses [Tool calls: ...] content format)
                _tc_spec = [{"name": tc.function.name, "args": tc.function.arguments} for tc in w_msg.tool_calls]
                _asst_tc_content = ((w_msg.content or "") + "\n\n[Tool calls: " + json.dumps(_tc_spec) + "]").lstrip("\n").rstrip()
                _asst_tc_id = str(_uuid_sim.uuid4())
                db_rows.append({
                    "id": _asst_tc_id, "role": "assistant", "content": _asst_tc_content,
                    "tool_name": None, "tool_call_id": None, "parent_id": parent_id,
                    "input": _input_snap,
                    "output": json.dumps({"role": "assistant", "content": w_msg.content, "tool_calls": _tc_list}),
                    "metadata": json.dumps({"model": llm_model, "turn": turn_num,
                                            "duration_ms": _w_dur, "iteration": tool_iterations}),
                })
                parent_id = _asst_tc_id

                for tc in w_msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = tc.function.arguments

                    transcript.append({
                        "role": "tool_call", "tool": fn_name,
                        "args": fn_args[:400],
                        "content": f"[Tool call: {fn_name}({fn_args[:300]})]",
                    })

                    # Execute the tool
                    _tool_start = _time.time()
                    tc_result = await _execute_sim_tool(fn_name, fn_args, tools, test_uid, test_session_id)
                    _tool_dur = int((_time.time() - _tool_start) * 1000)

                    transcript.append({"role": "tool_result", "tool": fn_name, "content": tc_result[:600]})
                    worker_tool_calls.append({"tool": fn_name, "result": tc_result[:300]})

                    # Feed result back to worker
                    worker_messages.append({"role": "tool", "tool_call_id": tc.id, "content": tc_result})

                    # DB row: tool result (same format as real sessions)
                    _input_params = {}
                    try:
                        _input_params = json.loads(fn_args) if fn_args else {}
                    except Exception:
                        pass
                    _success = not tc_result.startswith("[Tool error") and not tc_result.startswith("[Tool '") and not tc_result.startswith("[Could not")
                    _tool_row_id = str(_uuid_sim.uuid4())
                    db_rows.append({
                        "id": _tool_row_id, "role": "tool", "content": tc_result[:4000],
                        "tool_name": fn_name, "tool_call_id": tc.id, "parent_id": _asst_tc_id,
                        "input": _msgs_json(worker_messages[:-1]),  # messages before tool result added
                        "output": json.dumps({"role": "tool", "content": tc_result,
                                              "tool_call_id": tc.id, "name": fn_name}),
                        "metadata": json.dumps({"success": _success, "duration_ms": _tool_dur,
                                                "input_params": _input_params, "error_message": None}),
                    })
                    parent_id = _tool_row_id

                # Worker continues after seeing tool results
                _input_snap = _msgs_json(worker_messages)
                _w_start = _time.time()
                w_resp2 = await llm_client.chat.completions.create(
                    model=llm_model,
                    messages=worker_messages,
                    tools=tool_definitions if tool_definitions else None,
                    tool_choice="auto" if tool_definitions else None,
                    temperature=0.0,
                    max_tokens=2048,
                )
                _w_dur = int((_time.time() - _w_start) * 1000)
                w_msg = w_resp2.choices[0].message
                worker_text = w_msg.content or ""

            # ── Final worker text response ────────────────────────────────────
            worker_messages.append({"role": "assistant", "content": worker_text})

        except Exception as e:
            worker_text = f"[Worker error: {e}]"
            _w_dur = 0
            logger.warning(f"Worker turn {turn} failed: {e}")

        transcript.append({"role": "worker", "content": worker_text, "tool_calls": worker_tool_calls})

        # DB row: final assistant text
        _final_id = str(_uuid_sim.uuid4())
        db_rows.append({
            "id": _final_id, "role": "assistant", "content": worker_text,
            "tool_name": None, "tool_call_id": None, "parent_id": parent_id,
            "input": _msgs_json(worker_messages[:-1]),
            "output": json.dumps({"role": "assistant", "content": worker_text}),
            "metadata": json.dumps({"model": llm_model, "turn": turn_num, "duration_ms": _w_dur}),
        })
        parent_id = _final_id

        # Last turn — don't collect another sim_user response
        if turn >= max_turns - 1:
            break

        # ── Sim user evaluates and responds ──────────────────────────────────
        try:
            sim_messages.append({"role": "user", "content": worker_text})
            s_resp = await llm_client.chat.completions.create(
                model=llm_model, messages=sim_messages, temperature=0.7, max_tokens=256,
            )
            sim_text = (s_resp.choices[0].message.content or "").strip()
            sim_messages.append({"role": "assistant", "content": sim_text})
        except Exception as e:
            sim_text = "[Sim user evaluation failed]"
            logger.warning(f"Sim user turn {turn} failed: {e}")

        # Detect natural conversation completion
        done_signals = [
            "that's all", "thank you", "perfect", "great, thanks", "exactly what i needed",
            "all set", "that answers", "that's correct", "test complete", "done testing",
            "satisfied", "works as expected", "problem solved",
        ]
        is_terminal = turn >= 1 and any(sig in sim_text.lower() for sig in done_signals)

        entry = {"role": "sim_user", "content": sim_text}
        if is_terminal:
            entry["terminal"] = True
        transcript.append(entry)

        if is_terminal:
            break

        worker_messages.append({"role": "user", "content": sim_text})

        # DB row: sim_user follow-up
        _follow_id = str(_uuid_sim.uuid4())
        db_rows.append({
            "id": _follow_id, "role": "user", "content": sim_text,
            "tool_name": None, "tool_call_id": None, "parent_id": parent_id,
            "input": json.dumps({"user_id": test_uid, "session_id": test_session_id, "message": sim_text}),
            "output": "",
            "metadata": json.dumps({"sim_role": "sim_user", "terminal": is_terminal}),
        })
        parent_id = _follow_id

    return transcript, db_rows


async def run_worker_trials(changes_json: str, user_id: str, session_id: str) -> str:
    """
    Test proposed changes by running a simulated multi-turn conversation between
    a sim_user agent and a worker agent (the real agent with the proposed change applied).

    For each change:
    1. Create temp DB (app/db/test_<uuid>.db) + init schema
    2. Build worker system prompt from real agent context + proposed change
    3. Read sim_user_prompt / planner_guidance / max_turns from the change dict
    4. Run _run_simulated_conversation: sim_user ↔ worker (with real tool execution)
    5. Store every message + tool call/result in temp DB interactions
    6. Return full transcript + metrics so Planner and Finalizer can evaluate

    The planner controls the test via three new change fields:
    - sim_user_prompt: the testing goal and opening strategy for the sim user
    - planner_guidance: injected into the worker's system prompt to handle multi-turn
    - max_turns: number of conversation rounds (default 3)

    Returns JSON string with results for all changes.
    Each result includes "test_db_path" and "trial_transcript" with full message history.
    """
    import json, uuid, time, os, re, sqlite3, logging
    import traceback as _tb
    from datetime import datetime, timezone

    try:
        changes = json.loads(changes_json) if isinstance(changes_json, str) else changes_json
    except Exception as e:
        logging.error(f"run_worker_trials JSON parse: {e}\n{_tb.format_exc()}")
        return json.dumps({"status": "error", "message": f"JSON parse: {e}"})
    logging.warning(f"run_worker_trials: {len(changes) if isinstance(changes, list) else 0} changes, user={user_id}, session={session_id}")
    if not isinstance(changes, list):
        changes = [changes]

    # Resolve real user_id from optimizer agent user_id (opt_role_realuser -> realuser)
    real_user_id = user_id
    if user_id.startswith('opt_'):
        parts = user_id.split('_', 2)
        if len(parts) == 3:
            real_user_id = parts[2]
    logging.warning(f"run_worker_trials: real_user_id={real_user_id}")

    # Determine paths (app/tools/optimizer_tools.py -> app/db/)
    _here = os.path.dirname(os.path.abspath(__file__))          # .../app/tools
    _project_root = os.path.normpath(os.path.join(_here, "..", ".."))  # project root
    _db_dir = os.path.normpath(os.path.join(_here, "..", "db"))       # .../app/db
    _local_path = os.path.join(_db_dir, "local.db")

    # ── Read from local.db (read-only connection) ──
    _local_conn = sqlite3.connect(_local_path)
    _local_conn.execute("PRAGMA journal_mode=WAL")
    _local_conn.execute("PRAGMA busy_timeout=10000")

    try:
        # Read original user message from optimizer:init interaction
        original_message = "what time is it in Detroit?"
        init_row = _local_conn.execute(
            "SELECT content FROM interactions WHERE session_id=? AND source='optimizer:init' ORDER BY created_at ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        if init_row:
            content = init_row[0]
            user_msgs = re.findall(r'\[user\]\s*(.+)', content)
            if user_msgs:
                original_message = user_msgs[0].strip()
        logging.warning(f"run_worker_trials: original_message={original_message}")

        # Read real agent (read-only — no write lock)
        real_agent = _local_conn.execute(
            "SELECT id, system_prompt, agent_prompt, user_prompt, skills_prompt, tasks_prompt, misc_prompt FROM agents WHERE user_id=? LIMIT 1",
            (real_user_id,)
        ).fetchone()
        if not real_agent:
            return json.dumps({"status": "error", "message": "No real agent found for user"})

        real_agent_id = real_agent[0]
        base_prompt = real_agent[1] or ""
        base_ctx = {
            "agent_prompt":  real_agent[2] or "",
            "user_prompt":   real_agent[3] or "",
            "skills_prompt": real_agent[4] or "",
            "tasks_prompt":  real_agent[5] or "",
            "misc_prompt":   real_agent[6] or "",
        }
    finally:
        _local_conn.close()

    # Build context docs list for prompt builder from context columns
    _PROMPT_COLS_LOCAL = [
        ("agent_prompt",  "agent",  "Agent Identity"),
        ("user_prompt",   "user",   "User"),
        ("skills_prompt", "skills", "Core Skills"),
        ("tasks_prompt",  "tasks",  "Common Tasks"),
        ("misc_prompt",   "misc",   "Misc"),
    ]
    base_context_docs = []
    for col, ct, title in _PROMPT_COLS_LOCAL:
        content = base_ctx.get(col, "") or ""
        if content.strip():
            base_context_docs.append({
                "context_type": ct,
                "title": title,
                "content": content,
                "tags": [],
            })

    # ── LLM client setup (same pattern as loop.py _get_client) ──
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    llm_base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    llm_api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    llm_model = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or "deepseek/deepseek-v4-flash"

    _llm_client = AsyncOpenAI(base_url=llm_base_url, api_key=llm_api_key, timeout=60.0)

    from app.agent.prompts import build_system_prompt
    from app.db.local import SCHEMA_SQL
    from app.tools.loader import load_tools

    results = []
    _now_iso = lambda: datetime.now(timezone.utc).isoformat()

    for ci, change in enumerate(changes):
        element = change.get("element", "unknown")
        new_content = change.get("new_content", "")
        element_type = change.get("element_type", "system_prompt")
        trial_id = str(uuid.uuid4())[:8]
        test_uid = f"worker-test-{trial_id}"
        test_agent_id = str(uuid.uuid4())
        temp_db_name = f"test_{uuid.uuid4().hex}.db"
        temp_db_path = os.path.join(_db_dir, temp_db_name)

        trial_entry = None
        temp_conn = None

        try:
            # 1. Create temp DB and init schema
            os.makedirs(_db_dir, exist_ok=True)
            temp_conn = sqlite3.connect(temp_db_path)
            temp_conn.execute("PRAGMA journal_mode=WAL")
            temp_conn.execute("PRAGMA busy_timeout=10000")
            temp_conn.executescript(SCHEMA_SQL)
            now = _now_iso()

            # 2. Build trial context: start from real agent's context, apply proposed change
            trial_ctx = dict(base_ctx)  # copy all 5 columns
            trial_prompt = base_prompt

            _TYPE_TO_COL_LOCAL = {
                "agent": "agent_prompt", "user": "user_prompt", "skills": "skills_prompt",
                "tasks": "tasks_prompt", "misc": "misc_prompt",
            }
            if element_type == "system_prompt" and new_content:
                trial_prompt += "\n\n" + new_content
            elif element_type in ("context_column", "context_document") and new_content:
                # element is the column name (agent_prompt) or context_type (agent)
                col = element if element in trial_ctx else _TYPE_TO_COL_LOCAL.get(element)
                if col and col in trial_ctx:
                    trial_ctx[col] = new_content

            # 3. Insert trial agent into temp db with updated context columns
            temp_conn.execute(
                """INSERT INTO agents
                   (id, user_id, system_prompt, status, metadata,
                    agent_prompt, user_prompt, skills_prompt, tasks_prompt, misc_prompt,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'active', '{}', ?, ?, ?, ?, ?, ?, ?)""",
                (test_agent_id, test_uid, trial_prompt,
                 trial_ctx["agent_prompt"], trial_ctx["user_prompt"],
                 trial_ctx["skills_prompt"], trial_ctx["tasks_prompt"], trial_ctx["misc_prompt"],
                 now, now)
            )

            # 5. Create session in temp db
            test_session_id = f"trial-{trial_id}"
            temp_conn.execute(
                "INSERT INTO sessions (id, user_id, title, metadata, created_at, updated_at) VALUES (?, ?, ?, '{}', ?, ?)",
                (test_session_id, test_uid, f"Worker trial: {element}", now, now)
            )

            temp_conn.commit()

            # 6. Build system prompt using the app's prompt builder
            system_prompt = await build_system_prompt(
                base_context_docs,
                brain_context=None,
                user_id=test_uid,
                bootstrap_tools=trial_ctx.get("bootstrap_tools", ""),
                agent_system_prompt=trial_prompt,
            )

            # 7. Parse simulation parameters from the change
            sim_user_prompt = change.get("sim_user_prompt") or (
                f"You are a simulated user testing an AI assistant. "
                f"Your goal: send a realistic message to test whether the assistant works correctly "
                f"and evaluate its response.\n\n"
                f"Start by sending this message: {original_message}\n\n"
                f"After each response, evaluate it. If satisfied, say "
                f"\"That answers my question, thank you.\" "
                f"If not, send one follow-up to push for a better answer."
            )
            planner_guidance = change.get("planner_guidance", "")
            max_turns = max(1, min(int(change.get("max_turns", 3)), 8))

            # Append planner guidance to worker system prompt so it handles multi-turn correctly
            worker_system = system_prompt
            if planner_guidance:
                worker_system += (
                    f"\n\n## [OPTIMIZER TEST GUIDANCE]\n"
                    f"You are being tested. Follow this guidance during the conversation:\n"
                    f"{planner_guidance}"
                )

            # 8. Load tools for the worker agent
            tools_for_worker = await load_tools(real_user_id)
            tool_definitions = []
            for tname, tinfo in tools_for_worker.items():
                desc = (
                    tinfo.handler.__doc__ if hasattr(tinfo, 'handler') and tinfo.handler.__doc__
                    else f"Execute {tname}"
                )
                desc = desc.split("\n")[0]
                tool_definitions.append({
                    "type": "function",
                    "function": {
                        "name": tname,
                        "description": desc,
                        "parameters": (
                            tinfo.parameters if hasattr(tinfo, 'parameters')
                            else {"type": "object", "properties": {}, "required": []}
                        ),
                    },
                })

            # 9. Run the simulated conversation: sim_user ↔ worker (tools are actually executed)
            start_time = time.time()
            sim_transcript, sim_db_rows = await _run_simulated_conversation(
                worker_system_prompt=worker_system,
                sim_user_prompt=sim_user_prompt,
                tool_definitions=tool_definitions,
                tools=tools_for_worker,
                llm_client=_llm_client,
                llm_model=llm_model,
                max_turns=max_turns,
                test_uid=test_uid,
                test_session_id=test_session_id,
            )
            elapsed = time.time() - start_time

            # 10. Store interactions in the temp DB using the full real-session format:
            #     - user rows: input = request context JSON
            #     - assistant rows: input = messages array, output = raw API response JSON
            #     - tool rows: input = messages at call time, output = tool result JSON,
            #                  metadata = {success, duration_ms, input_params}
            _ts_base = now  # reuse the existing ISO timestamp; rows share session_id so order is fine
            for row in sim_db_rows:
                temp_conn.execute(
                    "INSERT INTO interactions "
                    "(id, session_id, parent_id, role, content, tool_name, tool_call_id, "
                    "input, output, source, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sim_conversation', ?, ?)",
                    (
                        row["id"], test_session_id, row.get("parent_id"),
                        row["role"], row["content"][:4000],
                        row.get("tool_name"), row.get("tool_call_id"),
                        row.get("input", ""), row.get("output", ""),
                        row.get("metadata", "{}"),
                        _ts_base,
                    )
                )
            temp_conn.commit()

            # 11. Build result
            worker_replies = [e for e in sim_transcript if e["role"] == "worker"]
            tool_calls_made = [e for e in sim_transcript if e["role"] == "tool_call"]
            last_reply = worker_replies[-1]["content"] if worker_replies else ""
            sim_user_satisfied = any(e.get("terminal") for e in sim_transcript)
            turn_count = len(worker_replies)

            success = bool(last_reply and not last_reply.startswith("[Worker error"))
            confidence = 0.85 if (success and sim_user_satisfied) else (0.6 if success else 0.2)
            rel_db_path = os.path.relpath(temp_db_path, _project_root).replace("\\", "/")

            trial_entry = {
                "element": element,
                "element_type": element_type,
                "new_content": new_content,
                "success": success,
                "sim_user_satisfied": sim_user_satisfied,
                "reply": last_reply[:400],
                "turn_count": turn_count,
                "tool_calls_made": len(tool_calls_made),
                "token_estimate": max(len(json.dumps(sim_transcript)) // 4, 10),
                "duration_ms": int(elapsed * 1000),
                "confidence": confidence,
                "test_db_path": rel_db_path,
                "trial_transcript": sim_transcript,
            }

        except Exception as e:
            tb = _tb.format_exc()
            logging.error(f"Worker trial {ci} for {element} crashed: {e}\n{tb}")
            trial_entry = {
                "element": element,
                "error": f"{e}",
                "traceback": tb,
                "confidence": 0.0,
                "success": False,
                "reply": f"Error: {e}",
            }
        finally:
            if temp_conn:
                try:
                    temp_conn.close()
                except Exception:
                    pass

        if trial_entry:
            results.append(trial_entry)

    return json.dumps(results, indent=2)


async def handoff_to_finalizer(summary: str = "", user_id: str = "", session_id: str = "",
                                judging_criteria: str = "", baseline_transcript: str = "",
                                worker_results: str = "") -> str:
    """Hand off the optimization to the Finalizer agent for review.
    Creates a new finalizer-<uuid8> session with all relevant data pre-injected
    as interaction rows in the session's temp DB. The Finalizer sees the full
    context (criteria, baseline, trials) via its conversation history — not metadata.
    """
    import uuid as _uuid_mod
    from datetime import datetime as _dt2, timezone as _tz2

    # 1. Resolve real user_id
    real_user_id = user_id
    if user_id.startswith('opt_'):
        parts = user_id.split('_', 2)
        if len(parts) == 3:
            real_user_id = parts[2]
    logger.warning(f"handoff_to_finalizer: real_user_id={real_user_id}")

    # 2. Compute paths
    _here = os.path.dirname(os.path.abspath(__file__))
    _db_dir = os.path.normpath(os.path.join(_here, "..", "db"))
    _local_path = os.path.join(_db_dir, "local.db")
    os.makedirs(_db_dir, exist_ok=True)
    temp_db_name = f"finalizer_{_uuid_mod.uuid4().hex[:16]}.db"
    temp_db_path = os.path.join(_db_dir, temp_db_name)

    # 3. Init temp DB schema directly — no LocalBackend constructor (avoids seeding side effects)
    from app.db.local import SCHEMA_SQL as _FSQL
    _tc = sqlite3.connect(temp_db_path)
    try:
        _tc.executescript(_FSQL)
        _tc.commit()
    finally:
        _tc.close()

    # 4. Read opt_finalizer template from agent_templates in local.db
    template_data = {}
    try:
        _lc = sqlite3.connect(_local_path)
        _lc.row_factory = sqlite3.Row
        try:
            trow = _lc.execute("SELECT * FROM agent_templates WHERE id='opt_finalizer'").fetchone()
            if trow:
                template_data = dict(trow)
        finally:
            _lc.close()
    except Exception as e:
        logger.warning(f"handoff_to_finalizer: failed to read agent_templates: {e}")

    if not template_data:
        return json.dumps({"status": "error", "message": "opt_finalizer template not found in agent_templates"})

    # 5. Find target_session_id from planner session metadata in local.db
    target_session_id = ""
    try:
        _lc2 = sqlite3.connect(_local_path)
        _lc2.row_factory = sqlite3.Row
        try:
            srow = _lc2.execute("SELECT metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
            if srow and srow["metadata"]:
                _smeta = json.loads(srow["metadata"])
                target_session_id = _smeta.get("target_session", "")
        finally:
            _lc2.close()
    except Exception as e:
        logger.warning(f"handoff_to_finalizer: failed to find target_session: {e}")

    # 6. Generate IDs and timestamps
    finalizer_sid = f"finalizer-{_uuid_mod.uuid4().hex[:8]}"
    agent_user_id = f"opt_finalizer_{real_user_id}"
    agent_id = str(_uuid_mod.uuid4())
    now_sql = _dt2.now(_tz2.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 7. Create agent and session in temp DB
    _tc2 = sqlite3.connect(temp_db_path)
    try:
        _tc2.execute(
            """INSERT INTO agents
               (id, user_id, template_id, system_prompt, max_turn_count, model, provider,
                temperature, max_tokens, status, metadata, agent_prompt, user_prompt,
                skills_prompt, tasks_prompt, misc_prompt, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, agent_user_id, "opt_finalizer",
             template_data.get("system_prompt", ""),
             template_data.get("max_turn_count", 10),
             template_data.get("model"),
             template_data.get("provider"),
             template_data.get("temperature", 0.2),
             template_data.get("max_tokens", 4096),
             template_data.get("metadata", "{}"),
             template_data.get("agent_prompt", ""),
             template_data.get("user_prompt", ""),
             template_data.get("skills_prompt", ""),
             template_data.get("tasks_prompt", ""),
             template_data.get("misc_prompt", ""),
             now_sql, now_sql),
        )
        participants_json = json.dumps([
            {"id": real_user_id, "role": "user"},
            {"id": agent_id, "role": "agent"},
        ])
        _tc2.execute(
            "INSERT INTO sessions (id, user_id, title, agent_id, participants, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (finalizer_sid, real_user_id,
             f"Finalizer - {session_id[:12]}",
             agent_id, participants_json,
             json.dumps({"opt_role": "finalizer", "source_optimizer_session": session_id}),
             now_sql, now_sql),
        )
        _tc2.commit()

        # 8. Pre-inject all context as interaction rows so Finalizer sees them in history
        _iid_counter = [0]
        def _inject(role, content):
            _iid_counter[0] += 1
            # offset created_at by counter so ORDER BY created_at gives deterministic order
            from datetime import datetime as _dti, timezone as _tzi, timedelta as _tdd
            ts = (_dti.now(_tzi.utc) + _tdd(seconds=_iid_counter[0])).strftime("%Y-%m-%d %H:%M:%S")
            _tc2.execute(
                "INSERT INTO interactions (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(_uuid_mod.uuid4()), finalizer_sid, role, content, ts),
            )

        # 8a. Judging criteria + summary as the opening context
        _inject("user", f"## Judging Criteria\n{judging_criteria}\n\n## Optimization Summary\n{summary}")

        # 8b. Baseline interactions from target session in local.db
        baseline_injected = False
        if target_session_id:
            try:
                _lc3 = sqlite3.connect(_local_path)
                _lc3.row_factory = sqlite3.Row
                try:
                    b_rows = _lc3.execute(
                        "SELECT role, content FROM interactions WHERE session_id=? ORDER BY created_at",
                        (target_session_id,)
                    ).fetchall()
                    if b_rows:
                        _inject("user", f"## Baseline Conversation (original session `{target_session_id[:8]}`)")
                        for br in b_rows:
                            role = br["role"] if br["role"] in ("user", "assistant") else "user"
                            _inject(role, (br["content"] or "")[:2000])
                        baseline_injected = True
                finally:
                    _lc3.close()
            except Exception as e:
                logger.warning(f"handoff_to_finalizer: baseline inject failed: {e}")

        if not baseline_injected and baseline_transcript:
            _inject("user", f"## Baseline Transcript\n{baseline_transcript[:3000]}")

        # 8c. Trial transcripts from worker_results JSON
        try:
            wr_data = json.loads(worker_results) if isinstance(worker_results, str) else worker_results
            if isinstance(wr_data, list):
                for i, trial in enumerate(wr_data):
                    t_transcript = trial.get("trial_transcript", [])
                    sim_satisfied = trial.get("sim_user_satisfied", False)
                    element = trial.get("element", "?")
                    tool_count = trial.get("tool_calls_made", 0)
                    lines = [
                        f"## Trial {i+1}: `{element}` | Sim User Satisfied: {'YES' if sim_satisfied else 'NO'} | Tools Called: {tool_count}"
                    ]
                    for entry in t_transcript:
                        role_label = {
                            "sim_user": "Sim User",
                            "worker": "Worker",
                            "tool_call": "Tool Call",
                            "tool_result": "Tool Result",
                        }.get(entry.get("role", ""), entry.get("role", ""))
                        terminal = " [SATISFIED]" if entry.get("terminal") else ""
                        lines.append(f"[{role_label}]{terminal}: {entry.get('content', '')[:500]}")
                    _inject("user", "\n".join(lines))
        except Exception as e:
            logger.warning(f"handoff_to_finalizer: trial transcript inject failed: {e}")
            if worker_results:
                _inject("user", f"## Worker Results (raw)\n{worker_results[:3000]}")

        _tc2.commit()
    finally:
        _tc2.close()

    # 9. Create session in local.db with temp_db_path in metadata so chat.py switches to temp DB
    local_meta = json.dumps({
        "opt_role": "finalizer",
        "source_optimizer_session": session_id,
        "temp_db_path": temp_db_path,
    })
    _lc4 = sqlite3.connect(_local_path)
    try:
        _lc4.execute(
            "INSERT OR IGNORE INTO sessions (id, user_id, title, metadata, agent_id, participants, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (finalizer_sid, real_user_id,
             f"Finalizer - {session_id[:12]}",
             local_meta, agent_id,
             json.dumps([{"id": real_user_id, "role": "user"}, {"id": agent_id, "role": "agent"}]),
             now_sql, now_sql),
        )
        _lc4.commit()
    finally:
        _lc4.close()

    logger.warning(f"Finalizer session created: {finalizer_sid} (user={real_user_id}, agent={agent_id[:8]})")

    # 10. Kickstart the Finalizer
    try:
        asyncio.create_task(_kickstart_finalizer(real_user_id, finalizer_sid, summary))
    except Exception:
        pass

    return json.dumps({
        "status": "ok",
        "finalizer_session_id": finalizer_sid,
        "summary": summary,
    })


async def _kickstart_finalizer(user_id: str, finalizer_sid: str, summary: str) -> None:
    """Kickstart the Finalizer by posting a trigger message to the chat API.
    The Finalizer's system prompt has Auto-Start Rule: it evaluates immediately on first message.
    All context (criteria, baseline, trials) is pre-injected as session history.
    """
    import httpx, os, logging as _log
    try:
        port = os.environ.get('PORT', '8080')
        _log.warning(f"Kickstarting Finalizer {finalizer_sid}")
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/api/v1/chat",
                json={
                    "message": "Start your evaluation.",
                    "user_id": user_id,
                    "session_id": finalizer_sid,
                },
            )
            _log.warning(f"Kickstart Finalizer responded: {resp.status_code}")
    except Exception as e:
        _log.warning(f"Kickstart Finalizer failed (non-fatal): {e}")


async def deploy_optimization(changes_json: str, user_id: str, session_id: str) -> str:
    """Deploy approved optimization changes to the target user's agent."""
    import uuid

    # Resolve real user_id from optimizer agent user_id (opt_role_realuser -> realuser)
    real_user_id = user_id
    if user_id.startswith('opt_'):
        parts = user_id.split('_', 2)
        if len(parts) == 3:
            real_user_id = parts[2]

    changes = json.loads(changes_json)
    deployed = []

    from app.db import get_db
    backend = get_db()
    async with backend._write_lock:
        raw = backend._get_conn()
        c = raw.cursor()

        _TYPE_TO_COL_DEPLOY = {
            "agent": "agent_prompt", "user": "user_prompt", "skills": "skills_prompt",
            "tasks": "tasks_prompt", "misc": "misc_prompt",
            "agent_prompt": "agent_prompt", "user_prompt": "user_prompt", "skills_prompt": "skills_prompt",
            "tasks_prompt": "tasks_prompt", "misc_prompt": "misc_prompt",
        }

        for ch in changes:
            element = ch.get("element", "")
            new_content = ch.get("new_content", "")
            element_type = ch.get("element_type", "system_prompt")

            if element_type == "system_prompt":
                cur = c.execute("SELECT system_prompt FROM agents WHERE user_id=? LIMIT 1", (real_user_id,))
                row = cur.fetchone()
                old = row[0] if row else ""
                new_full = old + "\n\n" + new_content if old else new_content
                c.execute("UPDATE agents SET system_prompt=?, updated_at=datetime('now') WHERE user_id=?", (new_full, real_user_id))
                deployed.append(f"Updated system_prompt: {element}")
            elif element_type in ("context_column", "context_document"):
                col = _TYPE_TO_COL_DEPLOY.get(element)
                if col:
                    c.execute(f"UPDATE agents SET {col}=?, updated_at=datetime('now') WHERE user_id=?",
                              (new_content, real_user_id))
                    deployed.append(f"Updated {col}: {element}")
                else:
                    logging.warning(f"deploy_optimization: unknown element '{element}' -- skipping")

        try:
            c.execute(
                "INSERT INTO skill_improvements (id,skill_id,old_version,new_version,opportunity_type,proposer_reasoning,deployed_at) "
                "VALUES (?,?,?,?,?,?,datetime('now'))",
                (str(uuid.uuid4()), 'optimizer', '1', '2', 'optimizer', 'Deployed by optimizer Finalizer')
            )
        except Exception:
            pass  # skill_improvements table may not exist in all setups
        raw.commit()

    return json.dumps({"status": "deployed", "deployed": deployed})
