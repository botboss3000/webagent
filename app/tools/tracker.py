"""
Tool execution tracker for performance monitoring.
"""
import json
import logging
from typing import Dict, Any, Optional
from app.db import get_db

logger = logging.getLogger(__name__)


class ToolExecutionTracker:
    """Track tool executions and log them to the database."""

    def __init__(self):
        self._client = get_db().get_raw_client()

    async def track_execution(
        self,
        tool_name: str,
        user_id: str,
        session_id: str,
        success: bool,
        duration_ms: int,
        interaction_id: Optional[str] = None,
        error_message: Optional[str] = None,
        input_params: Optional[Dict[str, Any]] = None,
        output_result: Optional[str] = None,
    ) -> None:
        """
        Track a tool execution and log it to the database.

        Args:
            tool_name: The name of the tool
            user_id: The user who executed the tool
            session_id: The session in which the tool was executed
            success: Whether the execution was successful
            duration_ms: Duration of execution in milliseconds
            interaction_id: FK to interactions table (links to the tool call)
            error_message: Error message if execution failed
            input_params: Input parameters (truncated if large)
            output_result: Output result (truncated if large)
        """
        try:
            if input_params:
                input_str = str(input_params)
                if len(input_str) > 1000:
                    input_params = {"truncated": True, "original_length": len(input_str)}

            if output_result and len(output_result) > 1000:
                output_result = output_result[:1000] + " [truncated]"

            data = {
                "tool_name": tool_name,
                "user_id": user_id,
                "session_id": session_id,
                "interaction_id": interaction_id,
                "success": success,
                "duration_ms": duration_ms,
                "error_message": error_message,
                "input_params": json.dumps(input_params) if input_params else "{}",
                "output_result": output_result,
            }

            response = self._client.table("tool_executions").insert(data).execute()

            if response.data:
                logger.debug(f"Tracked tool execution: {tool_name} for user {user_id}")
            else:
                logger.warning(f"Failed to track tool execution: {tool_name}")

        except Exception as e:
            logger.error(f"Error tracking tool execution for {tool_name}: {e}")


# Global instance
_tool_tracker = ToolExecutionTracker()


async def track_execution(
    tool_name: str,
    user_id: str,
    session_id: str,
    success: bool,
    duration_ms: int,
    interaction_id: Optional[str] = None,
    error_message: Optional[str] = None,
    input_params: Optional[Dict[str, Any]] = None,
    output_result: Optional[str] = None,
) -> None:
    """
    Track a tool execution and log it to the database.

    Args:
        tool_name: The name of the tool
        user_id: The user who executed the tool
        session_id: The session in which the tool was executed
        success: Whether the execution was successful
        duration_ms: Duration of execution in milliseconds
        interaction_id: FK to interactions table
        error_message: Error message if execution failed
        input_params: Input parameters (truncated if large)
        output_result: Output result (truncated if large)
    """
    await _tool_tracker.track_execution(
        tool_name=tool_name,
        user_id=user_id,
        session_id=session_id,
        success=success,
        duration_ms=duration_ms,
        interaction_id=interaction_id,
        error_message=error_message,
        input_params=input_params,
        output_result=output_result,
    )
