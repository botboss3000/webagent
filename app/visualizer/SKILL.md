# p5.js Creative Coding — Visualizer Skill

Use when user requests: p5.js sketches, creative coding, generative art, interactive visualizations, canvas animations, data viz, shader effects, 3D scenes, audio-reactive visuals, or kinetic typography.

## Creative Standard

This is visual art rendered in the browser. The canvas is the medium; the algorithm is the brush.

**Before writing a single line of code**, articulate the creative concept. What does this piece communicate? What makes the viewer stop scrolling? What separates this from a code tutorial example?

**First-render excellence is non-negotiable.** The output must be visually striking on first load.

**Dense, layered, considered.** Every frame should reward viewing. Never flat white backgrounds. Always compositional hierarchy. Always intentional color. Always micro-detail that only appears on close inspection.

**Be proactively creative.** If user asks for "a particle system," deliver a particle system with emergent flocking behavior, trailing ghost echoes, palette-shifted depth fog, and a background noise field that breathes. Include at least one visual detail the user didn't ask for but will appreciate.

**Cohesive aesthetic over feature count.** All elements must serve a unified visual language — shared color temperature, consistent stroke weight vocabulary, harmonious motion speeds. A sketch with ten unrelated effects is worse than one with three that belong together.

## Modes

| Mode | Input | Output |
|------|-------|--------|
| **Generative art** | Seed / parameters / description | Procedural visual composition (still or animated) |
| **Data visualization** | Dataset / description | Interactive charts, graphs, custom data displays |
| **Interactive experience** | Description | Mouse/keyboard/touch-driven sketch |
| **Animation / motion graphics** | Concept / storyboard | Timed sequences, kinetic typography, transitions |
| **3D scene** | Concept description | WebGL geometry, lighting, camera, materials |
| **Image processing** | Image / description | Pixel manipulation, filters, pointillism |
| **Audio-reactive** | Description | Sound-driven generative visuals |

## Stack

Single self-contained HTML file per project. No build step required.

| Layer | Tool | Purpose |
|-------|------|---------|
| Core | p5.js 1.11.3 (CDN) | Canvas rendering, math, transforms, event handling |
| 3D | p5.js WebGL mode | 3D geometry, camera, lighting, GLSL shaders |
| Audio | p5.sound.js (CDN) | FFT analysis, amplitude, mic input, oscillators |
| Export | `saveCanvas()` / `saveGif()` | PNG, GIF output |
| Fonts | Google Fonts / `loadFont()` | Custom typography |

## Pipeline

Every project follows the same path:

**CONCEPT → CODE → RENDER**

1. **CONCEPT** — Articulate the creative vision: mood, color world, motion vocabulary, what makes this unique
2. **CODE** — Write single HTML file with inline p5.js. Structure: globals → `preload()` → `setup()` → `draw()` → helpers → classes → event handlers
3. **RENDER** — Call `render_visual` tool with the HTML string. User sees result in AutoAgent tab.

## Creative Direction

### Aesthetic Dimensions

| Dimension | Options |
|-----------|---------|
| **Color system** | HSB/HSL, RGB, named palettes, procedural harmony, gradient interpolation |
| **Noise vocabulary** | Perlin noise, simplex, fractal (octaved), domain warping, curl noise |
| **Particle systems** | Physics-based, flocking, trail-drawing, attractor-driven, flow-field following |
| **Shape language** | Geometric primitives, custom vertices, bezier curves |
| **Motion style** | Eased, spring-based, noise-driven, physics sim, lerped, stepped |
| **Typography** | System fonts, loaded OTF, `textToPoints()` particle text, kinetic |
| **Shader effects** | GLSL fragment/vertex, filter shaders, post-processing |
| **Composition** | Grid, radial, golden ratio, rule of thirds, organic scatter, tiled |
| **Interaction model** | Mouse follow, click spawn, drag, keyboard state, scroll-driven |
| **Blend modes** | `BLEND`, `ADD`, `MULTIPLY`, `SCREEN`, `DIFFERENCE`, `EXCLUSION`, `OVERLAY` |
| **Layering** | `createGraphics()` offscreen buffers, alpha compositing, masking |

### Core Rules

- **Custom color palette always** — never raw `fill(255,0,0)`. Design a 3-7 color palette
- **Background treatment** — never plain `background(0)` or `background(255)`. Texture, gradient, or layered
- **Stroke weight vocabulary** — thin (0.5), medium (1-2), bold (3-5)
- **Motion variety** — different speeds for different elements
- **Seeded randomness** — always `randomSeed()` + `noiseSeed()` for reproducibility
- **Color mode** — use `colorMode(HSB, 360, 100, 100, 100)` for intuitive control

## HTML Template

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
const PALETTE = { bg: '#0a0a0f', primary: '#e8d5b7' };

function setup() {
  createCanvas(windowWidth, windowHeight);
  randomSeed(CONFIG.seed);
  noiseSeed(CONFIG.seed);
  colorMode(HSB, 360, 100, 100, 100);
}

function draw() {
  // Render frame
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}
</script>
</body>
</html>
```

## Important

- **Canvas fits the window** — use `createCanvas(windowWidth, windowHeight)` and handle `windowResized()`
- **Disable FES** — always `p5.disableFriendlyErrors = true;` before `setup()`
- **No external dependencies** beyond p5.js CDN
- **Call `render_visual`** with the full HTML string when ready
- **Iterate** — if user gives feedback, edit the HTML and call `render_visual` again
