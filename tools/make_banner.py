from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "docs" / "brand"
SOURCE = BRAND_DIR / "chaos.jpg"
BANNER = BRAND_DIR / "banner.png"
HEADER = BRAND_DIR / "header.png"

NEAR_BLACK = (13, 17, 23)
TITLE = (230, 237, 243)
SUBTITLE = (201, 209, 217)
ACCENT = (167, 139, 250)

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/segoeuib.ttf"),
    Path("C:/Windows/Fonts/bahnschrift.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
]


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def resize_to_height(image: Image.Image, height: int) -> Image.Image:
    width = round(image.width * height / image.height)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def sampled_edge(image: Image.Image, target_height: int, x: int = 0) -> list[tuple[int, int, int]]:
    column = image.crop((x, 0, x + 1, image.height)).resize(
        (1, target_height), Image.Resampling.BICUBIC
    )
    column = column.filter(ImageFilter.GaussianBlur(radius=18))
    return [column.getpixel((0, y))[:3] for y in range(target_height)]


def blend_background(
    width: int,
    height: int,
    edge_colors: list[tuple[int, int, int]],
    seed: int,
) -> Image.Image:
    rng = random.Random(seed)
    pixels = Image.new("RGB", (width, height))
    px = pixels.load()

    for y in range(height):
        edge = edge_colors[min(y, len(edge_colors) - 1)]
        for x in range(width):
            t = x / max(width - 1, 1)
            # Keep the text field calm; let the sampled edge color arrive near the art.
            t = t**2.4
            t = t * t * (3 - 2 * t)
            dust = rng.randint(-3, 3) if rng.random() < 0.035 else 0
            r = round(NEAR_BLACK[0] * (1 - t) + edge[0] * t) + dust
            g = round(NEAR_BLACK[1] * (1 - t) + edge[1] * t) + dust
            b = round(NEAR_BLACK[2] * (1 - t) + edge[2] * t) + dust
            px[x, y] = (
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
            )

    dust_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dust_layer)
    for _ in range(width * height // 2400):
        x = rng.randrange(width)
        y = rng.randrange(height)
        alpha = rng.randrange(16, 44)
        tone = rng.choice([(230, 237, 243), (167, 139, 250), (125, 92, 255)])
        draw.point((x, y), fill=(*tone, alpha))

    dust_layer = dust_layer.filter(ImageFilter.GaussianBlur(radius=0.35))
    return Image.alpha_composite(pixels.convert("RGBA"), dust_layer).convert("RGB")


def subtle_radial_background(
    width: int,
    height: int,
    center: tuple[int, int],
    radius: int,
    seed: int,
) -> Image.Image:
    rng = random.Random(seed)
    base = Image.new("RGBA", (width, height), (*NEAR_BLACK, 255))
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_px = glow.load()

    for y in range(height):
        for x in range(width):
            dx = (x - center[0]) / radius
            dy = (y - center[1]) / radius
            distance = min(1.0, (dx * dx + dy * dy) ** 0.5)
            opacity = round(64 * (1 - distance) ** 2)
            if opacity:
                glow_px[x, y] = (*ACCENT, opacity)

    dust_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dust_layer)
    for _ in range(width * height // 3600):
        x = rng.randrange(width)
        y = rng.randrange(height)
        alpha = rng.randrange(10, 28)
        tone = rng.choice([(230, 237, 243), (167, 139, 250), (125, 92, 255)])
        draw.point((x, y), fill=(*tone, alpha))

    dust_layer = dust_layer.filter(ImageFilter.GaussianBlur(radius=0.35))
    return Image.alpha_composite(Image.alpha_composite(base, glow), dust_layer).convert("RGB")


def paste_with_left_feather(
    canvas: Image.Image,
    image: Image.Image,
    xy: tuple[int, int],
    feather_width: int,
) -> None:
    rgba = image.convert("RGBA")
    alpha = Image.new("L", rgba.size, 255)
    alpha_px = alpha.load()
    for x in range(min(feather_width, rgba.width)):
        t = x / max(feather_width - 1, 1)
        t = t * t * (3 - 2 * t)
        value = round(255 * t)
        for y in range(rgba.height):
            alpha_px[x, y] = value
    rgba.putalpha(alpha)
    canvas.paste(rgba, xy, rgba)


def draw_text_block(
    image: Image.Image,
    *,
    title_size: int,
    subtitle_size: int,
    note_size: int,
    margin: int,
    max_text_width: int,
) -> None:
    draw = ImageDraw.Draw(image)
    title_font = font(title_size)
    subtitle_font = fit_font(
        "Solana wallet research, token ranking, paper trading.",
        subtitle_size,
        max_text_width,
        draw,
    )
    note_font = font(note_size)

    lines = [
        ("chaos-trader", title_font, TITLE),
        ("Solana wallet research, token ranking, paper trading.", subtitle_font, SUBTITLE),
    ]
    boxes = [draw.textbbox((0, 0), text, font=line_font, anchor="ls") for text, line_font, _ in lines]

    baselines = [0]
    baselines.append(28 - boxes[1][1])

    block_top = min(baseline + box[1] for baseline, box in zip(baselines, boxes))
    block_bottom = max(baseline + box[3] for baseline, box in zip(baselines, boxes))
    offset_y = round((image.height - (block_bottom - block_top)) / 2 - block_top)

    for (text, line_font, fill), baseline in zip(lines, baselines):
        draw.text((margin, offset_y + baseline), text, font=line_font, fill=fill, anchor="ls")


def fit_font(
    text: str,
    size: int,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    while size > 18:
        candidate = font(size)
        bbox = draw.textbbox((0, 0), text, font=candidate)
        if bbox[2] - bbox[0] <= max_width:
            return candidate
        size -= 1
    return font(size)


def make_banner(source: Image.Image) -> Image.Image:
    character = resize_to_height(source, 640)
    left = blend_background(640, 640, sampled_edge(character, 640, 0), seed=7)
    canvas = Image.new("RGB", (1280, 640), NEAR_BLACK)
    canvas.paste(left, (0, 0))
    paste_with_left_feather(canvas, character, (640, 0), feather_width=220)
    draw_text_block(
        canvas,
        title_size=72,
        subtitle_size=28,
        note_size=22,
        margin=72,
        max_text_width=540,
    )
    return canvas


def make_header(source: Image.Image) -> Image.Image:
    # Crop the existing artwork to the face and hair area, then place it on the right.
    crop = source.crop((120, 0, 640, 640))
    character = resize_to_height(crop, 350)
    right_x = 1400 - character.width
    canvas = subtle_radial_background(
        1400,
        350,
        center=(right_x + 170, 175),
        radius=300,
        seed=17,
    )
    paste_with_left_feather(canvas, character, (right_x, 0), feather_width=220)
    draw_text_block(
        canvas,
        title_size=56,
        subtitle_size=24,
        note_size=19,
        margin=72,
        max_text_width=760,
    )
    return canvas


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    source = Image.open(SOURCE).convert("RGB")

    banner = make_banner(source)
    header = make_header(source)

    banner.save(BANNER)
    header.save(HEADER)

    print(f"{BANNER.as_posix()} {banner.size[0]}x{banner.size[1]}")
    print(f"{HEADER.as_posix()} {header.size[0]}x{header.size[1]}")


if __name__ == "__main__":
    main()
