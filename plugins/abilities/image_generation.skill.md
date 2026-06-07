# Making images

You can create images even when your own model can't draw — the work is delegated
for you. This skill is about composing good prompts and knowing your options.

## Default — `generate_image` (delegates for you)

Call **`generate_image`** with a text prompt. It always runs on the model the admin
configured for image **output** (ticked **Out** in App Config → Models), regardless
of whether *your* model can produce images. The result is saved and returned to you.

- **Compose the prompt from the conversation.** Pull in the concrete details the
  user has given (subject, style, colours, mood, composition, text to include) so
  the image matches what they actually want.
- **Iterate.** If the first image is off, call again with a **revised, more
  specific** prompt — adjust one thing at a time (e.g. "same, but warmer lighting
  and a portrait aspect ratio"). Each call is independent.
- If it reports **no image-output model is configured**, tell the user an admin
  needs to tick **Out** on an image-capable model in App Config → Models — don't
  pretend you produced an image.

## Taking over — `switch_model` (only to a text+image-out model)

If a stretch of the conversation is image-generation heavy and a model exists that
can be the **brain** (text **and** tools) **and** output images, you may
`switch_model('<model>')` to run on it directly. If only image-only output models
are configured, switching isn't possible — that's expected; **keep using
`generate_image`** to delegate. Revert with `switch_model('default')` when done.

## Rules of thumb

- Need an image → **`generate_image`** (works on any model; delegates).
- Result not right → call again with a sharper, revised prompt.
- No image-out model configured → tell the user; **don't fake an image**.
- Heavy generation stretch *and* a text+image-out model exists → optional
  `switch_model`, then revert with `switch_model('default')`.
