"""
Pydantic models for agent data structures.
"""

from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


# ── Existing models ──────────────────────────────────────────────────────────


class ContextDocumentPayload(BaseModel):
    """Context slice forwarded from the Web Portal (doc_type matches DB context_type values)."""

    doc_type: str
    title: str
    content: str


class HistoryMessagePayload(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    """Request model for POST /chat"""

    user_id: str
    session_id: str
    message: str
    documents: Optional[List[ContextDocumentPayload]] = None
    history: Optional[List[HistoryMessagePayload]] = None
    attachment_ids: Optional[List[str]] = None
    agent_template_id: Optional[str] = None  # request a specific agent template for this session
    agent_id: Optional[str] = None  # request a specific custom agent by ID for this session
    execution_mode: Optional[str] = 'write'  # 'read' | 'write' | 'auto' — controls tool execution permission


class ChatResponse(BaseModel):
    """Response model for POST /chat (response mirrors Next.js client expectation)."""

    reply: str
    response: str
    session_id: str


class DocumentCreate(BaseModel):
    """Request model for creating a context row"""

    context_type: str
    title: str
    content: str
    tags: Optional[List[str]] = None


class DocumentResponse(BaseModel):
    """Row shape for public.context"""

    id: str
    user_id: str
    context_type: str
    title: str
    content: str
    tags: List[str]
    created_at: datetime
    updated_at: datetime


class InteractionRecord(BaseModel):
    """Internal representation of an interactions row

    Each interaction is a single atomic unit:
      - user:     the user's message
      - assistant: a complete assistant response (text + any tool calls)
      - tool:     a single tool result tied to a tool_call_id
    """

    id: str
    session_id: str
    parent_id: Optional[str] = None
    role: str  # 'user', 'assistant', 'tool'
    content: str
    tool_name: Optional[str] = None       # for tool role
    tool_call_id: Optional[str] = None    # for tool role
    channel: Optional[str] = None         # source: 'webagent_ui' | 'telegram' | 'web_portal' | 'api'
    metadata: Optional[str] = None        # JSON: model, turn, duration, tokens
    input: Optional[str] = None           # JSON: the exact messages array sent to LLM
    output: Optional[str] = None          # JSON: the complete response/result for this interaction
    template_id: Optional[str] = None     # agent template id used for this interaction
    from_id: Optional[str] = None         # user id or agent id who sent it
    to_id: Optional[str] = None           # user id or agent id who received it
    source: Optional[str] = None          # e.g. 'optimizer:init', 'optimizer:prefilter'
    session_seq: Optional[int] = None     # monotonic order within session (NULL on legacy rows)
    turn_id: Optional[str] = None         # uuid of the user message that started this turn
    turn_seq: Optional[int] = None        # order within turn (0, 1, 2, ...)
    created_at: datetime


class SeedDocsRequest(BaseModel):
    """Request model for POST /seed-docs"""

    user_id: str


# ── New models for tool system ────────────────────────────────────────────────


class ToolRecord(BaseModel):
    """Representation of a tool in the tools table."""

    id: str
    name: str
    code: str
    description: str
    parameters: Dict[str, Any]
    language: str = "python"
    status: str = "active"  # active, deprecated
    created_by: str
    stages: Optional[str] = "[]"       # JSON array of loop node IDs where this tool fires
    destructive: int = 0                # 1 = requires confirmation before running
    agent_types: Optional[str] = "[]"  # JSON array of agent type names; empty = all agents
    created_at: datetime
    updated_at: datetime


class ToolExecutionRecord(BaseModel):
    """Representation of a tool execution in the tool_executions table."""

    id: str
    tool_name: str
    user_id: str
    session_id: str
    interaction_id: Optional[str] = None
    success: bool
    duration_ms: int
    error_message: Optional[str] = None
    input_params: Optional[Dict[str, Any]] = None
    output_result: Optional[str] = None
    created_at: datetime


class CredentialRecord(BaseModel):
    """Representation of a credential in the agent_credentials table."""

    id: str
    user_id: str
    tool_name: str
    credential_type: str  # api_key, oauth_token, session_cookie, password, app_password
    encrypted_data: str
    display_name: Optional[str] = None
    expires_at: Optional[datetime] = None
    scopes: Optional[List[str]] = None
    last_used_at: Optional[datetime] = None
    use_count: int
    is_active: bool
    requires_renewal: bool
    created_at: datetime
    updated_at: datetime


# ── Agent models ──────────────────────────────────────────────────────────────────


class AgentTemplate(BaseModel):
    """Blueprint for new agents. Read-only after seed."""

    id: str
    system_prompt: str
    max_turn_count: int
    max_wall_seconds: Optional[float] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 4096
    metadata: str = "{}"
    created_at: datetime
    updated_at: datetime


class AgentRecord(BaseModel):
    """Assigned agent — one per user."""

    id: str
    user_id: str
    system_prompt: str
    max_turn_count: int = 9999  # bounded "effectively unlimited" default; 0 = truly unlimited
    max_wall_seconds: Optional[float] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 4096
    status: str = "active"
    metadata: str = "{}"
    assigned_at: datetime
    created_at: datetime
    updated_at: datetime
    turn_count: int = 0


class UserRecord(BaseModel):
    id: str
    name: str
    email: str
    role: str
    provider: str
    avatar_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class SessionRecord(BaseModel):
    id: str
    user_id: str
    agent_id: str
    summary: Optional[str] = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class CreateToolRequest(BaseModel):
    """Request model for creating/updating a tool."""

    name: str
    description: str
    parameters: Dict[str, Any]
    code: str
    change_summary: Optional[str] = None


class ReviewToolRequest(BaseModel):
    """Request model for reviewing/approving a tool."""

    tool_id: str
    action: str  # approve, reject
    reason: Optional[str] = None  # Required for reject


class ToolValidationResult(BaseModel):
    """Structured result from tool-call validation (same fields as prior dataclass)."""

    is_valid: bool
    error_message: Optional[str] = None
    corrected_name: Optional[str] = None
    corrected_args: Optional[dict] = None
