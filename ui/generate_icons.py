"""Generate PWA icons for webAgent."""
from PIL import Image, ImageDraw, ImageFont
import os

def create_icon(size, output_path):
    """Create a PWA icon matching the webAgent brand.

    A terminal-like glyph on a dark rounded square.
    """
    pad = size // 8
    r = size // 6  # corner radius
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background: rounded dark rectangle
    draw.rounded_rectangle(
        [(pad, pad), (size - pad, size - pad)],
        radius=r,
        fill=(13, 13, 26, 255),  # --bg-0: #0d0d1a
    )

    # Accent border
    draw.rounded_rectangle(
        [(pad, pad), (size - pad, size - pad)],
        radius=r,
        outline=(125, 207, 255, 255),  # --accent: #7dcfff
        width=max(1, size // 64),
    )

    # Terminal prompt glyph: ">_" cursor
    cx = size // 2
    cy = size // 2
    glyph_w = size // 3
    glyph_h = size // 5
    stroke = max(1, size // 20)

    # Chevron >
    points = [
        (cx - glyph_w // 2, cy),
        (cx - glyph_w // 4, cy - glyph_h // 2),
        (cx - glyph_w // 4, cy + glyph_h // 2),
    ]
    draw.line(points, fill=(192, 202, 245, 255), width=stroke)

    # Cursor _
    cursor_x = cx + glyph_w // 4
    cursor_y = cy + glyph_h // 3
    cursor_w = glyph_w // 3
    draw.line(
        [(cursor_x, cursor_y), (cursor_x + cursor_w, cursor_y)],
        fill=(192, 202, 245, 255),
        width=stroke,
    )

    img.save(output_path, "PNG")


def create_maskable_icon(size, output_path):
    """Create a maskable variant (safe zone = 80% of canvas)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    safe = size * 0.2  # 20% margin per spec
    r = int(size * 0.12)

    # Full background fill (safe zone covers entire canvas)
    draw.rounded_rectangle(
        [(0, 0), (size, size)],
        radius=r if size > 256 else size // 12,
        fill=(13, 13, 26, 255),
    )

    draw.rounded_rectangle(
        [(0, 0), (size, size)],
        radius=r if size > 256 else size // 12,
        outline=(125, 207, 255, 255),
        width=max(1, size // 64),
    )

    # Glyph stays inside safe zone
    cx = size // 2
    cy = size // 2
    glyph_w = size // 4
    glyph_h = size // 6
    stroke = max(1, size // 20)

    # Chevron >
    points = [
        (cx - glyph_w // 2, cy),
        (cx - glyph_w // 4, cy - glyph_h // 2),
        (cx - glyph_w // 4, cy + glyph_h // 2),
    ]
    draw.line(points, fill=(192, 202, 245, 255), width=stroke)

    cursor_x = cx + glyph_w // 4
    cursor_y = cy + glyph_h // 3
    cursor_w = glyph_w // 3
    draw.line(
        [(cursor_x, cursor_y), (cursor_x + cursor_w, cursor_y)],
        fill=(192, 202, 245, 255),
        width=stroke,
    )

    img.save(output_path, "PNG")


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(__file__), "icons")
    os.makedirs(out_dir, exist_ok=True)

    sizes = {
        "icon-192x192.png": 192,
        "icon-512x512.png": 512,
    }
    for fname, sz in sizes.items():
        create_icon(sz, os.path.join(out_dir, fname))
        print(f"Created {fname}")

    create_maskable_icon(192, os.path.join(out_dir, "icon-192x192-maskable.png"))
    print("Created icon-192x192-maskable.png")
    create_maskable_icon(512, os.path.join(out_dir, "icon-512x512-maskable.png"))
    print("Created icon-512x512-maskable.png")
