# AutoAgent Page Workspace — Skill Guide

Use when the user sends a prompt via the AutoAgent tab. Prompts arrive tagged with the current page and its agent context:

```
[User → UI Agent → Page: "dashboard" | Context: "You are the dashboard agent..."]: <message>
```

Read the page name and context from the tag. Your job is to build or update that specific page.

## Page Agent Roles

Each page has its own agent with a specific purpose:

| Page | Default Role |
|------|-------------|
| **home** | webAgent onboarding & info page |
| **dashboard** | Live data display, charts, stats widgets |
| **notes** | Note-taking, lists, markdown-style content |
| **any custom** | Defined by the `agent_context` in the prompt tag |

Honour the agent context from the prompt tag — it defines who you are for that page.

## Pipeline

**READ TAG → CONCEPT → CODE → RENDER**

1. **Read** the `Page:` and `Context:` fields from the tag
2. **Concept** — articulate what the page should show/do
3. **Code** — write a single self-contained HTML file
4. **Render** — call `render_visual` with `page_name` matching the tag

## render_visual Usage

Always pass `page_name` so the output goes to the right page:

```python
render_visual(
    html="<!DOCTYPE html>...",
    title="My Dashboard",
    page_name="dashboard"   # ← must match the Page: from the prompt tag
)
```

`page_name` defaults to `"home"` if omitted — only omit it when the user is on the Home page.

## Page Management Tools

| Tool | When to use |
|------|------------|
| `list_pages` | User asks "what pages do I have?" |
| `create_page(slug, title, agent_context, initial_html)` | User asks to make a new page |
| `delete_page(slug)` | User asks to delete a page (home is protected) |
| `render_visual(html, title, page_name)` | Any time you want to render/update a page |

## HTML Guidelines

### For informational/app pages (home, notes, docs)

Standard HTML/CSS/JS. No external dependencies needed.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Title</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: #0a0a0f; color: #c0caf5;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  </style>
</head>
<body>
  <!-- content -->
</body>
</html>
```

### For creative / generative / p5.js pages

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sketch</title>
  <script>p5.disableFriendlyErrors = true;</script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js"></script>
  <style>
    html, body { margin: 0; padding: 0; overflow: hidden; background: #000; }
    canvas { display: block; }
  </style>
</head>
<body>
<script>
const CONFIG = { seed: 42 };

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(CONFIG.seed);
  noiseSeed(CONFIG.seed);
  colorMode(HSB, 360, 100, 100, 100);
}

function draw() {
  // render frame
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
</script>
</body>
</html>
```

## webAgent Aesthetic

When designing pages, match the app's look:

| Token | Value |
|-------|-------|
| Background | `#0a0a0f` |
| Surface | `#0d0d1e` |
| Border | `#1e1e3a` |
| Text primary | `#c0caf5` |
| Text muted | `#565f89` |
| Accent blue | `#7aa2f7` |
| Accent cyan | `#7dcfff` |
| Accent purple | `#bb9af7` |
| Error / red | `#f7768e` |

## Design Rules (for all page types)

- **Custom color palette always** — design 3–7 intentional colors
- **Canvas fills window** — use `createCanvas(windowWidth, windowHeight)` for p5.js, `height: 100vh` for HTML pages
- **Never plain backgrounds** — texture, gradient, or layered treatment
- **Be proactively creative** — if asked for "a chart", deliver a chart with animation, tooltips, and a polished layout. Include at least one detail the user didn't ask for but will appreciate.

## Important

- **Always call `render_visual`** — that's how the output appears in the iframe
- **Match `page_name` to the tagged page** — don't render to "home" when the user is on "dashboard"
- **Iterate on feedback** — when the user says "change X", fetch the current state from the prompt context and update it
- **First-render quality** — the page must look great on first load, no blank states or errors
