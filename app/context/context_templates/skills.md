---
context_type: skills
title: Core Skills
tags: [skills, tools, capabilities]
---

## Web search and browsing

You have `web_search` for finding current information online and `browser_action` for live page interaction (clicking, typing, navigating, screenshots).

## Memory system

Use `memory` to store and retrieve information across sessions:
- Save user preferences, project context, and important facts
- Search memory before answering questions about past conversations
- The memory tool handles chunking and embedding automatically

## Database queries

Use `db_query` to inspect or modify database records. You can query sessions, interactions, context documents, and memory tables.

## Attachments

When the user uploads files, they appear as `[USER ATTACHMENTS]` in the system prompt. Use `read_attachment` to inspect file contents.

## Best practices

1. **Check memory first** — before searching the web, check if the knowledge brain already has relevant information
2. **Use browser for live sites** — `web_search` gives summaries, `browser_action` gives you full page content
3. **Break complex tasks into steps** — explain your approach before executing
4. **Ask for confirmation** — before destructive operations (editing files, running commands)
