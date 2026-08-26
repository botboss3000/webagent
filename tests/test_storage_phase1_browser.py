"""Real-browser IndexedDB integration coverage for the Phase 1 cache contract."""

from __future__ import annotations

import functools
import http.server
import threading
import unittest
import uuid
from pathlib import Path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class IndexedDBIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")
        root = Path(__file__).resolve().parents[1]
        handler = functools.partial(_QuietHandler, directory=str(root))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")
        cls.page = cls.browser.new_page()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.page.goto(f"{cls.base}/ui/chat/index.html")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()

    def _scope(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def test_scout_response_updates_in_place_and_is_ephemeral(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              document.body.appendChild(host);
              const previous = app.chatMessages;
              app.chatMessages = host;

              stream.showScoutResponse(
                'Gathering context now.', 'scout-preview:user-1', 'user-1',
                '2026-08-26T12:00:00Z', 'starting',
              );
              const first = host.querySelector('.scout-preview');
              const firstSnapshot = {
                count: host.querySelectorAll('.scout-preview').length,
                text: first?.querySelector('.llm-section')?.textContent.trim(),
                owner: first?.dataset.scoutOwner,
                provisional: first?.dataset.provisional,
              };

              stream.showScoutResponse(
                'I understand the goal and will inspect the lifecycle first.',
                'scout-preview:user-1', 'user-1',
                '2026-08-26T12:00:01Z', 'ready',
              );
              const updated = {
                count: host.querySelectorAll('.scout-preview').length,
                text: host.querySelector('.scout-preview .llm-section')?.textContent.trim(),
                sameNode: first === host.querySelector('.scout-preview'),
              };

              stream.showScoutResponse(
                'A replacement request is being oriented.',
                'scout-preview:user-2', 'user-2',
                '2026-08-26T12:00:02Z', 'starting',
              );
              const replacement = {
                count: host.querySelectorAll('.scout-preview').length,
                owner: host.querySelector('.scout-preview')?.dataset.scoutOwner,
              };

              stream.dismissScoutResponse('user-2');
              const remaining = host.querySelectorAll('.scout-preview').length;
              host.remove();
              app.chatMessages = previous;
              return {firstSnapshot, updated, replacement, remaining};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["firstSnapshot"], {
            "count": 1,
            "text": "Gathering context now.",
            "owner": "user-1",
            "provisional": "true",
        })
        self.assertEqual(result["updated"], {
            "count": 1,
            "text": "I understand the goal and will inspect the lifecycle first.",
            "sameNode": True,
        })
        self.assertEqual(result["replacement"], {"count": 1, "owner": "user-2"})
        self.assertEqual(result["remaining"], 0)

    def test_streamed_agent_message_reuses_saved_structure_without_duplication(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              host.id = 'stream-message-test';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeTurnModel: app._activeTurnModel,
                activeTurnEffort: app._activeTurnEffort,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                isProcessing: app.isProcessing,
                agentBuffer: app.agentBuffer,
                turnHasBubble: app._turnHasBubble,
              };
              app.chatMessages = host;
              app.currentSessionId = 'stream-test-session';
              app._activeTurnModel = 'test-model';
              app._activeTurnEffort = 'high';

              stream.appendStreamToActiveBubble(
                'Hello', 'assistant-1', '2026-08-07T12:00:00Z'
              );
              stream.appendStreamToActiveBubble(
                ' world', 'assistant-1', '2026-08-07T12:00:00Z'
              );
              // Stream paints are intentionally coalesced so token-rate events
              // cannot force a full layout for every chunk.
              await new Promise(resolve => setTimeout(resolve, 125));

              const snapshot = () => ({
                bubbles: host.querySelectorAll('.chat-bubble.agent').length,
                sections: host.querySelectorAll(':scope > .chat-bubble.agent > .turn-section.llm-section').length,
                gutters: host.querySelectorAll(':scope > .chat-bubble.agent > .turn-gutter').length,
                text: host.querySelector('.turn-section.llm-section')?.textContent,
                model: host.querySelector('.chat-bubble.agent')?.dataset.modelLabel,
                time: host.querySelectorAll('.turn-gutter-time').length,
                buttons: host.querySelectorAll('.turn-gutter button').length,
                deletes: host.querySelectorAll('.turn-gutter .bubble-delete-btn').length,
                streaming: host.querySelectorAll('.chat-bubble.agent.streaming').length,
                turnId: host.querySelector('.chat-bubble.agent')?.dataset.turnId,
                msgId: host.querySelector('.chat-bubble.agent')?.dataset.msgId,
              });

              const live = snapshot();
              const section = host.querySelector('.turn-section.llm-section');
              const gutter = host.querySelector('.turn-gutter');
              section.click();
              const liveHiddenAfterClick = gutter.hasAttribute('hidden');
              section.click();
              const liveShownAfterSecondClick = !gutter.hasAttribute('hidden');
              stream.finalizeAgentResponse('Hello world', 'assistant-1');
              const saved = snapshot();
              section.click();
              const savedHiddenAfterClick = gutter.hasAttribute('hidden');
              section.click();
              const savedShownAfterSecondClick = !gutter.hasAttribute('hidden');
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeTurnModel = previous.activeTurnModel;
              app._activeTurnEffort = previous.activeTurnEffort;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app.isProcessing = previous.isProcessing;
              app.agentBuffer = previous.agentBuffer;
              app._turnHasBubble = previous.turnHasBubble;
              return {
                live,
                saved,
                liveHiddenAfterClick,
                liveShownAfterSecondClick,
                savedHiddenAfterClick,
                savedShownAfterSecondClick,
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["live"],
            {
                "bubbles": 1,
                "sections": 1,
                "gutters": 1,
                "text": "Hello world",
                "model": "test-model HIGH",
                "time": 1,
                "buttons": 6,
                "deletes": 0,
                "streaming": 1,
                "turnId": "assistant-1",
                "msgId": "assistant-1",
            },
        )
        self.assertEqual(result["saved"]["bubbles"], 1)
        self.assertEqual(result["saved"]["sections"], 1)
        self.assertEqual(result["saved"]["gutters"], 1)
        self.assertEqual(result["saved"]["text"], "Hello world")
        self.assertEqual(result["saved"]["model"], "test-model HIGH")
        self.assertEqual(result["saved"]["time"], 1)
        self.assertEqual(result["saved"]["buttons"], 7)
        self.assertEqual(result["saved"]["deletes"], 1)
        self.assertEqual(result["saved"]["streaming"], 0)
        self.assertFalse(result["liveHiddenAfterClick"], result)
        self.assertTrue(result["liveShownAfterSecondClick"])
        self.assertFalse(result["savedHiddenAfterClick"])
        self.assertTrue(result["savedShownAfterSecondClick"])

    def test_virtual_scroll_evicts_bubbles_that_leave_viewport(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const cache = await import(base + '/ui/chat/js/chat-message-cache.js');
              const virtual = await import(base + '/ui/chat/js/chat-virtual-scroll.js');
              await import(base + '/ui/chat/js/chat-bubble.js');

              const scroller = document.createElement('div');
              scroller.style.cssText = 'height:240px;overflow:auto;position:relative';
              const host = document.createElement('div');
              scroller.appendChild(host);
              document.body.appendChild(scroller);
              const previous = {
                chatMessages: app.chatMessages,
                chatScroller: app._chatScroller,
                currentSessionId: app.currentSessionId,
              };
              app.chatMessages = host;
              app._chatScroller = scroller;
              app.currentSessionId = 'virtual-test-session';

              const messages = [];
              for (let i = 0; i < 100; i++) {
                const id = `virtual-${i}`;
                messages.push({id, role: 'assistant', content: `Message ${i}`, session_seq: i});
                app._agentTurnBubble = null;
                const bubble = app.addChatBubble('agent', `Message ${i}`, null, null, null, id);
                bubble.style.flex = '0 0 80px';
                bubble.style.height = '80px';
                bubble.style.minHeight = '80px';
              }
              cache._messageCache.set(app.currentSessionId, {
                messages, light: true, loadedAt: Date.now(),
              });

              virtual._installVirtualScroll();
              await new Promise(resolve => requestAnimationFrame(resolve));
              const initial = {
                real: host.querySelectorAll('.chat-bubble').length,
                placeholders: host.querySelectorAll('.chat-bubble-placeholder').length,
              };
              scroller.scrollTop = scroller.scrollHeight;
              scroller.dispatchEvent(new Event('scroll'));
              await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
              const after = {
                real: host.querySelectorAll('.chat-bubble').length,
                placeholders: host.querySelectorAll('.chat-bubble-placeholder').length,
                lastVisible: host.lastElementChild?.textContent,
              };

              virtual._teardownVirtualScroll();
              cache._messageCache.delete(app.currentSessionId);
              scroller.remove();
              app.chatMessages = previous.chatMessages;
              app._chatScroller = previous.chatScroller;
              app.currentSessionId = previous.currentSessionId;
              return {initial, after};
            }""",
            {"base": self.base},
        )
        self.assertLess(result["initial"]["real"], 20)
        self.assertGreater(result["initial"]["placeholders"], 80)
        self.assertLess(result["after"]["real"], 20)
        self.assertGreater(result["after"]["placeholders"], 80)
        self.assertIn("Message 99", result["after"]["lastVisible"])

    def test_streamed_tool_call_batches_share_one_summary_line(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
                isProcessing: app.isProcessing,
              };
              app.chatMessages = host;
              app.currentSessionId = 'stream-tool-summary-test';
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;

              stream.appendStreamToActiveBubble('First step', 'assistant-tool-1');
              stream.attachToolCallsToLastBubble([
                {tool: 'search', args: {}, status: 'done'},
                {tool: 'read', args: {}, status: 'done'},
              ]);
              stream.finalizeAgentStep('First step', 'assistant-tool-1');

              // A later streamed inference step merges into the same agent bubble.
              bubbles.addChatBubble('agent', 'Second step', 'streaming', undefined, 'assistant-tool-2');
              stream.attachToolCallsToLastBubble([
                {tool: 'write', args: {}, status: 'done'},
              ]);
              stream.finalizeAgentStep('Second step', 'assistant-tool-2');

              const summary = {
                bubbles: host.querySelectorAll('.chat-bubble.agent').length,
                toolSections: host.querySelectorAll('.turn-section.tool-section').length,
                containers: host.querySelectorAll('.bubble-tool-calls').length,
                rows: host.querySelectorAll('.bubble-tool-calls-panel > .ca-tool-row').length,
                heading: host.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              app.isProcessing = previous.isProcessing;
              return summary;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["bubbles"], 1)
        self.assertEqual(result["toolSections"], 1)
        self.assertEqual(result["containers"], 1)
        self.assertEqual(result["rows"], 5)  # 3 tools + 2 bundled progress messages
        self.assertIn("3 tool calls", result["heading"])

    def test_activity_group_interleaves_progress_and_tools_by_turn(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              const before = bubbles.addChatBubble(
                'user', 'before', undefined, undefined, undefined, 'before',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(before, 10);
              const resumed = bubbles.addChatBubble(
                'user', 'resumed', undefined, undefined, undefined, 'resumed',
                '2026-08-07T12:00:00Z',
              );
              bubbles._setBubbleSessionSeq(resumed, 40);

              stream.attachActivityEntries([
                {tool: 'one', toolCallId: 'tc-1', args: {}, status: 'done'},
                {tool: 'two', toolCallId: 'tc-2', args: {}, status: 'done'},
                {kind: 'progress', id: 'progress-1', content: 'First update'},
                {tool: 'three', toolCallId: 'tc-3', args: {}, status: 'done'},
                {kind: 'progress', id: 'progress-2', content: 'Second update'},
              ], null, {
                id: 'progress-1',
                interactionSeq: 20,
                turnId: 'user-turn',
                createdAt: '2026-08-07T11:00:00Z',
              }, 'user-turn');

              const group = host.querySelector('.activity-group');
              const rowKinds = Array.from(
                group.querySelectorAll('.bubble-tool-calls-panel > .ca-tool-row'),
              ).map(row => row.classList.contains('ca-progress-row')
                ? 'progress' : row.querySelector('.ca-tool-name')?.textContent);
              const order = Array.from(host.children).map(el =>
                el.classList.contains('activity-group') ? 'activity'
                  : el.querySelector('.bubble-body')?.textContent,
              );
              const heading = group.querySelector('.bubble-tool-calls-head')?.textContent.trim();

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {rowKinds, order, heading};
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["rowKinds"],
            ["one", "two", "progress", "three", "progress"],
        )
        self.assertEqual(result["order"], ["before", "activity", "resumed"])
        self.assertIn("3 tool calls", result["heading"])
        self.assertIn("2 updates", result["heading"])

    def test_persisted_tools_updates_and_responses_share_one_run_bubble(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              bubbles.addChatBubble(
                'user', 'Run it', undefined, undefined,
                undefined, 'user-run', '2026-08-21T12:00:00Z',
              );

              stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'tool-1', args: {}, status: 'done'},
              ], null, {id: 'step-1', activityGroupId: 'segment-1',
                        turnId: 'user-run', interactionSeq: 10}, 'user-run');
              stream.attachActivityEntries([
                {kind: 'progress', id: 'update-1', content: 'Checking results'},
              ], null, {id: 'step-2', activityGroupId: 'segment-2',
                        turnId: 'user-run', interactionSeq: 20}, 'user-run');
              stream.attachActivityEntries([
                {kind: 'response', id: 'response-1', content: 'The result is ready'},
              ], null, {id: 'step-3', activityGroupId: 'segment-3',
                        turnId: 'user-run', interactionSeq: 30}, 'user-run');

              const groups = host.querySelectorAll(':scope > .activity-group');
              const group = groups[0];
              const rows = Array.from(group.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-tool-row',
              )).map(row => row.classList.contains('ca-progress-row') ? 'update'
                : row.classList.contains('ca-response-row') ? 'response' : 'tool');
              const result = {
                groups: groups.length,
                containers: group.querySelectorAll('.bubble-tool-calls').length,
                rows,
                heading: group.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return result;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["containers"], 1)
        self.assertEqual(result["rows"], ["tool", "update", "response"])
        self.assertIn("1 tool call", result["heading"])
        self.assertIn("1 update", result["heading"])
        self.assertIn("1 response", result["heading"])

    def test_live_activity_reuses_turn_group_after_active_pointer_resets(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              stream.attachActivityEntries([
                {tool: 'search', toolCallId: 'call-1', args: {}, status: 'done'},
              ], null, {
                id: 'assistant-step-1', turnId: 'user-turn', interactionSeq: 10,
              }, 'user-turn');

              // Simulate a stream/reconcile boundary clearing transient state.
              app._activeToolGroupBubble = null;
              stream.attachActivityEntries([
                {kind: 'progress', id: 'assistant-step-2', content: 'Update two'},
                {tool: 'read', toolCallId: 'call-2', args: {}, status: 'done'},
              ], null, {
                id: 'assistant-step-2', turnId: 'user-turn', interactionSeq: 20,
              }, 'user-turn');

              const groups = host.querySelectorAll(':scope > .activity-group');
              const heading = groups[0]?.querySelector('.bubble-tool-calls-head')?.textContent;
              const rows = groups[0]?.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-tool-row',
              ).length || 0;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {groups: groups.length, heading, rows};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["rows"], 3)
        self.assertIn("2 tool calls", result["heading"])
        self.assertIn("1 update", result["heading"])

    def test_live_activity_coalesces_different_backend_turn_ids(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              const user = bubbles.addChatBubble(
                'user', 'Do the work', undefined, undefined,
                undefined, 'visible-user-turn', '2026-08-12T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(user, 1);

              // Reproduce recovery/inference segments already rendered as
              // separate disclosures with different backend turn identities.
              stream.attachActivityEntries([
                {tool: 'one', toolCallId: 'one', args: {}, status: 'done'},
              ], null, {id: 'a1', activityGroupId: 'segment-1',
                        turnId: 'backend-turn-1', interactionSeq: 10}, 'backend-turn-1');
              stream.attachActivityEntries([
                {kind: 'progress', id: 'p2', content: 'Second update'},
                {tool: 'two', toolCallId: 'two', args: {}, status: 'done'},
              ], null, {id: 'a2', activityGroupId: 'segment-2',
                        turnId: 'backend-turn-2', interactionSeq: 20}, 'backend-turn-2');
              app._activeToolGroupBubble = null;

              // The next live batch must coalesce every disclosure after the
              // latest visible user message, regardless of backend turn id.
              stream.attachActivityEntries([
                {kind: 'progress', id: 'p3', content: 'Third update'},
                {tool: 'three', toolCallId: 'three', args: {}, status: 'done'},
              ], null, {id: 'a3', turnId: 'backend-turn-3', interactionSeq: 30},
                 'backend-turn-3');

              const groups = host.querySelectorAll(':scope > .activity-group');
              const heading = groups[0]?.querySelector('.bubble-tool-calls-head')?.textContent;
              const rows = groups[0]?.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-tool-row',
              ).length || 0;
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {groups: groups.length, heading, rows};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["rows"], 5)
        self.assertIn("3 tool calls", result["heading"])
        self.assertIn("2 updates", result["heading"])

    def test_prepending_older_page_cannot_merge_old_final_into_latest_final(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              await import(base + '/ui/chat/js/chat-stream.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app.addChatBubble = bubbles.addChatBubble;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;

              session._renderSessionWindowed([
                {id: 'late-progress', role: 'assistant', content: 'finishing',
                 message_phase: 'progress', turn_id: 'user-2', session_seq: 220,
                 created_at: '2026-08-12T11:16:00Z', status: 'complete'},
                {id: 'final-2', role: 'assistant', content: 'changes complete',
                 message_phase: 'final', turn_id: 'user-2', session_seq: 300,
                 created_at: '2026-08-12T11:17:00Z', status: 'complete'},
              ], 'session', null, false, false, true);

              session._prependMessagesToTranscript([
                {id: 'old-progress', role: 'assistant', content: 'planning',
                 message_phase: 'progress', turn_id: 'user-1', session_seq: 20,
                 created_at: '2026-08-12T10:08:00Z', status: 'complete'},
                {id: 'final-1', role: 'assistant', content: 'shall I implement it?',
                 message_phase: 'final', turn_id: 'user-1', session_seq: 100,
                 created_at: '2026-08-12T10:10:00Z', status: 'complete'},
                {id: 'user-2', role: 'user', content: 'continue', turn_id: 'user-2',
                 session_seq: 101, created_at: '2026-08-12T11:09:00Z', status: 'complete'},
              ], true);

              const finals = Array.from(host.querySelectorAll('.chat-bubble.agent'))
                .filter(el => !el.classList.contains('activity-group'))
                .map(el => ({
                  seq: Number(el.dataset.sessionSeq),
                  text: el.textContent,
                  sections: el.querySelectorAll(':scope > .llm-section').length,
                }));
              const order = Array.from(host.children).map(el =>
                Number(el.dataset.sessionSeq),
              ).filter(Number.isFinite);
              const debug = Array.from(host.children).map(el => ({
                className: el.className, seq: el.dataset.sessionSeq,
                text: el.textContent,
              }));

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {finals, order, debug};
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            [item["seq"] for item in result["finals"]], [100, 300], result
        )
        self.assertIn("shall I implement it?", result["finals"][0]["text"])
        self.assertIn("changes complete", result["finals"][1]["text"])
        self.assertEqual([item["sections"] for item in result["finals"]], [1, 1])
        self.assertEqual(result["order"], sorted(result["order"]))

    def test_reprojection_coalesces_recovery_activity_into_one_run_bubble(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              await import(base + '/ui/chat/js/chat-stream.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app.addChatBubble = bubbles.addChatBubble;
              const messages = [
                {id: 'user', role: 'user', content: 'go', turn_id: 'user',
                 session_seq: 1, created_at: '2026-08-12T11:00:00Z'},
                {id: 'before-recovery', role: 'assistant', content: 'first work',
                 message_phase: 'progress', turn_id: 'user', session_seq: 10,
                 created_at: '2026-08-12T11:01:00Z', status: 'complete'},
                {id: 'recovery', role: 'system', content: 'Recovered and resumed',
                 session_seq: 15, created_at: '2026-08-12T11:02:00Z'},
                {id: 'after-recovery', role: 'assistant', content: 'resumed work',
                 message_phase: 'progress', turn_id: 'user', session_seq: 20,
                 created_at: '2026-08-12T11:03:00Z', status: 'complete'},
                {id: 'final', role: 'assistant', content: 'done',
                 message_phase: 'final', turn_id: 'user', session_seq: 30,
                 created_at: '2026-08-12T11:04:00Z', status: 'complete'},
              ];
              session._renderSessionWindowed(messages, 'session', null, false, false, true);
              const order = Array.from(host.children).map(el => ({
                seq: Number(el.dataset.sessionSeq),
                activity: el.classList.contains('activity-group'),
                text: el.textContent,
              }));
              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return order;
            }""",
            {"base": self.base},
        )
        self.assertEqual([item["seq"] for item in result], [1, 10, 15, 30])
        self.assertEqual([item["activity"] for item in result].count(True), 1)
        activity = next(item for item in result if item["activity"])
        self.assertIn("first work", activity["text"])
        self.assertIn("resumed work", activity["text"])
        self.assertIn("Recovered and resumed", result[2]["text"])

    def test_live_transcript_uses_time_only_until_durable_sequence_arrives(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;

              const saved = bubbles.addChatBubble(
                'user', 'saved-old', undefined, undefined, undefined, 'saved-old',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(saved, 10);
              const recent = bubbles.addChatBubble(
                'user', 'recent-live', undefined, undefined, undefined, 'recent-live',
                '2026-08-07T12:00:00Z',
              );
              // Simulate an older replay arriving after the recent live node.
              const delayed = bubbles.addChatBubble(
                'user', 'delayed-live', undefined, undefined, undefined, 'delayed-live',
                '2026-08-07T11:00:00Z',
              );
              const provisional = Array.from(host.children).map(
                el => el.querySelector('.bubble-body')?.textContent,
              );

              // Durable sequences replace timestamp fallback. Deliberately use
              // conflicting timestamps to prove time cannot override saved order.
              bubbles._setBubbleSessionSeq(delayed, 30);
              bubbles._setBubbleSessionSeq(recent, 20);
              const durable = Array.from(host.children).map(
                el => el.querySelector('.bubble-body')?.textContent,
              );

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return {provisional, durable};
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["provisional"],
            ["saved-old", "delayed-live", "recent-live"],
        )
        self.assertEqual(
            result["durable"],
            ["saved-old", "recent-live", "delayed-live"],
        )

    def test_stopped_marker_stays_at_interrupted_turn_after_resume(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                isProcessing: app.isProcessing,
              };
              app.chatMessages = host;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;

              const before = bubbles.addChatBubble(
                'user', 'before', undefined, undefined, undefined, 'before',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(before, 10);
              stream.markAgentInterrupted('stopped-turn', '2026-08-07T11:00:00Z');
              const resumed = bubbles.addChatBubble(
                'user', 'resumed', undefined, undefined, undefined, 'resumed',
                '2026-08-07T12:00:00Z',
              );
              bubbles._setBubbleSessionSeq(resumed, 40);

              const provisional = Array.from(host.children).map(el =>
                el.classList.contains('activity-group') ? 'Stopped'
                  : (el.querySelector('.bubble-body')?.textContent
                    || el.querySelector('.llm-section')?.textContent),
              );
              // DB-tail later supplies the interrupted interaction's durable row.
              stream.markAgentInterrupted(
                'stopped-turn', '2026-08-07T10:30:00Z', 20,
              );
              const stopped = host.querySelector('.activity-group');
              const durable = Array.from(host.children).map(el =>
                el.classList.contains('activity-group') ? 'Stopped'
                  : (el.querySelector('.bubble-body')?.textContent
                    || el.querySelector('.llm-section')?.textContent),
              );

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app.isProcessing = previous.isProcessing;
              return {
                provisional, durable,
                stoppedSeq: stopped?.getAttribute('data-session-seq'),
                stoppedCount: host.querySelectorAll(':scope > .activity-group').length,
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["provisional"], ["before", "Stopped", "resumed"])
        self.assertEqual(result["durable"], ["before", "Stopped", "resumed"])
        self.assertEqual(result["stoppedSeq"], "20")
        self.assertEqual(result["stoppedCount"], 1)

    def test_live_tool_group_uses_owning_assistant_sequence(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                agentTurnBubble: app._agentTurnBubble,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
              };
              app.chatMessages = host;
              app._agentTurnBubble = null;
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;

              const before = bubbles.addChatBubble(
                'user', 'before', undefined, undefined, undefined, 'before',
                '2026-08-07T10:00:00Z',
              );
              bubbles._setBubbleSessionSeq(before, 10);
              const resumed = bubbles.addChatBubble(
                'user', 'resumed', undefined, undefined, undefined, 'resumed',
                '2026-08-07T12:00:00Z',
              );
              bubbles._setBubbleSessionSeq(resumed, 40);

              stream.attachToolCallsToLastBubble([
                {tool: 'search', args: {}, status: 'done'},
                {tool: 'read', args: {}, status: 'done'},
              ], undefined, {
                id: 'assistant-tools', sessionSeq: 20,
                createdAt: '2026-08-07T11:00:00Z',
              });
              const toolBubble = host.querySelector('[data-msg-id="assistant-tools"]');
              const order = Array.from(host.children).map(el =>
                el.classList.contains('tool-only')
                  ? el.querySelector('.bubble-tool-calls-head')?.textContent.trim()
                  : el.querySelector('.bubble-body')?.textContent,
              );

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._agentTurnBubble = previous.agentTurnBubble;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              return {
                order,
                seq: toolBubble?.getAttribute('data-session-seq'),
                id: toolBubble?.getAttribute('data-msg-id'),
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["order"][0], "before")
        self.assertIn("2 tool calls", result["order"][1])
        self.assertEqual(result["order"][2], "resumed")
        self.assertEqual(result["seq"], "20")
        self.assertEqual(result["id"], "assistant-tools")

    def test_zz_folded_stream_uses_one_run_bubble_and_full_live_preview(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const prompts = await import(base + '/ui/shared/js/app-prompts.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
                turnHasBubble: app._turnHasBubble,
                fetch: window.fetch,
              };
              window.fetch = async () => new Response(JSON.stringify({chat_ui: {
                chat_common: {
                  fold_main_messages: true,
                  classify_main_messages: true,
                  show_tool_calls: true,
                  mirror_activity_in_transcript: true,
                  message_visibility: {defaults: {main: true, progress: true, tool: true}},
                },
              }}), {headers: {'Content-Type': 'application/json'}});
              await prompts.loadUiMessages();
              window.fetch = previous.fetch;
              app.chatMessages = host;
              app.currentSessionId = 'folded-session';
              app._activeToolGroupBubble = null;
              app._turnHasBubble = false;
              bubbles.addChatBubble('user', 'Do the work', undefined, undefined,
                undefined, 'user-turn', '2026-08-21T12:00:00Z');
              stream.mirrorActivityNote('Thinking…', 'folded-session', 'user-turn');

              stream.appendStreamToActiveBubble(
                'Alpha', 'step-1', '2026-08-21T12:00:01Z', 'user-turn', 2,
              );
              stream.appendStreamToActiveBubble(
                ' beta', 'step-1', '2026-08-21T12:00:01Z', 'user-turn', 2,
              );
              await new Promise(resolve => setTimeout(resolve, 125));
              stream.attachToolCallsToLastBubble([
                {tool: 'search', toolCallId: 'search-1', args: {}, status: 'done'},
              ], undefined, {id: 'step-1', turnId: 'user-turn', interactionSeq: 2});
              stream.appendStreamToActiveBubble(
                ' gamma', 'step-1', '2026-08-21T12:00:01Z', 'user-turn', 2,
              );
              await new Promise(resolve => setTimeout(resolve, 125));

              // Reproduce a late activity-pointer change. The next ticker
              // update must migrate/replace the existing mirror, not create a
              // second progress bubble.
              const foreign = bubbles.addChatBubble(
                'agent', '', 'tool-only activity-group', undefined,
                'foreign-group', 'foreign-group', '2026-08-21T12:00:01Z',
              );
              foreign.dataset.activityTurnId = 'foreign-turn';
              app._activeToolGroupBubble = foreign;
              stream.mirrorActivityNote(
                'Turn 2: Reading results', 'folded-session', 'user-turn',
              );
              foreign.remove();

              const live = {
                bubbles: host.querySelectorAll(':scope > .chat-bubble.agent').length,
                progressNotes: host.querySelectorAll('.live-activity-note').length,
                progressText: host.querySelector('.live-activity-label')?.textContent,
                progressIsBubbleTail: host.querySelector('.live-activity-note')
                  === host.querySelector('.live-activity-note')?.closest('.chat-bubble')?.lastElementChild,
                progressBubbleIsTranscriptTail: host.querySelector('.live-activity-note')
                  ?.closest('.chat-bubble') === host.lastElementChild,
                previews: host.querySelectorAll('.bubble-tool-calls-preview').length,
                previewText: host.querySelector('.bubble-tool-calls-preview .ca-activity-entry-body')?.textContent,
                heading: host.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
              };
              stream.finalizeAgentStep('Alpha beta gamma', 'step-1',
                '2026-08-21T12:00:02Z', 'user-turn', 2);
              stream.finalizeAgentResponse('Final answer', 'step-2', false,
                '2026-08-21T12:00:03Z', 'user-turn', 3);
              const final = {
                bubbles: host.querySelectorAll(':scope > .chat-bubble.agent').length,
                previews: host.querySelectorAll('.bubble-tool-calls-preview').length,
                previewText: host.querySelector('.bubble-tool-calls-preview .ca-activity-entry-body')?.textContent,
                heading: host.querySelector('.bubble-tool-calls-head')?.textContent.trim(),
                rows: host.querySelectorAll('.bubble-tool-calls-panel > .ca-tool-row').length,
              };
              host.remove();
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._turnHasBubble = previous.turnHasBubble;
              return {live, final};
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["live"]["bubbles"], 1)
        self.assertEqual(result["live"]["progressNotes"], 1)
        self.assertEqual(result["live"]["progressText"], "Turn 2: Reading results")
        self.assertTrue(result["live"]["progressIsBubbleTail"])
        self.assertTrue(result["live"]["progressBubbleIsTranscriptTail"])
        self.assertEqual(result["live"]["previews"], 1)
        self.assertEqual(result["live"]["previewText"], "Alpha beta gamma")
        self.assertIn("1 tool call", result["live"]["heading"])
        self.assertIn("1 live update", result["live"]["heading"])
        self.assertEqual(result["final"]["bubbles"], 1)
        self.assertEqual(result["final"]["previews"], 1)
        self.assertEqual(result["final"]["previewText"], "Final answer")
        self.assertIn("1 update", result["final"]["heading"])
        self.assertIn("1 response", result["final"]["heading"])
        self.assertEqual(result["final"]["rows"], 3)

    def test_folded_activity_preview_layout_uses_only_visible_content(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const stylePaths = [
                '/ui/shared/css/app1.css',
                '/ui/shared/css/design-system.css',
                '/ui/shared/css/index.css',
              ];
              const addedStyles = [];
              for (const path of stylePaths) {
                if (document.querySelector(`link[data-layout-test="${path}"]`)) continue;
                await new Promise((resolve, reject) => {
                  const link = document.createElement('link');
                  link.rel = 'stylesheet';
                  link.href = base + path;
                  link.dataset.layoutTest = path;
                  link.onload = resolve;
                  link.onerror = reject;
                  document.head.appendChild(link);
                  addedStyles.push(link);
                });
              }

              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const panelHost = document.createElement('div');
              panelHost.id = 'chat-panel';
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column;width:1000px';
              panelHost.appendChild(host);
              document.body.appendChild(panelHost);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              const completePreview = 'Here is the complete preview.\\n\\nThis final line must remain visible.';
              const group = stream.attachActivityEntries([
                {kind: 'response', id: 'older-response', content: 'X'.repeat(600)},
                {kind: 'response', id: 'latest-response', content: completePreview},
              ], null, {id: 'latest-response', turnId: 'user-layout'}, 'user-layout');
              const note = document.createElement('div');
              note.className = 'live-activity-note';
              note.innerHTML = '<span class="live-activity-label">Working…</span>';
              group.appendChild(note);
              const closer = bubbles.addChatBubble(
                'agent', 'Audited final answer', 'summary-bubble', undefined,
                undefined, 'closer-layout',
              );

              const panel = group.querySelector('.bubble-tool-calls-panel');
              const preview = group.querySelector('.bubble-tool-calls-preview');
              const body = preview.querySelector('.ca-activity-entry-body');
              const headingLabel = group.querySelector('.bubble-tool-calls-label');
              const noteLabel = note.querySelector('.live-activity-label');
              const style = getComputedStyle(group);
              const previewStyle = getComputedStyle(preview);
              const closerStyle = getComputedStyle(closer);
              const snapshot = {
                previewText: body.textContent,
                previewFullyVisible: body.scrollHeight <= body.clientHeight,
                groupWidth: group.getBoundingClientRect().width,
                hostWidth: host.getBoundingClientRect().width,
                panelDisplay: getComputedStyle(panel).display,
                background: style.backgroundColor,
                borderWidth: style.borderTopWidth,
                borderRadius: style.borderTopLeftRadius,
                backdropFilter: style.backdropFilter,
                overflow: style.overflow,
                previewBorderWidth: previewStyle.borderTopWidth,
                closerBorderWidth: closerStyle.borderTopWidth,
                closerBorderRadius: closerStyle.borderTopLeftRadius,
                headingLeft: headingLabel.getBoundingClientRect().left,
                progressLeft: noteLabel.getBoundingClientRect().left,
              };

              panelHost.remove();
              addedStyles.forEach(link => link.remove());
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result["previewText"],
            "Here is the complete preview.\n\nThis final line must remain visible.",
        )
        self.assertTrue(result["previewFullyVisible"])
        self.assertLess(result["groupWidth"], result["hostWidth"] * 0.6)
        self.assertEqual(result["panelDisplay"], "none")
        self.assertIn(result["background"], ("rgba(0, 0, 0, 0)", "transparent"))
        self.assertEqual(result["borderWidth"], "0px")
        self.assertEqual(result["borderRadius"], "0px")
        self.assertEqual(result["backdropFilter"], "none")
        self.assertEqual(result["overflow"], "visible")
        self.assertNotEqual(result["previewBorderWidth"], "0px")
        self.assertNotEqual(result["closerBorderWidth"], "0px")
        self.assertNotEqual(result["closerBorderRadius"], "0px")
        self.assertAlmostEqual(result["progressLeft"], result["headingLeft"], delta=0.5)

    def test_closer_orders_after_user_hides_matching_preview_and_has_no_header(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
                agentTurnBubble: app._agentTurnBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;
              app._agentTurnBubble = null;

              // Reproduce the race: folded response materializes before the
              // owning user bubble and has no useful timestamp/sequence yet.
              stream.attachActivityEntries(
                [{kind: 'response', id: 'assistant-1', content: 'Final answer'}],
                null,
                {id: 'assistant-1', turnId: 'user-1'},
                'user-1',
              );
              const previewBefore = host.querySelectorAll('.bubble-tool-calls-preview').length;
              const entryHeadersBefore = host.querySelectorAll(
                '.ca-progress-row > .ca-activity-entry-head, '
                  + '.ca-response-row > .ca-activity-entry-head',
              ).length;
              bubbles.addChatBubble(
                'user', 'Question', undefined, undefined, 'user-1', 'user-1',
              );

              const beforeMismatch = stream.suppressMatchingResponsePreview(
                'Different answer', 'assistant-1',
              );
              const previewAfterMismatch = host.querySelectorAll(
                '.bubble-tool-calls-preview',
              ).length;
              const closer = bubbles.addChatBubble(
                'agent', 'Final answer', 'summary-bubble', undefined,
                undefined, 'closer-1',
              );
              const matched = stream.suppressMatchingResponsePreview(
                'Final answer', 'assistant-1',
              );

              const order = Array.from(host.children).map(el => {
                if (el.classList.contains('user')) return 'user';
                if (el.classList.contains('activity-group')) return 'activity';
                if (el.classList.contains('summary-bubble')) return 'closer';
                return 'other';
              });
              const snapshot = {
                previewBefore,
                entryHeadersBefore,
                beforeMismatch,
                previewAfterMismatch,
                matched,
                previewAfter: host.querySelectorAll('.bubble-tool-calls-preview').length,
                responseRows: host.querySelectorAll(
                  '.bubble-tool-calls-panel > .ca-response-row',
                ).length,
                closerLabels: closer.querySelectorAll(':scope > .label').length,
                closerText: closer.textContent.trim(),
                order,
              };

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              app._agentTurnBubble = previous.agentTurnBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["previewBefore"], 1)
        self.assertEqual(result["entryHeadersBefore"], 0)
        self.assertFalse(result["beforeMismatch"])
        self.assertEqual(result["previewAfterMismatch"], 1)
        self.assertTrue(result["matched"])
        self.assertEqual(result["previewAfter"], 0)
        self.assertEqual(result["responseRows"], 1)
        self.assertEqual(result["closerLabels"], 0)
        self.assertEqual(result["closerText"], "Final answer")
        self.assertEqual(result["order"], ["user", "activity", "closer"])

    def test_persisted_same_owner_segments_do_not_cross_closer_boundaries(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const bubbles = await import(base + '/ui/chat/js/chat-bubble.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                activeToolGroupBubble: app._activeToolGroupBubble,
              };
              app.chatMessages = host;
              app._activeToolGroupBubble = null;

              const user = bubbles.addChatBubble(
                'user', 'Question', undefined, undefined, 'user-1', 'user-1',
              );
              bubbles._setBubbleSessionSeq(user, 1);
              const first = stream.attachActivityEntries(
                [{kind: 'response', id: 'response-a', content: 'First phase'}],
                null,
                {id: 'response-a', activityGroupId: 'segment-a', turnId: 'user-1', interactionSeq: 2},
                'user-1',
              );
              const closerA = bubbles.addChatBubble(
                'agent', 'Closer A', 'summary-bubble', undefined, undefined, 'closer-a',
              );
              bubbles._setBubbleSessionSeq(closerA, 3);
              const second = stream.attachActivityEntries(
                [{kind: 'response', id: 'response-b', content: 'Second phase'}],
                null,
                {id: 'response-b', activityGroupId: 'segment-b', turnId: 'user-1', interactionSeq: 4},
                'user-1',
              );
              const closerB = bubbles.addChatBubble(
                'agent', 'Closer B', 'summary-bubble', undefined, undefined, 'closer-b',
              );
              bubbles._setBubbleSessionSeq(closerB, 5);

              const order = Array.from(host.children).map(el =>
                el.dataset.activitySegmentId || el.dataset.msgId || el.dataset.turnId,
              );
              const responseIds = Array.from(host.querySelectorAll(
                '.bubble-tool-calls-panel > .ca-response-row',
              ))
                .map(row => row.__entry && row.__entry.id);
              const snapshot = {
                order,
                segmentCount: host.querySelectorAll('.activity-group').length,
                responseIds,
                firstIsDistinct: first !== second,
                duplicateMsgIds: Array.from(host.children)
                  .map(el => el.dataset.msgId)
                  .filter(Boolean)
                  .filter((id, i, ids) => ids.indexOf(id) !== i),
              };

              host.remove();
              app.chatMessages = previous.chatMessages;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertTrue(result["firstIsDistinct"])
        self.assertEqual(result["segmentCount"], 2)
        self.assertEqual(result["responseIds"], ["response-a", "response-b"])
        self.assertEqual(
            result["order"],
            ["user-1", "segment-a", "closer-a", "segment-b", "closer-b"],
        )
        self.assertEqual(result["duplicateMsgIds"], [])

    def test_refresh_message_bypasses_cache_and_repairs_panel_and_preview(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              await import(base + '/ui/chat/js/chat-bubble-actions.js');
              await import(base + '/ui/chat/js/session-load.js');
              const host = document.createElement('div');
              host.style.cssText = 'display:flex;flex-direction:column';
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
                activeToolGroupBubble: app._activeToolGroupBubble,
                fetch: window.fetch,
              };
              app.chatMessages = host;
              app.currentSessionId = 'refresh-repair-session';
              app._activeToolGroupBubble = null;
              stream.attachActivityEntries(
                [{kind: 'response', id: 'assistant-refresh', content: 'Short'}],
                null,
                {id: 'assistant-refresh', turnId: 'user-refresh'},
                'user-refresh',
              );

              let request = null;
              window.fetch = async (url, init) => {
                request = {url: String(url), cache: init && init.cache};
                return new Response(JSON.stringify({messages: [{
                  id: 'assistant-refresh', role: 'assistant',
                  content: 'Complete authoritative response', status: 'complete',
                  created_at: '2026-08-22T12:00:00Z', session_seq: 2,
                }]}), {headers: {'Content-Type': 'application/json'}});
              };

              const preview = host.querySelector('.bubble-tool-calls-preview');
              preview.querySelector('.turn-gutter-more').click();
              const refresh = document.querySelector(
                '.bubble-more-menu [data-refresh-row="1"]',
              );
              refresh.click();
              for (let i = 0; i < 30; i += 1) {
                if (preview.querySelector('.ca-activity-entry-body')?.textContent
                    === 'Complete authoritative response') break;
                await new Promise(resolve => setTimeout(resolve, 20));
              }

              const snapshot = {
                request,
                previewText: preview.querySelector('.ca-activity-entry-body')?.textContent,
                panelText: host.querySelector(
                  '.bubble-tool-calls-panel > .ca-response-row .ca-activity-entry-body',
                )?.textContent,
                sourceText: host.querySelector(
                  '.bubble-tool-calls-panel > .ca-response-row',
                )?.__entry?.content,
              };
              document.querySelector('.bubble-more-menu')?.remove();
              host.remove();
              window.fetch = previous.fetch;
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              app._activeToolGroupBubble = previous.activeToolGroupBubble;
              return snapshot;
            }""",
            {"base": self.base},
        )
        self.assertIn("_refresh=", result["request"]["url"])
        self.assertEqual(result["request"]["cache"], "no-store")
        self.assertEqual(result["previewText"], "Complete authoritative response")
        self.assertEqual(result["panelText"], "Complete authoritative response")
        self.assertEqual(result["sourceText"], "Complete authoritative response")

    def test_background_cache_refresh_replaces_truncated_existing_row(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const cacheModule = await import(base + '/ui/chat/js/chat-message-cache.js');
              const session = await import(base + '/ui/chat/js/session-load.js');
              const sid = 'truncated-cache-repair';
              const previous = {
                chatMessages: app.chatMessages,
                currentSessionId: app.currentSessionId,
              };
              app.chatMessages = null;
              app.currentSessionId = sid;
              cacheModule._messageCache.set(sid, {
                messages: [{
                  id: 'assistant-cache', role: 'assistant', content: 'Hi',
                  status: 'streaming', session_seq: 1,
                }],
                maxSeq: 1, light: true,
              });
              session._mergeCachedRefresh(sid, [{
                id: 'assistant-cache', role: 'assistant',
                content: 'Hi! Ready when you are — what are we working on today?',
                status: 'complete', session_seq: 1,
              }], false);
              const repaired = cacheModule._messageCache.get(sid).messages[0];
              cacheModule._messageCache.delete(sid);
              app.chatMessages = previous.chatMessages;
              app.currentSessionId = previous.currentSessionId;
              return repaired;
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            result["content"],
            "Hi! Ready when you are — what are we working on today?",
        )

    def test_active_stream_polls_durable_tail_even_while_websocket_is_recent(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const reconcile = await import(base + '/ui/chat/js/chat-reconcile.js');
              if (typeof app.stopReconcileLoop === 'function') app.stopReconcileLoop();
              const previous = {
                fetch: window.fetch,
                currentSessionId: app.currentSessionId,
                currentUserId: app.currentUserId,
                isProcessing: app.isProcessing,
                lastWsEventAt: app._lastWsEventAt,
                lastInteractionSeq: app.lastInteractionSeq,
              };
              let calls = 0;
              window.fetch = async url => {
                if (String(url).includes('/api/v1/db/session-tail')) {
                  calls += 1;
                  return new Response(JSON.stringify({
                    messages: [],
                    run: {active: true, latest_session_seq: 0},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                return previous.fetch(url);
              };
              app.currentSessionId = 'active-order-poll';
              app.currentUserId = '';
              app.isProcessing = true;
              app._lastWsEventAt = {'active-order-poll': Date.now()};
              app.lastInteractionSeq = {'active-order-poll': 0};
              reconcile.startReconcileLoop();
              await new Promise(resolve => setTimeout(resolve, 950));
              app.stopReconcileLoop();

              window.fetch = previous.fetch;
              app.currentSessionId = previous.currentSessionId;
              app.currentUserId = previous.currentUserId;
              app.isProcessing = previous.isProcessing;
              app._lastWsEventAt = previous.lastWsEventAt;
              app.lastInteractionSeq = previous.lastInteractionSeq;
              reconcile.startReconcileLoop();
              return calls;
            }""",
            {"base": self.base},
        )
        self.assertGreaterEqual(result, 1)

    def test_multi_user_isolation_and_logout_purge(self):
        result = self.page.evaluate(
            """async ({base, first, second}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              db.setOwnerScope(first);
              await db.createSession({id: 'shared', agent_id: 'a', title: 'secret'});
              const firstCount = await db.countSessions();
              db.setOwnerScope(second);
              const secondCount = await db.countSessions();
              db.setOwnerScope(first);
              await db.clearAll();
              const purgedCount = await db.countSessions();
              return {firstCount, secondCount, purgedCount};
            }""",
            {
                "base": self.base,
                "first": self._scope("user_a"),
                "second": self._scope("user_b"),
            },
        )
        self.assertEqual(result, {"firstCount": 1, "secondCount": 0, "purgedCount": 0})

    def test_device_purge_resolves_scope_after_reload_and_only_then_acks(self):
        page = self.browser.new_page()
        try:
            page.goto(f"{self.base}/ui/chat/index.html")
            result = page.evaluate(
                """async ({base, userId, revision}) => {
                  const payload = btoa(JSON.stringify({
                    user_id: userId, sub: userId, rev: revision,
                  })).replace(/=/g, '').replace(/\\+/g, '-').replace(/\\//g, '_');
                  const token = `header.${payload}.signature`;
                  localStorage.setItem('webagent_accounts', JSON.stringify([{
                    user_id: userId, username: userId, access_token: token,
                  }]));
                  localStorage.setItem('webagent_active_user_id', userId);
                  localStorage.setItem('auth_user_id', userId);
                  localStorage.setItem('auth_token', token);

                  const input = new TextEncoder().encode(
                    `webagent-browser-cache:${userId}:${revision}`,
                  );
                  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
                  const scope = Array.from(
                    bytes, byte => byte.toString(16).padStart(2, '0'),
                  ).join('').slice(0, 24);
                  const {default: db} = await import(
                    base + '/ui/chat/js/storage/indexeddb.js'
                  );
                  db.setOwnerScope(scope);
                  await db.createSession({id: 'secret', agent_id: 'a'});
                  db.close();
                  return {token, scope};
                }""",
                {"base": self.base, "userId": self._scope("purge"), "revision": 3},
            )
            page.reload()
            outcome = page.evaluate(
                """async ({base, token}) => {
                  let acknowledgements = 0;
                  window.fetch = async url => {
                    if (String(url).includes('/device/purge-ack')) acknowledgements += 1;
                    return new Response('{}', {status: 200});
                  };
                  const {purgeAndAcknowledge} = await import(
                    base + '/ui/shared/js/device-purge.js'
                  );
                  const purged = await purgeAndAcknowledge(
                    token, {forgetAccount: false},
                  );
                  const {default: db} = await import(
                    base + '/ui/chat/js/storage/indexeddb.js'
                  );
                  return {
                    purged,
                    acknowledgements,
                    remaining: await db.countSessions(),
                  };
                }""",
                {"base": self.base, "token": result["token"]},
            )
            self.assertEqual(
                outcome,
                {"purged": True, "acknowledgements": 1, "remaining": 0},
            )

            absent = page.evaluate(
                """async ({base}) => {
                  const userId = 'absent-tenant';
                  const payload = btoa(JSON.stringify({
                    user_id: userId, sub: userId, rev: 99,
                  })).replace(/=/g, '').replace(/\\+/g, '-').replace(/\\//g, '_');
                  const token = `header.${payload}.signature`;
                  localStorage.setItem('webagent_accounts', JSON.stringify([{
                    user_id: userId, username: userId, access_token: token,
                  }]));
                  localStorage.setItem('webagent_active_user_id', userId);
                  localStorage.setItem('auth_user_id', userId);
                  let acknowledgements = 0;
                  window.fetch = async () => {
                    acknowledgements += 1;
                    return new Response('{}', {status: 200});
                  };
                  const {purgeAndAcknowledge} = await import(
                    base + '/ui/shared/js/device-purge.js'
                  );
                  const purged = await purgeAndAcknowledge(
                    token, {forgetAccount: false},
                  );
                  return {purged, acknowledgements};
                }""",
                {"base": self.base},
            )
            self.assertEqual(absent, {"purged": True, "acknowledgements": 1})
        finally:
            page.close()

    def test_multi_tab_sequence_allocation_is_atomic(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const mod = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const one = new mod.SessionDB();
              const two = new mod.SessionDB();
              one.setOwnerScope(scope);
              two.setOwnerScope(scope);
              await one.createSession({id: 's', agent_id: 'a'});
              await Promise.all([
                one.addInteraction('s', {id: 'one', role: 'user', content: 'one'}),
                two.addInteraction('s', {id: 'two', role: 'assistant', content: 'two'}),
              ]);
              const rows = await one.getInteractions('s');
              return rows.map(row => row.session_seq);
            }""",
            {"base": self.base, "scope": self._scope("tabs")},
        )
        self.assertEqual(result, [0, 1])

    def test_canonical_order_inserts_unsequenced_rows_by_real_timestamp(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {sortTranscriptCanonical} = await import(
                base + '/ui/chat/js/transcript-order.js'
              );
              const rows = [
                {id: 'durable-late', session_seq: 20,
                 created_at: '2026-08-22T12:02:00Z'},
                {id: 'legacy-middle', session_seq: null,
                 created_at: '2026-08-22 12:01:00'},
                {id: 'durable-early', session_seq: 10,
                 created_at: '2026-08-22T12:00:00Z'},
              ];
              return sortTranscriptCanonical(rows).map(row => row.id);
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result, ["durable-early", "legacy-middle", "durable-late"]
        )

    def test_server_cache_replace_preserves_missing_sequence(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({id: 's', agent_id: 'a'});
              await db.replaceInteractions('s', [
                {id: 'saved', role: 'assistant', content: 'saved', session_seq: 8,
                 created_at: '2026-08-22T12:00:00Z'},
                {id: 'unstamped', role: 'tool', content: '', session_seq: null,
                 created_at: '2026-08-22T12:01:00Z'},
              ]);
              const rows = await db.getInteractions('s', Infinity);
              db.close();
              return rows.map(row => ({id: row.id, session_seq: row.session_seq}));
            }""",
            {"base": self.base, "scope": self._scope("null_seq")},
        )
        self.assertEqual(
            result,
            [
                {"id": "saved", "session_seq": 8},
                {"id": "unstamped", "session_seq": None},
            ],
        )

    def test_hybrid_merge_replaces_stale_streaming_row(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              window.fetch = async url => {
                if (String(url).includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, transcript_ttl_seconds: 900},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                throw new Error('unexpected fetch ' + url);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 's', agent_id: 'a', _authority: 'server', _dirty: false,
              });
              await db.replaceInteractions('s', [{
                id: 'a', role: 'assistant', content: 'Hi', status: 'streaming',
                session_seq: 2, created_at: '2026-08-22T12:00:00Z',
              }]);
              await storageAdapter.mergeInteractionsIntoCache('s', [{
                id: 'a', role: 'assistant', content: 'Hi — complete response',
                status: 'complete', session_seq: 2,
                created_at: '2026-08-22T12:00:00Z',
              }], null);
              const row = (await db.getInteractions('s', Infinity))[0];
              return {content: row.content, status: row.status, count: await db.countInteractions('s')};
            }""",
            {"base": self.base, "scope": self._scope("stale_stream")},
        )
        self.assertEqual(
            result,
            {"content": "Hi — complete response", "status": "complete", "count": 1},
        )

    def test_tombstone_survives_reopen_after_interrupted_sync(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const mod = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new mod.SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({
                id: 's', agent_id: 'a', server_revision: 4, local_revision: 7,
              });
              await db.deleteSession('s');
              db.close();
              const reopened = new mod.SessionDB();
              reopened.setOwnerScope(scope);
              return (await reopened.listSyncOutbox())[0];
            }""",
            {"base": self.base, "scope": self._scope("interrupt")},
        )
        self.assertEqual(result["operation"], "delete")
        self.assertEqual(result["base_server_revision"], 4)

    def test_transcript_age_does_not_evict_but_quota_preserves_authority(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              db.setOwnerScope(scope);
              await db.createSession({
                id: 'cached', agent_id: 'a', _authority: 'server',
                content_hash: 'hash', cache_schema_version: 1,
                cache_expires_at: '2000-01-01T00:00:00.000Z', _dirty: false,
              });
              await db.createSession({
                id: 'owned', agent_id: 'a', _authority: 'browser', _dirty: true,
              });
              const agePolicy = await db.enforceCachePolicy({maxBytes: 50000000});
              const retainedAfterAgeSweep = !!(await db.getSession('cached'));
              const policy = await db.enforceCachePolicy({maxBytes: 0});
              return {
                agePolicy, retainedAfterAgeSweep, policy,
                cached: await db.getSession('cached'),
                owned: await db.getSession('owned'),
              };
            }""",
            {"base": self.base, "scope": self._scope("quota")},
        )
        self.assertTrue(result["retainedAfterAgeSweep"])
        self.assertEqual(result["agePolicy"]["evicted"], 0)
        self.assertIsNone(result["cached"])
        self.assertIsNotNone(result["owned"])
        self.assertTrue(result["policy"]["quotaExceeded"])

    def test_stream_projection_upsert_replaces_partial_row(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({id: 's', agent_id: 'a', _authority: 'server'});
              await db.upsertCachedInteraction('s', {
                id: 'a1', role: 'assistant', content: 'partial',
                session_seq: 8, status: 'streaming',
              });
              await db.upsertCachedInteraction('s', {
                id: 'a1', role: 'assistant', content: 'complete response',
                session_seq: 8, status: 'complete',
              });
              const rows = await db.getInteractions('s', Infinity);
              return {rows, session: await db.getSession('s')};
            }""",
            {"base": self.base, "scope": self._scope("stream_projection")},
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["content"], "complete response")
        self.assertEqual(result["rows"][0]["session_seq"], 8)
        self.assertTrue(result["session"]["cache_projection_dirty"])

    def test_session_cache_orders_pins_then_real_activity(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              await db.createSession({
                id: 'pin-second', agent_id: 'a', pinned: true, sort_order: 1,
                activity_at: '2026-08-21T12:00:00Z',
              });
              await db.createSession({
                id: 'pin-first', agent_id: 'a', pinned: true, sort_order: 0,
                activity_at: '2026-08-20T12:00:00Z',
              });
              await db.createSession({
                id: 'old', agent_id: 'a', pinned: false,
                activity_at: '2026-08-18T12:00:00Z',
                updated_at: '2099-01-01T00:00:00Z',
              });
              await db.createSession({
                id: 'recent', agent_id: 'a', pinned: false,
                activity_at: '2026-08-19T12:00:00Z',
                updated_at: '2020-01-01T00:00:00Z',
              });
              return (await db.listSessions()).map(row => ({
                id: row.id, sort_order: row.sort_order,
              }));
            }""",
            {"base": self.base, "scope": self._scope("session_order")},
        )
        self.assertEqual(
            result,
            [
                {"id": "pin-first", "sort_order": 0},
                {"id": "pin-second", "sort_order": 1},
                {"id": "recent", "sort_order": None},
                {"id": "old", "sort_order": None},
            ],
        )

    def test_session_cache_recent_order_blends_bucketed_recency_and_activity(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {SessionDB} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const db = new SessionDB();
              db.setOwnerScope(scope);
              // Same six-hour recency bucket: a tiny timestamp difference must
              // not displace a session with sustained activity.
              await db.createSession({
                id: 'one-off-newer', agent_id: 'a', pinned: false,
                activity_at: '2026-08-21T10:59:00Z', activity_count: 1,
              });
              await db.createSession({
                id: 'steady', agent_id: 'a', pinned: false,
                activity_at: '2026-08-21T06:01:00Z', activity_count: 31,
              });
              // A genuinely newer six-hour bucket still outranks old activity.
              await db.createSession({
                id: 'new-bucket', agent_id: 'a', pinned: false,
                activity_at: '2026-08-21T12:01:00Z', activity_count: 1,
              });
              return (await db.listSessions()).map(row => row.id);
            }""",
            {"base": self.base, "scope": self._scope("stable_recent_order")},
        )
        self.assertEqual(result, ["new-bucket", "steady", "one-off-newer"])

    def test_hybrid_warm_cache_reads_share_one_background_list_refresh(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let refreshCount = 0;
              let releaseRefresh;
              let markRefreshStarted;
              const refreshStarted = new Promise(resolve => { markRefreshStarted = resolve; });
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions')) {
                  refreshCount += 1;
                  markRefreshStarted();
                  await new Promise(resolve => { releaseRefresh = resolve; });
                  return new Response(JSON.stringify({sessions: [{
                    id: 'warm', agent_id: 'agent', title: 'Warm session',
                    created_at: '2026-08-21T12:00:00Z',
                    updated_at: '2026-08-21T12:00:00Z',
                    activity_at: '2026-08-21T12:00:00Z',
                    revision: 2, content_hash: 'server-hash',
                  }]}), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                throw new Error('unexpected fetch ' + value);
              };

              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'warm', agent_id: 'agent', title: 'Cached session',
                created_at: '2026-08-21T12:00:00Z',
                updated_at: '2026-08-21T12:00:00Z',
                activity_at: '2026-08-21T12:00:00Z',
                authority_revision: 1, content_hash: 'cached-hash',
                cache_schema_version: 2, _authority: 'server',
              });

              let deltaCount = 0;
              let resolveDelta;
              const delta = new Promise(resolve => { resolveDelta = resolve; });
              const onDelta = () => {
                deltaCount += 1;
                resolveDelta();
              };
              window.addEventListener('sessions-delta', onDelta);
              try {
                const reads = Promise.all([
                  storageAdapter.listSessions('single-flight-user'),
                  storageAdapter.listSessions('single-flight-user'),
                ]);
                const cached = await reads;
                await refreshStarted;
                releaseRefresh();
                await delta;
                await new Promise(resolve => setTimeout(resolve, 0));
                return {
                  mode: storageAdapter.mode,
                  refreshCount,
                  deltaCount,
                  titles: cached.map(rows => rows[0] && rows[0].title),
                };
              } finally {
                window.removeEventListener('sessions-delta', onDelta);
              }
            }""",
            {"base": self.base, "scope": self._scope("hybrid_single_flight")},
        )
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["refreshCount"], 1)
        self.assertEqual(result["deltaCount"], 1)
        self.assertEqual(result["titles"], ["Cached session", "Cached session"])

    def test_hybrid_reorder_updates_indexeddb_before_server_ack(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let releaseServer;
              let notifyStarted;
              const started = new Promise(resolve => { notifyStarted = resolve; });
              let submitted = null;
              window.fetch = async (url, options = {}) => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions/reorder')) {
                  submitted = JSON.parse(options.body);
                  notifyStarted();
                  await new Promise(resolve => { releaseServer = resolve; });
                  return new Response(JSON.stringify({success: true, updated: 2}), {
                    status: 200, headers: {'Content-Type': 'application/json'},
                  });
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'one', agent_id: 'a', pinned: true, sort_order: 0,
                updated_at: '2026-01-01T00:00:00Z',
                _authority: 'server', cache_schema_version: 2, content_hash: 'one-hash',
              });
              await db.createSession({
                id: 'two', agent_id: 'a', pinned: true, sort_order: 1,
                updated_at: '2026-01-02T00:00:00Z',
                _authority: 'server', cache_schema_version: 2, content_hash: 'two-hash',
              });
              const pending = storageAdapter.reorderSessions('authenticated', ['two', 'one']);
              await started;
              const beforeAck = await db.listSessions();
              releaseServer();
              const response = await pending;
              const shaped = await storageAdapter.listSessions('authenticated');
              return {
                mode: storageAdapter.mode,
                beforeAck: beforeAck.map(row => ({
                  id: row.id, sort_order: row.sort_order, updated_at: row.updated_at,
                })),
                submitted,
                response,
                shaped: shaped.map(row => ({id: row.id, sort_order: row.sort_order})),
              };
            }""",
            {"base": self.base, "scope": self._scope("hybrid_reorder")},
        )
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual([row["id"] for row in result["beforeAck"]], ["two", "one"])
        self.assertEqual([row["sort_order"] for row in result["beforeAck"]], [0, 1])
        self.assertEqual(
            [row["updated_at"] for row in result["beforeAck"]],
            ["2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"],
        )
        self.assertEqual(result["submitted"]["order"], ["two", "one"])
        self.assertEqual(result["response"], {"success": True, "updated": 2})
        self.assertEqual(result["shaped"][0], {"id": "two", "sort_order": 0})

    def test_hybrid_reorder_rolls_back_when_server_and_reconcile_fail(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {browser_authority: false, browser_session_cache: true},
                    cache_scope: scope,
                    cache_policy: {schema_version: 2, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions/reorder')) {
                  return new Response('{}', {status: 503});
                }
                if (value.includes('/api/v1/db/sessions')) {
                  throw new Error('offline during reconcile');
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({id: 'one', agent_id: 'a', pinned: true, sort_order: 0});
              await db.createSession({id: 'two', agent_id: 'a', pinned: true, sort_order: 1});
              let error = '';
              try {
                await storageAdapter.reorderSessions('authenticated', ['two', 'one']);
              } catch (e) {
                error = e.message;
              }
              return {
                error,
                rows: (await db.listSessions()).map(row => ({
                  id: row.id, sort_order: row.sort_order,
                })),
              };
            }""",
            {"base": self.base, "scope": self._scope("hybrid_reorder_rollback")},
        )
        self.assertIn("HTTP 503", result["error"])
        self.assertEqual(
            result["rows"],
            [{"id": "one", "sort_order": 0}, {"id": "two", "sort_order": 1}],
        )

    def test_sessions_page_prime_paints_dropdown_without_network_wait(self):
        result = self.page.evaluate(
            """async ({base, userId}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const list = await import(
                base + '/ui/chat/elements/session-dropdown/list.js'
              );
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              storageAdapter.switchToNormal();
              const previous = {
                userId: app.currentUserId,
                sessionId: app.currentSessionId,
                fetch: window.fetch,
              };
              app.currentUserId = userId;
              app.currentSessionId = 'warm-session';
              let menu = document.getElementById('session-dropdown-menu');
              let createdMenu = false;
              if (!menu) {
                menu = document.createElement('div');
                menu.id = 'session-dropdown-menu';
                document.body.appendChild(menu);
                createdMenu = true;
              }
              list.primeSessionMetadataCache(userId, [{
                id: 'warm-session',
                title: 'Already on the Sessions page',
                pinned: true,
                sort_order: 0,
                activity_at: '2026-08-21T12:00:00Z',
              }]);
              let fetchCalls = 0;
              window.fetch = async () => {
                fetchCalls += 1;
                await new Promise(() => {});
              };
              const started = performance.now();
              const servedFromCache = await Promise.race([
                list.populateSessionSelect(userId, {preferCache: true}),
                new Promise(resolve => setTimeout(() => resolve('timeout'), 250)),
              ]);
              const elapsed = performance.now() - started;
              const text = menu.textContent || '';
              if (createdMenu) menu.remove();
              window.fetch = previous.fetch;
              app.currentUserId = previous.userId;
              app.currentSessionId = previous.sessionId;
              return {servedFromCache, elapsed, fetchCalls, text};
            }""",
            {"base": self.base, "userId": self._scope("page_prime")},
        )
        self.assertIs(result["servedFromCache"], True)
        self.assertLess(result["elapsed"], 250)
        self.assertEqual(result["fetchCalls"], 0)
        self.assertIn("Already on the Sessions page", result["text"])

    def test_corrupt_or_stale_cache_manifest_fails_closed_to_server(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let serverReads = 0;
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {
                      browser_authority: false, browser_session_cache: true,
                    },
                    cache_scope: scope,
                    cache_policy: {schema_version: 1, max_bytes: 50000000},
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/sessions')) {
                  serverReads += 1;
                  return new Response(JSON.stringify({sessions: []}), {
                    status: 200, headers: {'Content-Type': 'application/json'},
                  });
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'corrupt', agent_id: 'a', _authority: 'server',
                cache_schema_version: 999, content_hash: '',
                cache_expires_at: '2999-01-01T00:00:00.000Z',
              });
              const rows = await storageAdapter.listSessions('authenticated');
              return {mode: storageAdapter.mode, serverReads, rows};
            }""",
            {"base": self.base, "scope": self._scope("corrupt")},
        )
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["serverReads"], 1)
        self.assertEqual(result["rows"], [])

    def test_partial_sync_marks_only_acknowledged_revision_clean(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              const {syncEngine} = await import(base + '/ui/chat/js/storage/sync.js');
              db.setOwnerScope(scope);
              await db.createSession({id: 'ok', agent_id: 'a', _dirty: true});
              await db.createSession({id: 'conflict', agent_id: 'a', _dirty: true});
              await syncEngine.markDirty('ok');
              await syncEngine.markDirty('conflict');
              window.fetch = async (_url, options) => {
                const body = JSON.parse(options.body);
                return new Response(JSON.stringify({
                  results: body.mutations.map(row => row.session_id === 'ok'
                    ? {
                        session_id: row.session_id,
                        mutation_id: row.mutation_id,
                        status: 'applied',
                        server_revision: 1,
                        content_hash: 'server-hash',
                        client_revision: row.client_revision,
                      }
                    : {
                        session_id: row.session_id,
                        mutation_id: row.mutation_id,
                        status: 'conflict',
                        server_revision: 2,
                        content_hash: 'newer',
                        client_revision: row.client_revision,
                        error: 'stale base revision',
                      }),
                }), {status: 200, headers: {'Content-Type': 'application/json'}});
              };
              const syncResult = await syncEngine.flush('authenticated');
              return {
                syncResult,
                ok: await db.getSession('ok'),
                conflict: await db.getSession('conflict'),
              };
            }""",
            {"base": self.base, "scope": self._scope("partial")},
        )
        self.assertFalse(result["syncResult"]["ok"])
        self.assertFalse(result["ok"]["_dirty"])
        self.assertTrue(result["conflict"]["_dirty"])
        self.assertEqual(result["conflict"]["_sync_error"], "stale base revision")

    def test_hybrid_transcript_serves_only_revision_validated_cache_hit(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              let validations = 0;
              let fullReads = 0;
              window.fetch = async url => {
                const value = String(url);
                if (value.includes('/api/v1/browser/routing')) {
                  return new Response(JSON.stringify({
                    routing: {session_data: 'server', session_cache: 'browser'},
                    capabilities: {
                      browser_authority: false, browser_session_cache: true,
                    },
                    cache_scope: scope,
                    cache_policy: {
                      schema_version: 2, metadata_ttl_seconds: 300,
                      transcript_ttl_seconds: 900, max_bytes: 50000000,
                    },
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (value.includes('/api/v1/db/session-messages')) {
                  if (value.includes('manifest_only=true')) {
                    validations += 1;
                    return new Response(JSON.stringify({
                      messages: [], not_modified: true, cache_status: 'validated',
                      manifest: {
                        authority_revision: 2, content_hash: 'server-hash',
                        cache_schema_version: 2, interaction_count: 2,
                      },
                    }), {status: 200, headers: {'Content-Type': 'application/json'}});
                  }
                  fullReads += 1;
                  return new Response(JSON.stringify({
                    messages: [
                      {id: 'u', session_id: 'validated', role: 'user', content: 'hello', session_seq: 1},
                      {id: 'a', session_id: 'validated', role: 'assistant', content: 'hi', session_seq: 2},
                    ],
                    manifest: {
                      authority_revision: 2, content_hash: 'server-hash',
                      cache_schema_version: 2, interaction_count: 2,
                    },
                  }), {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                throw new Error('unexpected fetch ' + value);
              };
              const {storageAdapter} = await import(
                base + '/ui/chat/js/storage/storage-adapter.js'
              );
              const {default: db} = await import(
                base + '/ui/chat/js/storage/indexeddb.js'
              );
              await storageAdapter.autoSelectMode('agent');
              await db.createSession({
                id: 'validated', agent_id: 'a', _authority: 'server',
                authority_revision: 2, content_hash: 'server-hash',
                cache_schema_version: 2, interaction_count: 2,
                cache_expires_at: '2999-01-01T00:00:00.000Z', _dirty: false,
              });
              await storageAdapter.getInteractions('validated', 20);
              const data = await storageAdapter.getInteractions('validated', 20);
              const validation = await storageAdapter.revalidateTranscript('validated');
              await db.updateInteraction('u', {content: 'tampered'});
              const recovered = await storageAdapter.getInteractions('validated', 20);
              return {
                validations, fullReads, cacheStatus: data.cache_status,
                validationNotModified: validation.not_modified,
                messages: data.messages.map(row => row.content),
                recovered: recovered.messages.map(row => row.content),
              };
            }""",
            {"base": self.base, "scope": self._scope("validated")},
        )
        self.assertEqual(result["validations"], 1)
        self.assertEqual(result["fullReads"], 2)
        self.assertEqual(result["cacheStatus"], "cached-hit")
        self.assertTrue(result["validationNotModified"])
        self.assertEqual(result["messages"], ["hello", "hi"])
        self.assertEqual(result["recovered"], ["hello", "hi"])

    def test_lifecycle_does_not_expire_cached_views_by_age(self):
        result = self.page.evaluate(
            """async ({base, scope}) => {
              const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
              db.setOwnerScope(scope);
              db.configureLifecyclePolicy({
                metadata_ttl_seconds: 1,
                run_state_ttl_seconds: 1,
                generated_html_ttl_seconds: 1,
              });
              const raw = await db.ready();
              await new Promise((resolve, reject) => {
                const tx = raw.transaction(
                  ['agent_config', 'session_runs', 'genui_html', 'app_cache'],
                  'readwrite',
                );
                tx.objectStore('agent_config').put({
                  id: 'old-agent', config_hash: 'old', cached_at: '2000-01-01T00:00:00Z',
                  expires_at: '2000-01-02T00:00:00Z', label: 'saved agent',
                });
                tx.objectStore('session_runs').put({
                  id: 'old-run', session_id: 'old-session', status: 'complete',
                  started_at: '2000-01-01T00:00:00Z', finished_at: '2000-01-01T00:01:00Z',
                  expires_at: '2000-01-02T00:00:00Z',
                });
                tx.objectStore('genui_html').put({
                  slug: 'old-page', html: '<main>saved page</main>',
                  saved_at: '2000-01-01T00:00:00Z', expires_at: '2000-01-02T00:00:00Z',
                });
                tx.objectStore('app_cache').put({
                  key: 'old-view', value: {label: 'saved view'}, expires_at: 2,
                  updated_at: 1, size: 10,
                });
                tx.oncomplete = resolve;
                tx.onerror = () => reject(tx.error);
                tx.onabort = () => reject(tx.error);
              });
              const cleanup = await db.enforceLifecyclePolicy({now: Date.now()});
              const appCache = await new Promise((resolve, reject) => {
                const tx = raw.transaction('app_cache', 'readonly');
                const req = tx.objectStore('app_cache').get('old-view');
                req.onsuccess = () => resolve(req.result || null);
                req.onerror = () => reject(req.error);
              });
              return {
                cleanup,
                agent: await db.getCachedAgentConfig('old-agent'),
                runs: await db.listRuns('old-session'),
                html: await db.getGenuiHtml('old-page'),
                appCache,
              };
            }""",
            {"base": self.base, "scope": self._scope("no-expiry")},
        )
        self.assertEqual(result["cleanup"]["rows_removed"], 0)
        self.assertEqual(result["agent"]["label"], "saved agent")
        self.assertEqual(result["runs"][0]["id"], "old-run")
        self.assertEqual(result["html"], "<main>saved page</main>")
        self.assertEqual(result["appCache"]["value"]["label"], "saved view")


if __name__ == "__main__":
    unittest.main()
