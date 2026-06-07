# Working with images

How you handle an attached image depends on what your **current model** can do.
The app routes images for you automatically — this skill is about what to do once
they're routed, and how to get more when you need it.

## What happens automatically (you usually don't lift a finger)

When a user attaches an image, the app checks whether your model can see images:

- **Your model can see images** → the image is sent to you directly. Just answer.
- **Your model is text-only** → a vision model writes a detailed, context-tailored
  description and it's folded into the user's message as
  `[Attached image — '<name>']: <description>`. **Answer from that description.** A
  short NOTE is added telling you it happened and what your options are.
- **No image ability / no vision model is configured** → you'll get a NOTE saying
  you **cannot read the image**. In that case, **tell the user plainly that you
  can't see images** and ask them to describe it. **Never guess or invent what's in
  an image you can't see.**

## When the auto-description isn't enough

### Quick, specific look — `process_image`
Call **`process_image`** with a precise question to get a fresh answer from a vision
model. You stay on your own model.

- Be specific: *"What colours are in the top-right logo?"* beats *"describe this."*
- **Iterate.** Each call is an independent look — start broad, then narrow:
  first *"describe this image"*, then *"what does the weather look like in this
  image?"* if the conversation turns to weather. Keep tightening the question until
  you have what you need.
- Uses the most recent image unless you pass an `attachment_id`.

Use this for one-off facts: reading text in a screenshot, checking a colour,
confirming an object, spot-checking a detail.

### Sustained image work — `switch_model` (take over on a vision model)
When the whole conversation is about images (a design review, iterating on a
mock-up, reasoning over a screenshot repeatedly), run *yourself* on a model that can
see images instead of delegating every turn. Call **`switch_model('<model>')`**.

- It only accepts a model that can **be the brain** (text **and** tools) **and**
  see images. If you pass a text-only model, an image-only model, or there's no
  valid target, it tells you — and says to keep using `process_image` instead.
- Changes the model for **this conversation only**, and **persists** until changed.
  Takes effect next turn.
- **Switch back** when the image stretch is over: `switch_model('default')`. Don't
  leave an expensive vision model running for plain text chat.

## Rules of thumb

- An image was described for you → answer from the description; only reach for
  `process_image` if a specific detail is missing.
- One quick question → **`process_image`** (iterate with sharper questions).
- Sustained, image-centric work → **`switch_model`** to a text+image model, then
  revert with **`switch_model('default')`**.
- Told you have **no** image ability → say so to the user; **do not hallucinate**
  the contents.
