"""Render our original camera icon, not an official Baseus or HA logo."""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def render_icon(size):
    # Supersample for clean small-radius curves on both 256px and 512px icons.
    image = Image.new("RGBA", (1536, 1536))
    draw = ImageDraw.Draw(image)
    scale = 3

    def box(coordinates):
        return tuple(int(value * scale) for value in coordinates)

    yellow, ink = "#FFD229", "#171C26"
    draw.rounded_rectangle(box((0, 0, 512, 512)), radius=112 * scale, fill=yellow)
    draw.rounded_rectangle(box((151, 114, 261, 193)), radius=23 * scale, fill=ink)
    draw.rounded_rectangle(box((73, 158, 439, 393)), radius=51 * scale, fill=ink)
    draw.ellipse(box((174, 196, 338, 360)), fill=yellow)
    draw.ellipse(box((196, 218, 316, 338)), fill=ink)
    draw.ellipse(box((218, 239, 246, 267)), fill="#FFF6CB")
    draw.rounded_rectangle(box((366, 193, 403, 208)), radius=7 * scale, fill=yellow)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def build(directory=None):
    directory = directory or ROOT / "custom_components/baseus_s1/brand"
    directory.mkdir(parents=True, exist_ok=True)
    for size, name in ((256, "icon.png"), (512, "icon@2x.png")):
        render_icon(size).save(directory / name, optimize=True)
    return directory


if __name__ == "__main__":
    print(build())
