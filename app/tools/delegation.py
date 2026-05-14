"""
Delegation tools — allow an agent to hand off to another agent mid-session.

delegate_to_agent(agent_template_id, context)
    Signals the loop to switch to the named agent for the remainder of the
    session.  Returns a sentinel JSON the loop recognises and acts on.

list_delegatable_agents()
    Lists every non-pipeline agent available to the current user, with its
    trigger_description so the calling agent can decide who to delegate to.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Sentinel key the loop watches for in tool results
DELEGATE_SENTINEL = "__delegate__"


def _delegation_signal(
    target_template_id: str,
    target_agent_id: str,
    target_name: str,
    context: str,
) -> str:
    """Return the sentinel JSON string the loop detects."""
    return json.dumps({
        DELEGATE_SENTINEL: True,
        "target_template_id": target_template_id,
        "target_agent_id":    target_agent_id,
        "target_name":        target_name,
        "context":            context,
    })


def build_delegation_tools(user_id: str):
    """
    Return a dict of delegation tool handlers scoped to user_id.
    Called by the tool loader for every non-pipeline agent.
    """
    from app.db import get_db

    async def delegate_to_agent(
        agent_template_id: str,
        context: str = "",
    ) -> str:
        """
        Hand off this session to a different agent.

        Args:
            agent_template_id: The template id of the target agent
                                (e.g. 'admin-agent', 'purchase-agent').
            context: Optional context message to pass to the new agent
                     so it understands why it was called.

        Returns:
            A delegation signal the loop acts on to switch agents.
        """
        db = get_db()
        try:
            # Verify template exists and is not a pipeline agent
            templates = await db.list_agent_templates(include_admin=True)
            target_tpl = next(
                (t for t in templates if t["id"] == agent_template_id), None
            )
            if not target_tpl:
                return json.dumps({
                    "error": f"Agent template '{agent_template_id}' not found."
                })
            if target_tpl.get("is_pipeline"):
                return json.dumps({
                    "error": f"Cannot delegate to pipeline agent '{agent_template_id}'."
                })

            # Resolve or create the agent instance for this user
            agents = await db.list_agents_for_user(user_id, include_admin=True)
            target_agent = next(
                (a for a in agents if a.get("template_id") == agent_template_id), None
            )
            if not target_agent:
                # Create the agent instance on the fly
                target_agent = await db.create_custom_agent(
                    user_id=user_id,
                    name=target_tpl.get("name", agent_template_id),
                    description=target_tpl.get("description", ""),
                )

            target_name = target_tpl.get("name") or agent_template_id
            logger.info(
                "Delegation requested: user=%s template=%s agent=%s",
                user_id, agent_template_id, target_agent["id"]
            )
            return _delegation_signal(
                target_template_id=agent_template_id,
                target_agent_id=target_agent["id"],
                target_name=target_name,
                context=context,
            )
        except Exception as e:
            logger.warning("delegate_to_agent error: %s", e)
            return json.dumps({"error": str(e)})

    async def list_delegatable_agents() -> str:
        """
        List agents you can delegate to, with each agent's trigger description.
        Use this to decide which agent to hand off to.
        """
        db = get_db()
        try:
            templates = await db.list_agent_templates(include_admin=True)
            available = []
            for t in templates:
                if t.get("is_pipeline"):
                    continue
                available.append({
                    "agent_template_id":  t["id"],
                    "name":               t.get("name") or t["id"],
                    "description":        t.get("description", ""),
                    "trigger_description": t.get("trigger_description", ""),
                    "icon":               t.get("icon", ""),
                })
            return json.dumps({"agents": available})
        except Exception as e:
            logger.warning("list_delegatable_agents error: %s", e)
            return json.dumps({"error": str(e)})

    return {
        "delegate_to_agent":      delegate_to_agent,
        "list_delegatable_agents": list_delegatable_agents,
    }
