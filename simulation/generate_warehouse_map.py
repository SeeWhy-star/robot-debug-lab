#!/usr/bin/env python3
"""Generate the Nav2 map and browser preview from the Gazebo world geometry."""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
MAP_DIR = ROOT / "simulation" / "maps"
WEB_DIR = ROOT / "web" / "public" / "assets"

RESOLUTION = 0.05
ORIGIN_X = -5.0
ORIGIN_Y = -4.0
WIDTH = 200
HEIGHT = 160

# These are the collision footprints in rdl_warehouse.sdf.xacro.
OBSTACLES = [
    (-5.0, -4.0, 5.0, -3.78),
    (-5.0, 3.78, 5.0, 4.0),
    (-5.0, -4.0, -4.78, 4.0),
    (4.78, -4.0, 5.0, 4.0),
    (-3.45, 0.70, -2.55, 2.90),
    (-3.45, -2.90, -2.55, -0.70),
    (-0.65, 2.00, 1.65, 2.80),
    (-0.65, -2.80, 1.65, -2.00),
    (2.75, -0.60, 3.65, 1.60),  # Shelf A
    (0.20, -0.85, 0.90, -0.15),  # crate / temporary obstacle
]


def pixel(x: float, y: float) -> tuple[int, int]:
    """Map world coordinates to image coordinates (image origin is top-left)."""
    return (
        round((x - ORIGIN_X) / RESOLUTION),
        HEIGHT - 1 - round((y - ORIGIN_Y) / RESOLUTION),
    )


def main() -> None:
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    occupancy = Image.new("L", (WIDTH, HEIGHT), 254)
    draw = ImageDraw.Draw(occupancy)
    for x0, y0, x1, y1 in OBSTACLES:
        left, bottom = pixel(x0, y0)
        right, top = pixel(x1, y1)
        draw.rectangle((left, top, right, bottom), fill=0)
    occupancy.save(MAP_DIR / "rdl_warehouse.pgm")

    preview = Image.new("RGB", occupancy.size, "#102126")
    pixels = preview.load()
    source = occupancy.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            pixels[x, y] = (54, 82, 88) if source[x, y] < 128 else (16, 33, 38)
    preview = preview.resize((1000, 800), Image.Resampling.NEAREST)
    preview.save(WEB_DIR / "rdl-warehouse-map.png")


if __name__ == "__main__":
    main()
