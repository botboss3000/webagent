# Working with images

Your own model may not be able to *see* images, and an image-only model can't run
tools. You have two ways to handle images — pick by how image-heavy the task is.

## 1. Quick look — delegate with `process_image` (default)

When you need a specific fact about an attached image, call **`process_image`**
with a precise question. It hands the image to a vision model and returns the
answer; you stay on your own model the whole time.

- Be specific: *"What colours are in the top-right logo?"* beats *"describe this."*
- Need more detail? Just call it again with a sharper question — each call is a
  fresh, independent look (cheaper and clearer than one long back-and-forth).
- It uses the most recent image in the chat unless you pass an `attachment_id`.

Use this for one-off questions: reading text in a screenshot, checking a colour,
confirming what's in a photo, spot-checking a design detail.

## 2. Take over — `switch_model` (image-heavy conversations)

When the whole conversation is about images (a web-design review, iterating on a
mock-up, repeatedly reasoning over a screenshot), it's better to run *yourself* on
a text+image model than to delegate every turn. Call **`switch_model`** with a
model that supports both vision and tools.

- This changes the model for **this conversation only**, and **persists** until
  you (or the user) switch back. It takes effect on the next turn.
- It needs permission (or auto-approval in the Tools panel).
- **Switch back** to the cheaper text model when the image stretch is over:
  `switch_model("default")`. Don't leave an expensive vision model running for
  plain text chat.

## Rules of thumb

- One quick question about an image → **`process_image`**.
- Sustained, image-centric work → **`switch_model`** to a text+image model, then
  revert with `switch_model("default")`.
- `switch_model` only accepts models that can run tools. If it rejects an
  image-only model (e.g. a `*-image` generation model), that's expected — use
  `process_image` to delegate to that model instead.
- If `process_image` says no vision model is configured, tell the user to tick
  **In** on a vision-capable model in App Config → Models.
