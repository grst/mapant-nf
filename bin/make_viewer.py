#!/usr/bin/env python3
"""
Fill in the map's style from its template, draw the sprite it needs, and copy the viewer.

assets/viewer/style.json is the whole MapLibre style: every layer, filter, dash and colour, in
karttapullautin's compositing order, editable as it is (Maputnik opens it). The vector tiles carry
classes, not colours -- every feature has karttapullautin's class as `layer` and its ISOM symbol as
`isom` -- so re-styling needs no re-rendering. Only what depends on the run is a placeholder:

    ["mapant:metres", m]      m metres of ground, in screen pixels at every zoom (at least one)
    ["mapant:scale", m, px]   an icon scale that draws px sprite pixels as m metres of ground
    "mapant:color:<name>"     a colour the render took from the ini (green-406, ..., building)
    "mapant:pattern:<name>"   an undergrowth pattern, switched by zoom
    "mapant:zoom:base"        the pyramid's shallowest zoom

Widths are in ground metres because karttapullautin's are: it draws at 600 dpi for 1:10 000, so a
line covers the same ground at every zoom, as it does on paper. Web mercator resolution halves per
zoom, so an exponential-base-2 interpolation is exact -- at one latitude, which is the region's
centre. Zooms are MapLibre's, i.e. 512 px tiles.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import math
import shutil
import sys
from pathlib import Path

import mercantile
from PIL import Image

# Metres of ground per screen pixel at zoom 0 on the equator, in 512 px tiles.
Z0_METRES_PER_PIXEL = 78271.51696402048

# src/palette.rs and src/render.rs: the colours the sprite is drawn in.
BROWN = (166, 85, 43)
UNDERGROWTH = (64, 121, 0)
MARSH = (0, 10, 220)

#: The undergrowth stripes as karttapullautin draws them, in ground metres: a stripe on each edge
#: of its 18 m cell for ISOM 407, and one more through the middle for 409.
UNDERGROWTH_SPACING_M = {"undergrowth": 18.0, "undergrowth-dense": 9.0}
UNDERGROWTH_STROKE_M = 0.85

#: ISOM 308, marsh -- a 0.15 mm blue stripe every 0.45 mm: period and stroke in sprite pixels.
MARSH_PATTERN = (3, 1)

#: The slope line icon, in sprite pixels: a tick hanging from the middle of a 6 x 14 box.
SLOPE_TICK_SIZE = (6, 14)

#: How far past the deepest zoom widths and patterns keep scaling, for a viewer that overzooms.
OVERZOOM = 4


def read_ini(path: Path) -> dict[str, str]:
    """A karttapullautin ini has no section header; see render_ini.py."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string("[pullauta]\n" + path.read_text())
    return dict(cp["pullauta"])


def rgb(color: tuple[int, int, int]) -> str:
    return "rgb({},{},{})".format(*color)


def green_shades(count: int, greentone: int) -> list[tuple[int, int, int]]:
    """karttapullautin's green ramp, exactly as `palette.rs` computes it."""
    if count < 2:
        return [(greentone, 254, greentone)]
    return [
        (
            int(greentone - greentone / (count - 1) * i),
            int(254.0 - (74.0 / (count - 1)) * i),
            int(greentone - greentone / (count - 1) * i),
        )
        for i in range(count)
    ]


def ini_colors(ini: dict[str, str]) -> dict[str, str]:
    """
    The colours the render took from the ini.

    karttapullautin draws each green shade index in its own tone and maps the indices to ISOM
    symbols by `greenshadeisom`; a symbol is drawn in the darkest tone that maps to it, which is
    where its class boundary sits on the rendered map.
    """
    shades = green_shades(
        count=len([s for s in ini.get("greenshades", "").split("|") if s]) or 11,
        greentone=int(float(ini.get("lightgreentone", "200"))),
    )
    shade_isom = [c.strip() for c in ini.get("greenshadeisom", "406|406|408|408|410").split("|") if c]
    colors = {"green-default": rgb(shades[-1])}
    for code in ("406", "408", "410"):
        indices = [i for i, c in enumerate(shade_isom) if c == code]
        colors[f"green-{code}"] = rgb(shades[min(indices[-1], len(shades) - 1)] if indices else shades[-1])

    building = [c.strip() for c in ini.get("buildingcolor", "0,0,0").split(",")]
    colors["building"] = rgb(tuple(int(c) for c in building)) if len(building) == 3 else rgb((0, 0, 0))
    return colors


def pixels_per_metre(zoom: int, latitude: float) -> float:
    return 2**zoom / (Z0_METRES_PER_PIXEL * math.cos(math.radians(latitude)))


class Filler:
    """Replaces the template's placeholders with this run's values."""

    def __init__(self, base_zoom: int, max_zoom: int, latitude: float, ini: dict[str, str]) -> None:
        self.latitude = latitude
        # Two anchors, the upper one past the deepest cut zoom so overzoomed tiles keep scaling.
        self.anchors = (base_zoom, max_zoom + OVERZOOM)
        self.pattern_zooms = range(base_zoom, max_zoom + OVERZOOM)
        self.values = {
            "color": ini_colors(ini),
            "pattern": {name: self.pattern_by_zoom(name) for name in UNDERGROWTH_SPACING_M},
            "zoom": {"base": base_zoom},
        }

    def by_zoom(self, pixels_at) -> list:
        low, high = self.anchors
        return ["interpolate", ["exponential", 2], ["zoom"], low, pixels_at(low), high, pixels_at(high)]

    def metres(self, metres: float) -> list:
        # The floor goes on each anchor rather than around the whole thing: the style spec only
        # allows a "zoom" expression as the direct input of a top-level interpolate. Interpolating
        # between two floored values stays above the floor anyway.
        return self.by_zoom(
            lambda z: max(1.0, round(metres * pixels_per_metre(z, self.latitude), 4))
        )

    def scale(self, metres: float, pixels: float) -> list:
        return self.by_zoom(lambda z: round(metres * pixels_per_metre(z, self.latitude) / pixels, 4))

    def pattern_by_zoom(self, name: str) -> list:
        """
        A `fill-pattern` expression picking `name`'s pattern for the zoom.

        A fill pattern is drawn in screen pixels at every zoom, so a single image would put the
        stripes a fixed number of pixels apart -- ten times too dense zoomed in, a grey wash zoomed
        out. One image per zoom keeps them the distance apart on the ground the rendered map has.
        """
        zooms = self.pattern_zooms
        expression: list = ["step", ["zoom"], f"{name}-z{zooms[0]}"]
        for zoom in zooms[1:]:
            expression += [zoom, f"{name}-z{zoom}"]
        return expression

    def fill(self, node):
        if isinstance(node, list):
            if node and node[0] == "mapant:metres":
                return self.metres(*node[1:])
            if node and node[0] == "mapant:scale":
                return self.scale(*node[1:])
            return [self.fill(item) for item in node]
        if isinstance(node, dict):
            return {key: self.fill(value) for key, value in node.items()}
        if isinstance(node, str) and node.startswith("mapant:"):
            kind, _, name = node.removeprefix("mapant:").partition(":")
            try:
                return self.values[kind][name]
            except KeyError:
                sys.exit(f"make_viewer.py: unknown placeholder {node!r} in the style template")
        return node

    def patterns(self) -> dict[str, tuple[int, int, str, tuple[int, int, int]]]:
        """Every pattern the sprite holds: (period, stroke, stripe direction, colour)."""
        patterns = {"marsh": (*MARSH_PATTERN, "horizontal", MARSH)}
        for zoom in self.pattern_zooms:
            ppm = pixels_per_metre(zoom, self.latitude)
            stroke = max(1, round(UNDERGROWTH_STROKE_M * ppm))
            for name, spacing in UNDERGROWTH_SPACING_M.items():
                period = min(256, max(stroke + 2, round(spacing * ppm)))
                patterns[f"{name}-z{zoom}"] = (period, stroke, "vertical", UNDERGROWTH)
        return patterns


def write_sprite(out_dir: Path, patterns: dict) -> None:
    """
    Write the sprite at both pixel ratios: MapLibre asks for the @2x sheet on a display that has
    one, and draws no pattern at all if that request fails.
    """
    for ratio in (1, 2):
        images: dict[str, Image.Image] = {}
        for name, (period, stroke, direction, color) in patterns.items():
            size = period * ratio
            image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            for along in range(size):
                for across in range(stroke * ratio):
                    xy = (across, along) if direction == "vertical" else (along, across)
                    image.putpixel(xy, (*color, 255))
            images[name] = image

        # The slope line: a tick hanging from the middle of the icon. Placed along a line, an icon's
        # bottom is on the right of the line's direction, and karttapullautin writes every contour
        # with its downhill side on the right -- so the tick points downhill.
        width, height = SLOPE_TICK_SIZE
        tick = Image.new("RGBA", (width * ratio, height * ratio), (0, 0, 0, 0))
        for along in range((height // 2) * ratio, height * ratio):
            for across in range((width // 2 - 1) * ratio, (width // 2 + 1) * ratio):
                tick.putpixel((across, along), (*BROWN, 255))
        images["slope-tick"] = tick

        sheet = Image.new(
            "RGBA",
            (sum(i.width for i in images.values()), max(i.height for i in images.values())),
            (0, 0, 0, 0),
        )
        boxes, x = {}, 0
        for name, image in images.items():
            sheet.paste(image, (x, 0))
            boxes[name] = {"width": image.width, "height": image.height, "x": x, "y": 0,
                           "pixelRatio": ratio}
            x += image.width

        suffix = "" if ratio == 1 else "@2x"
        sheet.save(out_dir / f"sprite{suffix}.png")
        (out_dir / f"sprite{suffix}.json").write_text(json.dumps(boxes, indent=1) + "\n")


def region_latitude(parent_tiles: Path) -> float:
    """The centre latitude of the parent tiles' envelope, which is what the widths are exact at."""
    south = north = None
    with parent_tiles.open(newline="") as fh:
        for row in csv.DictReader(fh):
            b = mercantile.bounds(int(row["x"]), int(row["y"]), int(row["z"]))
            south = b.south if south is None else min(south, b.south)
            north = b.north if north is None else max(north, b.north)
    if south is None:
        sys.exit(f"make_viewer.py: {parent_tiles} has no rows")
    return (south + north) / 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template-dir", type=Path, required=True, help="assets/viewer")
    ap.add_argument("--parent-tiles", type=Path, required=True)
    ap.add_argument("--ini", type=Path, required=True, help="the effective pullauta.ini of the render")
    ap.add_argument("--base-zoom", type=int, required=True)
    ap.add_argument("--max-zoom", type=int, required=True)
    ap.add_argument("--title", default="mapant")
    ap.add_argument("--attribution", default="")
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args(argv)

    filler = Filler(args.base_zoom, args.max_zoom, region_latitude(args.parent_tiles), read_ini(args.ini))
    style = filler.fill(json.loads((args.template_dir / "style.json").read_text()))
    style["name"] = args.title
    style["sources"]["mapant"]["attribution"] = args.attribution
    # A misspelt placeholder that fill() did not recognise as one, e.g. ["mapant:meters", 3].
    if '"mapant:' in json.dumps(style):
        sys.exit("make_viewer.py: the style template has a placeholder that was not filled in")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "style.json").write_text(json.dumps(style, indent=1) + "\n")
    write_sprite(args.out_dir, filler.patterns())
    shutil.copy(args.template_dir / "index.html", args.out_dir / "index.html")
    print(f"style.json, sprite and index.html for zoom {args.base_zoom}-{args.max_zoom} "
          f"at latitude {filler.latitude:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
