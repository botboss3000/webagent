from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_live_activity_note_never_moves_its_group_to_the_transcript_tail():
    source = _source("ui/chat/js/chat-stream.js")
    start = source.index("function mirrorActivityNote(")
    end = source.index("function mirrorActivityEnd(", start)
    body = source[start:end]

    assert "positionActivityGroupAfterOwner(ownerTurnId, bubble)" in body
    assert "app.chatMessages.appendChild(bubble)" not in body


def test_task_grouping_has_no_visible_chat_chrome():
    ui = _source("ui/chat/js/chat-ui.js")
    css = _source("ui/shared/css/app1.css")

    assert "initTaskFrames" not in ui
    assert ".task-frame" not in css


def test_mode_changes_are_counted_and_rendered_content_only_in_activity_group():
    stream = _source("ui/chat/js/chat-stream.js")
    load = _source("ui/chat/js/session-load.js")
    css = _source("ui/shared/css/design-system.css")

    assert "const modeChanges = rows.filter(row => row.classList.contains('ca-mode-row')).length" in stream
    assert "' mode change' : ' mode changes'" in stream
    assert "if (statusRow && entry.notice !== 'mode')" in stream
    assert "if (entry.notice === 'mode')" in stream
    assert "knownModeNotices.has(String(entry.label || '').trim())" in stream
    assert "if (src === 'system:mode')" in load
    assert "kind: 'system', notice: 'mode'" in load
    assert ".chat-bubble.info.system-mode .turn-gutter" in css
    assert "display: none !important" in css


def test_agent_mode_notice_is_persisted_with_owning_turn():
    loader = _source("app/tools/loader.py")
    loop = _source("app/agent/loop.py")

    assert "turn_id=turn_id or None" in loader
    assert 'turn_id=parent_interaction_id or ""' in loop


def test_cached_visit_uses_one_non_ordered_sync_tail_indicator():
    load = _source("ui/chat/js/session-load.js")
    css = _source("ui/shared/css/app1.css")

    assert "wrap.className = 'chat-sync-tail'" in load
    assert "Server connection is currently unavailable." in load
    assert "if (storageAdapter.isHybrid)" in load
    assert "_refreshCachedTranscript(sessionId);" in load
    assert "width: 30%;" in css
    assert "chat-sync-tail .chat-skeleton-line" in css


def test_tool_details_are_fetched_per_row_not_per_group():
    stream = _source("ui/chat/js/chat-stream.js")
    api = _source("app/api/db_viewer.py")

    assert "/session-tool-detail" in stream
    assert "_ensureToolCallDetail(panel.closest('.bubble-tool-calls'), rowEntry)" in stream
    assert "_ensureToolDetail(container)" not in stream
    assert '@router.get("/session-tool-detail")' in api
    assert '"tool_index": tool_index' in api


def test_codex_portal_sessions_use_native_transcript_api_and_survive_cache_deltas():
    send = _source("ui/chat/js/chat-send.js")
    load = _source("ui/chat/js/session-load.js")
    sessions = _source("ui/chat/elements/session-dropdown/list.js")

    assert "/api/v1/agents/${encodeURIComponent(app.currentAgentId)}" in send
    assert "activeAgent = detail.agent" in send
    assert "activeAgent.default_execution_mode" in send
    assert "execution_mode: portalExecutionMode" in send
    assert "/api/v1/engines/codex/portal/threads" in send
    assert "if (pendingRefresh) await pendingRefresh.catch(() => {});" in send
    assert "if (portalError) addChatBubble('agent'" in send
    assert "if (data.status === 'queued')" in send
    assert "Message queued in the active Codex task." in send
    assert "/api/v1/engines/codex/portal/threads/${encodeURIComponent(sessionId)}/messages" in load
    assert "headers: authHeaders()" in load
    assert "return storageAdapter.getInteractions(sessionId, limit, opts);" not in load[
        load.index("if (typeof sessionId === 'string' && sessionId.startsWith('codex:'))"):
        load.index("// Hybrid + browser + normal", load.index("if (typeof sessionId === 'string' && sessionId.startsWith('codex:'))"))
    ]
    delta = sessions[sessions.index("window.addEventListener('sessions-delta'"):]
    assert "_sessionsCache = _mergePortalSessions(" in delta
    controller = _source("ui/chat/elements/session-dropdown/controller.js")
    assert "menu.dataset.pointerSessionId = row?.dataset.id || '';" in controller
    assert "switchToSession(pointerSessionId || row.dataset.id)" in controller
    core = _source("ui/chat/js/session-core.js")
    assert "if (targetSess?.title)" in core
    assert "refreshedTarget.title = targetSess.title" in core


def test_codex_agent_sessions_are_the_complete_native_catalog():
    card = _source("ui/main-panel/agents/js/codex-agent.js")
    page = _source("ui/main-panel/agents/sessions/js/sessions-page.js")
    api = _source("plugins/engines/api.py")

    assert "Choose native Codex tasks" not in card
    assert "_mountPortalPicker" not in card
    assert "nativeCodex: agent.codex_code?.context_mode === 'codex_portal'" in card
    assert "async function _fetchNativeCodexSessions()" in page
    assert "const context = _nativeCodexContext;" in page
    assert "if (_nativeCodexContext !== context || app.currentUserId !== userId) return [];" in page
    assert "import { authHeaders } from '../../../../shared/js/left-login.js';" in page
    assert "limit: '200'" in page
    assert "if (cursor) qs.set('cursor', cursor)" in page
    assert "if (_nativeCodexContext) return _fetchNativeCodexSessions();" in page
    assert "viewButton.hidden = !!nextNativeCodex" in page
    assert "await _portal_agent_config(uid, agent_id)" in api
    assert "await _portal_agent_config(uid, body.agent_id)" in api
