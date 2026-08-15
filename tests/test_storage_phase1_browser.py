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
                model: host.querySelector('.turn-gutter-model')?.textContent,
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
                "buttons": 5,
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
        self.assertEqual(result["saved"]["buttons"], 6)
        self.assertEqual(result["saved"]["deletes"], 1)
        self.assertEqual(result["saved"]["streaming"], 0)
        self.assertTrue(result["liveHiddenAfterClick"], result)
        self.assertTrue(result["liveShownAfterSecondClick"])
        self.assertTrue(result["savedHiddenAfterClick"])
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

    def test_live_activity_reuses_turn_group_after_active_pointer_resets(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const {app} = await import(base + '/ui/shared/js/state.js');
              const stream = await import(base + '/ui/chat/js/chat-stream.js');
              const host = document.createElement('div');
              document.body.appendChild(host);
              const previous = {
                chatMessages: app.chatMessages,
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
              const rows = groups[0]?.querySelectorAll('.ca-tool-row').length || 0;
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
              const rows = groups[0]?.querySelectorAll('.ca-tool-row').length || 0;
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

    def test_reprojection_keeps_recovery_activity_segments_separate(self):
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
        self.assertEqual([item["seq"] for item in result], [1, 10, 15, 20, 30])
        self.assertEqual([item["activity"] for item in result].count(True), 2)
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

    def test_expired_and_quota_cache_eviction_preserves_authority(self):
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
              const policy = await db.enforceCachePolicy({maxBytes: 0});
              return {
                policy,
                cached: await db.getSession('cached'),
                owned: await db.getSession('owned'),
              };
            }""",
            {"base": self.base, "scope": self._scope("quota")},
        )
        self.assertIsNone(result["cached"])
        self.assertIsNotNone(result["owned"])
        self.assertTrue(result["policy"]["quotaExceeded"])

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
              await db.updateInteraction('u', {content: 'tampered'});
              const recovered = await storageAdapter.getInteractions('validated', 20);
              return {
                validations, fullReads, cacheStatus: data.cache_status,
                messages: data.messages.map(row => row.content),
                recovered: recovered.messages.map(row => row.content),
              };
            }""",
            {"base": self.base, "scope": self._scope("validated")},
        )
        self.assertEqual(result["validations"], 1)
        self.assertEqual(result["fullReads"], 2)
        self.assertEqual(result["cacheStatus"], "validated-hit")
        self.assertEqual(result["messages"], ["hello", "hi"])
        self.assertEqual(result["recovered"], ["hello", "hi"])


if __name__ == "__main__":
    unittest.main()
