"""File-storage integration tools — Google Drive + OneDrive + Dropbox."""

import base64 as _b64
import json

import httpx

from app.integrations.oauth_helper import (
    oauth_api_call,
    get_oauth_token,
    not_connected_payload,
)


# ── Google Drive ──────────────────────────────────────────────────────────

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"


async def drive_list_files(
    user_id: str,
    agent_id: str,
    query: str = "",
    page_size: int = 20,
    order_by: str = "modifiedTime desc",
) -> str:
    params: dict = {
        "pageSize": max(1, min(int(page_size or 20), 100)),
        "fields":   "files(id,name,mimeType,modifiedTime,owners,webViewLink,size),nextPageToken",
        "orderBy":  order_by or "modifiedTime desc",
    }
    if query:
        params["q"] = query
    result = await oauth_api_call(user_id, agent_id, "google", "GET", f"{_DRIVE_BASE}/files", params=params)
    if result.get("status") == "not_connected":
        return not_connected_payload("google")
    return json.dumps(result)


async def drive_get_file(
    user_id: str,
    agent_id: str,
    file_id: str,
    download: bool = False,
    export_mime_type: str = "",
) -> str:
    if not file_id:
        return json.dumps({"status": "error", "message": "file_id required"})

    if not download:
        result = await oauth_api_call(
            user_id, agent_id, "google", "GET",
            f"{_DRIVE_BASE}/files/{file_id}",
            params={"fields": "id,name,mimeType,modifiedTime,owners,webViewLink,size,parents"},
        )
        if result.get("status") == "not_connected":
            return not_connected_payload("google")
        return json.dumps(result)

    tok = await get_oauth_token(user_id, agent_id, "google")
    if not tok:
        return not_connected_payload("google")

    if export_mime_type:
        url = f"{_DRIVE_BASE}/files/{file_id}/export"
        params = {"mimeType": export_mime_type}
    else:
        url = f"{_DRIVE_BASE}/files/{file_id}"
        params = {"alt": "media"}

    return await _binary_download(url, params, tok["access_token"])


# ── OneDrive (Microsoft Graph) ────────────────────────────────────────────

_GRAPH = "https://graph.microsoft.com/v1.0"


async def onedrive_list_files(
    user_id: str,
    agent_id: str,
    folder_path: str = "",
    max_results: int = 25,
) -> str:
    """List children of a OneDrive folder. folder_path empty = root."""
    if folder_path:
        url = f"{_GRAPH}/me/drive/root:/{folder_path.strip('/')}:/children"
    else:
        url = f"{_GRAPH}/me/drive/root/children"
    params = {
        "$top": max(1, min(int(max_results or 25), 200)),
        "$select": "id,name,size,webUrl,lastModifiedDateTime,file,folder",
    }
    result = await oauth_api_call(user_id, agent_id, "microsoft", "GET", url, params=params)
    if result.get("status") == "not_connected":
        return not_connected_payload("microsoft")
    return json.dumps(result)


async def onedrive_search(user_id: str, agent_id: str, query: str, max_results: int = 25) -> str:
    if not query:
        return json.dumps({"status": "error", "message": "query required"})
    url = f"{_GRAPH}/me/drive/root/search(q='{query}')"
    result = await oauth_api_call(
        user_id, agent_id, "microsoft", "GET", url,
        params={"$top": max(1, min(int(max_results or 25), 200)),
                "$select": "id,name,size,webUrl,lastModifiedDateTime,file,folder"},
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("microsoft")
    return json.dumps(result)


async def onedrive_get_file(user_id: str, agent_id: str, file_id: str, download: bool = False) -> str:
    if not file_id:
        return json.dumps({"status": "error", "message": "file_id required"})

    if not download:
        result = await oauth_api_call(
            user_id, agent_id, "microsoft", "GET",
            f"{_GRAPH}/me/drive/items/{file_id}",
            params={"$select": "id,name,size,webUrl,lastModifiedDateTime,file,parentReference"},
        )
        if result.get("status") == "not_connected":
            return not_connected_payload("microsoft")
        return json.dumps(result)

    tok = await get_oauth_token(user_id, agent_id, "microsoft")
    if not tok:
        return not_connected_payload("microsoft")
    return await _binary_download(f"{_GRAPH}/me/drive/items/{file_id}/content", None, tok["access_token"])


# ── Dropbox ───────────────────────────────────────────────────────────────

_DBX = "https://api.dropboxapi.com/2"
_DBX_CONTENT = "https://content.dropboxapi.com/2"


async def dropbox_list_files(user_id: str, agent_id: str, path: str = "", recursive: bool = False, max_results: int = 50) -> str:
    """List Dropbox folder. path empty = root."""
    body = {
        "path": path or "",
        "recursive": bool(recursive),
        "limit": max(1, min(int(max_results or 50), 2000)),
    }
    result = await oauth_api_call(
        user_id, agent_id, "dropbox", "POST",
        f"{_DBX}/files/list_folder",
        json_body=body,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("dropbox")
    return json.dumps(result)


async def dropbox_search(user_id: str, agent_id: str, query: str, max_results: int = 25) -> str:
    if not query:
        return json.dumps({"status": "error", "message": "query required"})
    body = {
        "query": query,
        "options": {"max_results": max(1, min(int(max_results or 25), 1000))},
    }
    result = await oauth_api_call(
        user_id, agent_id, "dropbox", "POST",
        f"{_DBX}/files/search_v2",
        json_body=body,
    )
    if result.get("status") == "not_connected":
        return not_connected_payload("dropbox")
    return json.dumps(result)


async def dropbox_download(user_id: str, agent_id: str, path: str) -> str:
    """Download a Dropbox file. Path is the Dropbox file path (e.g. '/folder/file.txt')."""
    if not path:
        return json.dumps({"status": "error", "message": "path required"})
    tok = await get_oauth_token(user_id, agent_id, "dropbox")
    if not tok:
        return not_connected_payload("dropbox")

    headers = {
        "Authorization": f"Bearer {tok['access_token']}",
        "Dropbox-API-Arg": json.dumps({"path": path}),
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{_DBX_CONTENT}/files/download", headers=headers)
    except Exception as e:
        return json.dumps({"status": "error", "http_status": 0, "body": str(e)})

    return _wrap_binary_resp(resp)


# ── Shared binary helpers ─────────────────────────────────────────────────

async def _binary_download(url: str, params, access_token: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except Exception as e:
        return json.dumps({"status": "error", "http_status": 0, "body": str(e)})
    return _wrap_binary_resp(resp)


def _wrap_binary_resp(resp) -> str:
    ctype = resp.headers.get("content-type", "").lower()
    payload: dict = {"status": "ok" if resp.is_success else "error", "http_status": resp.status_code, "content_type": ctype}

    if not resp.is_success:
        payload["body"] = (resp.text or "")[:1000]
        return json.dumps(payload)

    if any(t in ctype for t in ("text/", "json", "csv", "xml", "html")):
        try:
            payload["body"] = resp.content.decode("utf-8", errors="replace")
            payload["encoding"] = "text"
        except Exception:
            payload["body"] = _b64.b64encode(resp.content).decode()
            payload["encoding"] = "base64"
    else:
        payload["body"] = _b64.b64encode(resp.content).decode()
        payload["encoding"] = "base64"
    payload["bytes"] = len(resp.content)
    return json.dumps(payload)


# ── Tool registry ─────────────────────────────────────────────────────────

TOOLS = [
    # ── Google Drive ──
    {
        "name": "drive_list_files",
        "provider": "google",
        "handler": drive_list_files,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "query":     {"type": "string",  "description": "Drive query, e.g. \"name contains 'budget'\".", "default": ""},
                "page_size": {"type": "integer", "description": "Max files (1-100).", "default": 20},
                "order_by":  {"type": "string",  "description": "Sort, e.g. 'modifiedTime desc' or 'name'.", "default": "modifiedTime desc"},
            },
            "required": [],
        },
    },
    {
        "name": "drive_get_file",
        "provider": "google",
        "handler": drive_get_file,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "file_id":          {"type": "string",  "description": "Drive file ID."},
                "download":         {"type": "boolean", "description": "True = return file contents; false = metadata only.", "default": False},
                "export_mime_type": {"type": "string",  "description": "For Google Docs/Sheets/Slides, export target (e.g. 'text/plain').", "default": ""},
            },
            "required": ["file_id"],
        },
    },

    # ── OneDrive ──
    {
        "name": "onedrive_list_files",
        "provider": "microsoft",
        "handler": onedrive_list_files,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string",  "description": "Folder path relative to root, e.g. 'Documents/Work'. Empty = root.", "default": ""},
                "max_results": {"type": "integer", "description": "Max items (1-200).", "default": 25},
            },
            "required": [],
        },
    },
    {
        "name": "onedrive_search",
        "provider": "microsoft",
        "handler": onedrive_search,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",  "description": "Free-text search across the user's OneDrive."},
                "max_results": {"type": "integer", "description": "Max matches (1-200).", "default": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "onedrive_get_file",
        "provider": "microsoft",
        "handler": onedrive_get_file,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "file_id":  {"type": "string",  "description": "OneDrive item ID."},
                "download": {"type": "boolean", "description": "True = return file contents; false = metadata only.", "default": False},
            },
            "required": ["file_id"],
        },
    },

    # ── Dropbox ──
    {
        "name": "dropbox_list_files",
        "provider": "dropbox",
        "handler": dropbox_list_files,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "path":        {"type": "string",  "description": "Folder path (e.g. '/Documents'). Empty = root.", "default": ""},
                "recursive":   {"type": "boolean", "description": "Walk subfolders.", "default": False},
                "max_results": {"type": "integer", "description": "Max items (1-2000).", "default": 50},
            },
            "required": [],
        },
    },
    {
        "name": "dropbox_search",
        "provider": "dropbox",
        "handler": dropbox_search,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",  "description": "Search text."},
                "max_results": {"type": "integer", "description": "Max matches (1-1000).", "default": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "dropbox_download",
        "provider": "dropbox",
        "handler": dropbox_download,
        "stages": ["execute_tools"],
        "destructive": False,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Full Dropbox file path, e.g. '/folder/file.txt'."},
            },
            "required": ["path"],
        },
    },
]
