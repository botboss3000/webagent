---
context_type: jobs
title: Jobs and Scheduled Tasks
tags: [jobs, tasks, background]
---

## Background jobs

The server supports fire-and-forget background tasks for:
- Saving chat summaries to memory
- Running the optimizer (session improvement suggestions)
- Webhook processing

## Optimizer

The optimizer reviews completed sessions and suggests improvements to context documents, tools, or agent behavior. It runs:
- After each completed agent turn
- Triggered by `_fire_optimizer()` in the agent loop
- Configurable via `optimizer.json`

## Webhooks

Generic inbound webhooks can receive external payloads and route them to the agent loop. Configured via:
- `POST /api/v1/webhooks/generic/{webhook_id}` — receive webhook calls
- The agent can register webhooks with the `register_webhook` tool
