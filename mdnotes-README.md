# 📝 MDNotes — Markdown Note-Taking App

> A lightweight, self-hosted Markdown note-taking application powered by a Flask REST API and a clean HTML/CSS/JS frontend. Write in Markdown, serve as HTML, never lose a thought.

---

## ✨ Features

- **Full Markdown editing** — write notes with headings, lists, code blocks, tables, links, images, and more
- **Live preview** — see rendered HTML side-by-side as you type
- **RESTful API** — create, read, update, delete, search, and list notes over HTTP
- **No database required** — notes stored as plain `.md` files on disk; portable and backup-friendly
- **Search** — full-text search across all notes with file-name and content matching
- **Tag support** — tag your notes with frontmatter-style `tags:` metadata
- **Slug-based URLs** — every note gets a human-readable URL slug
- **Light/dark themes** — toggleable in the UI
- **Zero external dependencies for runtime** — just Python, Flask, and your browser

---

## ⚙️ Setup

### Prerequisites

- Python **3.9+**
- `pip` (Python package manager)

### 1. Clone or download the project

```bash
git clone https://github.com/yourusername/mdnotes.git
cd mdnotes
```

### 2. Install dependencies

```bash
pip install flask markdown pygments
```

| Package     | Purpose                          |
|-------------|----------------------------------|
| `flask`     | Web framework (backend API + static serving) |
| `markdown`  | Convert Markdown text to HTML    |
| `pygments`  | Syntax highlighting in code blocks |

### 3. Run the app

```bash
python app.py
```

You should see:

```
 * Serving Flask app 'app'
 * Running on http://127.0.0.1:5000 (Press CTRL+C to quit)
```

### 4. Open the app

Visit **[http://127.0.0.1:5000](http://127.0.0.1:5000)** in your browser.

---

## 🚀 API Documentation

All API endpoints return JSON. The base URL is `http://127.0.0.1:5000/api`.

| Method | Endpoint              | Description                          | Request Body                                      | Response                                          |
|--------|-----------------------|--------------------------------------|---------------------------------------------------|---------------------------------------------------|
| `GET`  | `/api/notes`          | List all notes                       | —                                                 | `[{ "slug", "title", "created", "updated", "tags" }]` |
| `GET`  | `/api/notes/<slug>`   | Get a single note by slug            | —                                                 | `{ "slug", "title", "content", "html", "created", "updated", "tags" }` |
| `POST` | `/api/notes`          | Create a new note                    | `{ "title": "...", "content": "...", "tags": [] }` | `{ "slug", "title", "created", "message" }`       |
| `PUT`  | `/api/notes/<slug>`   | Update an existing note              | `{ "title": "...", "content": "...", "tags": [] }` | `{ "slug", "title", "updated", "message" }`       |
| `DELETE` | `/api/notes/<slug>` | Delete a note                        | —                                                 | `{ "message": "Note deleted" }`                   |
| `GET`  | `/api/notes/search?q=`| Full-text search across all notes    | —                                                 | `[{ "slug", "title", "snippet", "tags" }]`        |

### Example — Create a note

```bash
curl -X POST http://127.0.0.1:5000/api/notes \
  -H "Content-Type: application/json" \
  -d '{
    "title": "My First Note",
    "content": "# Hello World\n\nThis is **Markdown** content.",
    "tags": ["hello", "demo"]
  }'
```

**Response:**

```json
{
  "slug": "my-first-note",
  "title": "My First Note",
  "created": "2026-06-05T20:30:00Z",
  "message": "Note created"
}
```

### Example — Search notes

```bash
curl "http://127.0.0.1:5000/api/notes/search?q=markdown"
```

---

## 🖥️ Frontend Usage Guide

The frontend is a single-page application served at `http://127.0.0.1:5000/`.

### Interface overview

```
┌──────────────────────────────────────────────────┐
│  📝 MDNotes                          ☀️   🔍    │
├──────────┬─────────────────────────────────────┤
│          │                                     │
│  📂 My   │  # My First Note                    │
│  Notes   │                                     │
│          │  This is **Markdown** content.       │
│  ─────── │                                     │
│          │                                     │
│  📄 my-  │  [ Preview ] [ Save ] [ Delete ]    │
│  first-  │                                     │
│  note    ├─────────────────────────────────────┤
│          │                                     │
│  📄 todo │  # My First Note                    │
│          │                                     │
│          │  This is Markdown content.           │
│          │                                     │
└──────────┴─────────────────────────────────────┘
   Sidebar          Editor / Preview panes
```

### Workflows

| Action                          | How to do it                                               |
|---------------------------------|------------------------------------------------------------|
| **Create a new note**           | Click the "＋ New Note" button in the sidebar              |
| **Edit a note**                 | Click a note in the sidebar → edit the Markdown in the editor pane |
| **Preview rendered Markdown**   | Click the "Preview" tab or toggle split-view               |
| **Save changes**                | Click "Save" (or `Ctrl+S`)                                 |
| **Delete a note**               | Click "Delete" → confirm                                   |
| **Search notes**                | Type in the search bar at the top — results filter instantly |
| **Toggle theme**                | Click the sun/moon icon in the header bar                  |

### Supported Markdown

| Element           | Syntax                        |
|-------------------|-------------------------------|
| Heading           | `# H1` → `###### H6`         |
| Bold              | `**bold**`                    |
| Italic            | `*italic*`                    |
| Code (inline)     | `` `code` ``                  |
| Code block        | ```` ```language ````         |
| Unordered list    | `- item`                      |
| Ordered list      | `1. item`                     |
| Link              | `[text](url)`                 |
| Image             | `![alt](url)`                 |
| Table             | `\| col1 \| col2 \|`          |
| Blockquote        | `> quote`                     |
| Horizontal rule   | `---`                         |

### Tags

Add tags at the top of any note using YAML-style frontmatter:

```markdown
---
tags: [python, tutorial, draft]
---

# My Note Content

Starts here...
```

Tags appear in the sidebar and are searchable via the search bar.

---

## 📸 Screenshots

> *Screenshots coming soon! Here's what you can expect:*

### Editor view

![Editor screenshot placeholder](https://via.placeholder.com/800x500?text=Editor+View)

*Split-pane view: raw Markdown on the left, rendered preview on the right.*

### Notes list

![List screenshot placeholder](https://via.placeholder.com/800x400?text=Notes+List)

*Sidebar showing all notes with titles, tags, and last-updated timestamps.*

### Dark mode

![Dark mode screenshot placeholder](https://via.placeholder.com/800x500?text=Dark+Mode)

*Same editor in dark theme.*

---

## 📁 Project Structure

```
mdnotes/
├── app.py                 # Flask application (routes, API, static serving)
├── requirements.txt       # Python dependencies
├── README.md              # This file
├── static/
│   ├── style.css          # App styling (light + dark themes)
│   └── app.js             # Frontend logic (SPA, API calls, editor)
├── templates/
│   └── index.html         # Main HTML page
└── notes/                 # Notes directory — created automatically
    ├── my-first-note.md
    └── todo.md
```

---

## 🔧 Configuration

All configuration is at the top of `app.py`:

```python
NOTES_DIR = "notes"           # Directory where .md files are stored
HOST = "127.0.0.1"           # Bind address
PORT = 5000                  # Port number
DEBUG = True                 # Enable Flask debug mode
```

Change these to suit your environment (e.g., `HOST = "0.0.0.0"` to expose on your network).

---

## 🐳 Docker (optional)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install flask markdown pygments
CMD ["python", "app.py"]
```

Build & run:

```bash
docker build -t mdnotes .
docker run -p 5000:5000 mdnotes
```

---

## 🧪 Running tests

```bash
pip install pytest
pytest tests/
```

---

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Commit changes: `git commit -am 'Add cool feature'`
4. Push: `git push origin feat/my-feature`
5. Open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">Made with ❤️, Markdown, and Flask.</p>