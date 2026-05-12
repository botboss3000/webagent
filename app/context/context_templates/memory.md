---
context_type: memory
title: Memory Usage Guide
tags: [memory, brain, knowledge]
---

## How memory works

The knowledge brain stores information as **pages** with:
- A unique **slug** (like `projects/my-project`)
- A **page type** (meeting, project, person, concept, note)
- **Compiled truth** — what you know to be true
- **Timeline** — dated entries of new information

## When to use memory

- **Before answering**: if the user asks about something we've discussed before, search memory first
- **After learning**: save important facts, decisions, and preferences
- **Project context**: store architecture decisions, API keys locations, configuration details

## Memory commands

- `memory search "query"` — find relevant pages
- `memory get "slug"` — read a specific page
- `memory save "slug" --type project --truth "..." --timeline "..."` — create or update a page
- `memory delete "slug"` — remove a page
- `memory list --type project` — browse pages by type
