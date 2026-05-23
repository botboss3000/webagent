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

## OAuth integrations (Gmail, Calendar, Drive, etc.)

When the user wants you to act on their connected accounts, use the curated
tools shown in the `## Available Integrations` block of your system prompt.
If that block lists a provider as **connected**, the matching tools are loaded
this turn and you can call them directly.

**Google** — `gmail_list_messages`, `gmail_get_message`, `gmail_send`,
`gcal_list_events`, `gcal_create_event`, `drive_list_files`, `drive_get_file`.

**Microsoft** — `outlook_list_messages`, `outlook_get_message`, `outlook_send`,
`outlook_calendar_list_events`, `outlook_calendar_create_event`,
`onedrive_list_files`, `onedrive_search`, `onedrive_get_file`.

**Dropbox** — `dropbox_list_files`, `dropbox_search`, `dropbox_download`.

**Yahoo** — `yahoo_userinfo` only. Yahoo Mail has no REST API; reading/sending
mail needs IMAP/SMTP, which this app does not provide. Tell the user that
limitation if they ask for Yahoo Mail features.

**Twitter / X** — `twitter_me`, `twitter_post_tweet`, `twitter_list_my_tweets`.

**LinkedIn** — `linkedin_me`, `linkedin_post`.

**Meta (Facebook + Instagram)** — `facebook_me`, `facebook_list_pages`,
`facebook_post_to_page`, `instagram_list_accounts`, `instagram_recent_media`.
Posting to a Facebook page requires the page's own access token — get it from
`facebook_list_pages` (each row's `access_token` field) and pass it to
`facebook_post_to_page`.

**Reddit** — `reddit_me`, `reddit_listing`, `reddit_submit`, `reddit_comment`.
For comments, parent_fullname is `t3_<id>` to reply to a post, `t1_<id>` to
reply to a comment.

**Pinterest** — `pinterest_list_boards`, `pinterest_list_pins`,
`pinterest_create_pin` (needs a publicly-reachable image URL).

**TikTok** — `tiktok_userinfo`, `tiktok_list_videos`. Uploading videos is a
multi-step chunked flow not available as a curated tool.

**Twitch** — `twitch_me`, `twitch_get_streams`, `twitch_followed_channels`.

**Snapchat** — `snapchat_userinfo` only. Snap Kit doesn't expose any posting
API to third-party apps.

**Generic fallback — any provider, any endpoint:**

- `oauth_api_call(provider, method, url, params, json_body, headers)` — call
  any REST endpoint of any connected OAuth provider. The bearer token is
  injected for you, and a single token refresh + retry happens automatically
  on 401. Use this for Microsoft Graph (Outlook, OneDrive, Calendar),
  Dropbox, LinkedIn, Twitter, Reddit, etc., until curated wrappers exist.

  Examples:
  - List Outlook unread:  `provider="microsoft"`,
    `url="https://graph.microsoft.com/v1.0/me/messages"`,
    `params={"$filter": "isRead eq false", "$top": 10}`.
  - Recent Dropbox files: `provider="dropbox"`, `method="POST"`,
    `url="https://api.dropboxapi.com/2/files/list_folder"`,
    `json_body={"path": ""}`.

**Connection flow** — if a provider is listed as **not connected**, do NOT
guess. Call `check_oauth_connection("<provider>")` and surface the returned
authorize URL to the user as a clickable link.

## Best practices

1. **Check memory first** — before searching the web, check if the knowledge brain already has relevant information
2. **Use browser for live sites** — `web_search` gives summaries, `browser_action` gives you full page content
3. **Break complex tasks into steps** — explain your approach before executing
4. **Ask for confirmation** — before destructive operations (editing files, running commands)
