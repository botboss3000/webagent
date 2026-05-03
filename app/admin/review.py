"""
Admin endpoints for tool review and management.
"""

from fastapi import APIRouter, HTTPException
from typing import List
from app.models.schemas import ToolRecord, ReviewToolRequest
from app.db import get_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/tools", response_model=List[ToolRecord])
async def list_all_tools():
    """
    Get all active tools across all users (admin view).
    """
    try:
        client = get_db().get_raw_client()
        response = (
            client.table("tools")
            .select("*")
            .eq("status", "active")
            .execute()
        )

        tools = []
        for row in response.data or []:
            tools.append(ToolRecord(**row))

        return tools
    except Exception as e:
        logger.error(f"Error fetching tools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools/{tool_name}")
async def get_tool(tool_name: str):
    """
    Get a specific tool by name.
    """
    try:
        client = get_db().get_raw_client()
        response = (
            client.table("tools")
            .select("*")
            .eq("name", tool_name)
            .eq("status", "active")
            .limit(1)
            .execute()
        )

        if not response.data:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

        return response.data[0]
    except Exception as e:
        logger.error(f"Error fetching tool {tool_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/tools/{tool_id}")
async def delete_tool(tool_id: str):
    """
    Soft-delete (deprecate) a tool.
    """
    try:
        client = get_db().get_raw_client()
        response = (
            client.table("tools")
            .update({"status": "deprecated", "updated_at": "now()"})
            .eq("id", tool_id)
            .execute()
        )

        if not response.data:
            raise HTTPException(status_code=404, detail="Tool not found")

        logger.info(f"Deprecated tool {tool_id}")
        return {"message": "Tool deprecated successfully"}
    except Exception as e:
        logger.error(f"Error deprecating tool {tool_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
