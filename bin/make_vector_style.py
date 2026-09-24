#!/usr/bin/env python3
"""
Write the MapLibre style, metadata and preview page for the vector pyramid.

The vector tiles carry classes, not colours -- `c` for a traced raster class, `k` for a
karttapullautin layer name, `isom` for a shape's symbol code -- so the map's whole appearance lives
in this one document, and re-styling it needs no re-rendering.

Every colour and width of the terrain is karttapullautin's own, taken from its source rather than
sampled from an image: the palette from `src/palette.rs` and the line widths from the square brush
`draw_curves` strokes with. Widths are therefore given in **ground metres** and interpolated with
an exponential base of 2, which is exact for web mercator: a line then covers the same ground at
every zoom, as it does on the paper map.

The OSM shapes are the one place this departs from the rendered image. karttapullautin strokes them
as flat coloured lines of one width, which is not what any of them looks like on a map, so they are
drawn here as the ISOM 2017-2 symbols they were matched to instead: a road with its black casing, a
vehicle track and a footpath with their own dashes, a marsh with its stripes. The widths and dash
lengths in `ISOM_LINES` are the symbol set's own, in millimetres of paper at 1:10 000 -- where a
millimetre of paper is ten metres of ground, which is the unit everything else here is in too.

The style is plain style-spec v8, so MapLibre GL, Mapbox GL and OpenLayers (through
ol-mapbox-style) all take it; only the source declaration differs, which `--tiles-url` sets.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import mercantile

# Metres of ground per pixel of karttapullautin's render: it draws at 600 dpi for a 1:10 000 map,
# so one pixel is 254/600 mm on paper and 10 000 times that on the ground.
METRES_PER_RENDER_PIXEL = 254.0 / 600.0

# src/render.rs: the contour brown, and the default of `depressions_color`.
BROWN = (166, 85, 43)
PURPLE = (200, 0, 200)
BLACK = (0, 0, 0)
# src/palette.rs: open land, and the undergrowth green.
YELLOW = (255, 219, 166)
UNDERGROWTH = (64, 121, 0)
# src/shapefile/render.rs: the canvas colours karttapullautin strokes the shapes with. Only the
# water colours are still used as such -- for the rest see ISOM_LINES below.
KP_BLUE = (29, 190, 255)
KP_MARSH = (0, 10, 220)
KP_OLIVE = (194, 176, 33)
KP_SHAPE_YELLOW = (255, 184, 83)

# The ISOM 2017-2 colours the shapes are drawn in, converted from the CMYK of the symbol set the
# same way OpenOrienteering Mapper converts them (255*(1-ink)*(1-black)), except for water, which
# stays karttapullautin's own so that a stream and a detected lake are the same blue.
ISOM_BLACK = (0, 0, 0)
ISOM_BROWN = (209, 92, 0)  # Brown 100%: 0/56/100/18
ISOM_BROWN_50 = (232, 167, 116)  # Upper brown 50%: 0/28/50/9
ISOM_WHITE = (255, 255, 255)


@dataclass(frozen=True)
class IsomLine:
    """One line symbol of ISOM 2017-2, in millimetres of paper at 1:10 000."""

    symbol: str  # the 2017-2 symbol number, for the style layer's id
    color: tuple[int, int, int]
    width_mm: float
    #: Dash and gap lengths, alternating. Empty for a solid line.
    dashes: tuple[float, ...] = ()
    #: A wider line drawn underneath: the black casing of a road.
    casing_mm: float = 0.0
    #: A line drawn on top of this one: the black dashes of a railway over its white bed.
    over: tuple[tuple[int, int, int], float, tuple[float, ...]] | None = None


# karttapullautin's shapefile rules speak ISOM 2000, which is what the `isom` property carries, so
# each code is crosswalked to its 2017-2 equivalent first -- the same crosswalk the OCAD export
# uses. Codes ending in T are the same symbol on a tunnel or bridge section.
ISOM_LINES: dict[str, IsomLine] = {
    # Small crossable watercourse, 0.27 mm blue.
    "306": IsomLine("305", KP_BLUE, 0.27),
    # The bank line of an uncrossable lake.
    "301.1": IsomLine("301", ISOM_BLACK, 0.18),
    # Wide road: brown 50% between two black edges (0.45 + 2 x 0.21 mm).
    "503": IsomLine("502", ISOM_BROWN_50, 0.45, casing_mm=0.87),
    # Road: solid black, 0.525 mm.
    "504": IsomLine("503", ISOM_BLACK, 0.525),
    # Vehicle track: the same line dashed, 4.5 mm dashes with 0.375 mm gaps.
    "505": IsomLine("504", ISOM_BLACK, 0.525, dashes=(4.5, 0.375)),
    # Small footpath: 0.27 mm, 1.5 mm dashes.
    "507": IsomLine("506", ISOM_BLACK, 0.27, dashes=(1.5, 0.375)),
    # Railway: black dashes over a white bed, both 0.525 mm.
    "515": IsomLine(
        "509", ISOM_WHITE, 0.525, over=(ISOM_BLACK, 0.525, (2.25, 1.5))
    ),
    # Power line: 0.21 mm. The bars that mark the pylons need a symbol along the line, which a
    # style cannot draw, so the line is plain.
    "516": IsomLine("510", ISOM_BLACK, 0.21),
    # Impassable fence: 0.375 mm. Its cross ticks are left off for the same reason.
    "524": IsomLine("518", ISOM_BLACK, 0.375),
    # The edge of a paved area, and karttapullautin's generic black line.
    "529.1": IsomLine("501.1", ISOM_BLACK, 0.14),
    "414": IsomLine("516", ISOM_BLACK, 0.21),
}

# The area fills, by the ISOM 2000 code karttapullautin matched. `pattern` names an image in the
# sprite this script writes; `color` is a flat fill.
ISOM_AREAS: dict[str, dict] = {
    "401": {"color": KP_SHAPE_YELLOW, "symbol": "403"},  # rough open land
    "310": {"pattern": "marsh", "symbol": "308"},  # marsh: blue stripes
    "527": {"color": KP_OLIVE, "symbol": "520"},  # area that shall not be entered
    "529": {"color": ISOM_BROWN_50, "symbol": "501.1"},  # paved area
    "301": {"color": KP_BLUE, "symbol": "301"},  # uncrossable body of water
}

# Millimetres of paper per metre of ground on a 1:10 000 map: one is ten of the other.
METRES_PER_MM = 10.0

OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def rgb(color: tuple[int, int, int], alpha: float | None = None) -> str:
    r, g, b = color
    return f"rgb({r},{g},{b})" if alpha is None else f"rgba({r},{g},{b},{alpha})"


def brush_metres(curvew: float) -> float:
    """
    Ground width of the square brush `draw_curves` strokes a curve with.

    It offsets each segment from `-curvew - 0.5` to `+curvew + 0.5` in both axes, so the stroke is
    `2 * curvew + 1` render pixels wide -- not `curvew`, which is the mistake that makes every line
    on a re-implementation come out too thin.
    """
    return (2.0 * curvew + 1.0) * METRES_PER_RENDER_PIXEL


def render_px_metres(pixels: float) -> float:
    return pixels * METRES_PER_RENDER_PIXEL


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


def width_expression(metres: float, latitude: float, base_zoom: int, max_zoom: int) -> list:
    """
    A line width in pixels that keeps `metres` of ground covered at every zoom.

    Web mercator resolution halves per zoom level, so an exponential-base-2 interpolation between
    two anchor zooms is not an approximation but the exact curve. The floor of one pixel keeps a
    line visible when zoomed far out, where the true width is a fraction of a pixel.
    """

    # Two anchors, the upper one past the deepest cut zoom so overzoomed tiles keep scaling.
    low, high = base_zoom, max_zoom + 4
    # The floor goes on each anchor rather than around the whole thing: the style spec only allows
    # a "zoom" expression as the direct input of a top-level interpolate, so wrapping it in a
    # "max" makes the style invalid. Interpolating between two floored values stays above the
    # floor anyway, so the result is the same curve.
    return [
        "interpolate",
        ["exponential", 2],
        ["zoom"],
        low,
        max(1.0, round(metres * _pixels_per_metre(low, latitude), 4)),
        high,
        max(1.0, round(metres * _pixels_per_metre(high, latitude), 4)),
    ]


def read_ini(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string("[pullauta]\n" + path.read_text())
    return dict(cp["pullauta"])


#: The patterned symbols, as a stripe period and stripe width in pixels of the sprite, plus the
#: direction the stripes run. Kept in screen pixels rather than ground metres because a style
#: cannot scale a pattern with the zoom: this is a texture, and the period below is roughly what
#: the symbol has on paper at the deepest zoom of the pyramid.
PATTERNS: dict[str, tuple[int, int, str, tuple[int, int, int]]] = {
    # ISOM 407, vegetation: slow running, good visibility -- a 0.18 mm stripe every 1.26 mm, so
    # the green covers about a seventh of the area and reads as a texture over what is underneath
    # rather than as a shade of its own.
    "undergrowth": (7, 1, "vertical", UNDERGROWTH),
    # ISOM 409, the same for walking speed, at half the spacing.
    "undergrowth-dense": (3, 1, "vertical", UNDERGROWTH),
    # ISOM 308, marsh -- a 0.15 mm blue stripe every 0.45 mm, across the map.
    "marsh": (3, 1, "horizontal", KP_MARSH),
}


def write_sprite(out_dir: Path) -> None:
    """
    Write the sprite the patterned area symbols need, at both pixel ratios.

    A style cannot carry an image inline, and MapLibre asks for the @2x sheet as soon as it is on a
    display that has one -- and draws no pattern at all if that request fails -- so both are
    written next to the style that names them.
    """
    from PIL import Image

    for ratio in (1, 2):
        boxes: dict[str, dict] = {}
        images = []
        offset = 0
        for name, (period, stroke, direction, color) in PATTERNS.items():
            size = period * ratio
            image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            pixels = image.load()
            for along in range(size):
                for across in range(stroke * ratio):
                    if direction == "vertical":
                        pixels[across, along] = (*color, 255)
                    else:
                        pixels[along, across] = (*color, 255)
            boxes[name] = {
                "width": size,
                "height": size,
                "x": offset,
                "y": 0,
                "pixelRatio": ratio,
            }
            images.append(image)
            offset += size

        sheet = Image.new("RGBA", (offset, max(i.height for i in images)), (0, 0, 0, 0))
        position = 0
        for image in images:
            sheet.paste(image, (position, 0))
            position += image.width

        suffix = "" if ratio == 1 else "@2x"
        sheet.save(out_dir / f"sprite{suffix}.png")
        (out_dir / f"sprite{suffix}.json").write_text(json.dumps(boxes, indent=1) + "\n")


def codes_for(code: str) -> list[str]:
    """A shape code and its tunnel/bridge variant, which is the same symbol."""
    return [code, code + "T"]


def dash_array(spec: IsomLine) -> list[float]:
    """
    A dash pattern in multiples of the line width, which is how the style spec measures one.

    Because the width is in ground metres, so is the dash: it keeps the same length on the ground
    at every zoom, as it does on paper.
    """
    return [round(length / spec.width_mm, 3) for length in spec.dashes]


def shape_line_layer(code: str, spec: IsomLine, casing: bool, width, suffix: str = "") -> dict:
    """One style layer for one ISOM line symbol -- its casing, or the line itself."""
    millimetres = spec.casing_mm if casing else spec.width_mm
    layer = {
        # Named after the code it filters on, with the ISOM symbol it draws in the metadata:
        # the id is what a downstream style override addresses the layer by.
        "id": f"shape-{code}{'-casing' if casing else suffix}",
        "metadata": {"isom:symbol": spec.symbol},
        "type": "line",
        "source": "mapant",
        "source-layer": "osm_high",
        "filter": ["in", ["get", "isom"], ["literal", codes_for(code)]],
        "paint": {
            "line-color": rgb(ISOM_BLACK if casing else spec.color),
            "line-width": width(millimetres * METRES_PER_MM),
        },
        # line-cap is a layout property, not a paint one. Butt caps, so that the dashes of a track
        # are the length the symbol says and two pieces of the same way still meet.
        "layout": {"line-cap": "butt", "line-join": "round"},
    }
    if spec.dashes and not casing:
        layer["paint"]["line-dasharray"] = dash_array(spec)
    return layer


def area_layers(source_layer: str, codes: tuple[str, ...], extra_filter: list | None = None) -> list[dict]:
    """The fill layers for a set of shape codes, flat or patterned as the symbol requires."""
    layers = []
    for code in codes:
        spec = ISOM_AREAS[code]
        selector: list = ["in", ["get", "isom"], ["literal", codes_for(code)]]
        if extra_filter is not None:
            selector = ["all", extra_filter, selector]
        paint = (
            {"fill-pattern": spec["pattern"]}
            if "pattern" in spec
            else {"fill-color": rgb(spec["color"]), "fill-antialias": False}
        )
        layers.append(
            {
                "id": f"shape-{code}",
                "metadata": {"isom:symbol": spec["symbol"]},
                "type": "fill",
                "source": "mapant",
                "source-layer": source_layer,
                "filter": selector,
                "paint": paint,
            }
        )
    return layers


def build_style(
    *,
    tiles_url: str,
    base_zoom: int,
    max_zoom: int,
    bounds: tuple[float, float, float, float],
    latitude: float,
    ini: dict[str, str],
    title: str,
) -> dict:
    """The whole map as one style document, in karttapullautin's compositing order."""
    shades = green_shades(
        count=len([s for s in ini.get("greenshades", "").split("|") if s]) or 11,
        greentone=int(float(ini.get("lightgreentone", "200"))),
    )
    building = ini.get("buildingcolor", "0,0,0").split(",")
    building_color = tuple(int(c) for c in building) if len(building) == 3 else BLACK
    depressions = ini.get("depressions_color", "200,0,200").split(",")
    depression_color = tuple(int(c) for c in depressions) if len(depressions) == 3 else PURPLE

    def width(metres: float) -> list:
        return width_expression(metres, latitude, base_zoom, max_zoom)

    # Vegetation class -> colour: 1 is open land, 2 upward are the green shades in order.
    vegetation_match: list = ["match", ["get", "c"], 1, rgb(YELLOW)]
    for index, shade in enumerate(shades):
        vegetation_match += [index + 2, rgb(shade)]
    vegetation_match.append(rgb((255, 255, 255)))

    layers: list[dict] = [
        {
            # The paper the map is printed on: white is a colour on an orienteering map (runnable
            # forest), not an absence of one, so it is a layer rather than a transparent gap.
            #
            # It starts at the base zoom because that is where the pyramid starts. Below it there
            # is nothing to draw, and a viewer wants to see where in the world it is looking --
            # which is what the OSM raster underneath is for.
            "id": "paper",
            "type": "background",
            "minzoom": base_zoom,
            "paint": {"background-color": "#ffffff"},
        },
        {
            "id": "vegetation",
            "type": "fill",
            "source": "mapant",
            "source-layer": "vegetation",
            "paint": {"fill-color": vegetation_match, "fill-antialias": False},
        },
    ]

    # Undergrowth is vertical green stripes over whatever vegetation is underneath -- ISOM 407 and
    # 409, the two densities karttapullautin distinguishes. A flat wash of green here would read as
    # one more vegetation shade, which is the opposite of what the symbol says, so it is drawn with
    # the pattern from the sprite this script writes next to the style. There is one layer per
    # density because `fill-pattern` picks one image for the whole layer.
    layers += [
        {
            "id": f"undergrowth-{value}",
            "type": "fill",
            "source": "mapant",
            "source-layer": "undergrowth",
            "filter": ["==", ["get", "c"], value],
            "paint": {"fill-pattern": name},
        }
        for value, name in ((1, "undergrowth"), (2, "undergrowth-dense"))
    ]

    # The OSM area fills karttapullautin composites under the contours.
    layers += area_layers("osm_low", ("401", "310", "527", "529"))

    # The curves. Plain and index contours are brown; everything that belongs to a depression is
    # drawn in the depression colour, which is what `render.rs` does with the same classes.
    layers += [
        {
            "id": "contours",
            "type": "line",
            "source": "mapant",
            "source-layer": "contours",
            "filter": ["==", ["get", "k"], "contour"],
            "paint": {"line-color": rgb(BROWN), "line-width": width(brush_metres(2.0))},
        },
        {
            "id": "contours-index",
            "type": "line",
            "source": "mapant",
            "source-layer": "contours",
            "filter": ["==", ["get", "k"], "contour_index"],
            "paint": {"line-color": rgb(BROWN), "line-width": width(brush_metres(3.5))},
        },
        {
            "id": "depressions",
            "type": "line",
            "source": "mapant",
            "source-layer": "contours",
            "filter": ["in", ["get", "k"], ["literal", ["depression", "slope_line"]]],
            "paint": {
                "line-color": rgb(depression_color),
                "line-width": width(brush_metres(2.0)),
            },
        },
        {
            "id": "depressions-index",
            "type": "line",
            "source": "mapant",
            "source-layer": "contours",
            "filter": ["==", ["get", "k"], "depression_index"],
            "paint": {
                "line-color": rgb(depression_color),
                "line-width": width(brush_metres(3.5)),
            },
        },
        {
            "id": "small-depressions",
            "type": "line",
            "source": "mapant",
            "source-layer": "contours",
            "filter": ["==", ["get", "k"], "small_depression"],
            "paint": {
                "line-color": rgb(depression_color),
                "line-width": width(brush_metres(3.0)),
            },
        },
        {
            "id": "formlines",
            "type": "line",
            "source": "mapant",
            "source-layer": "formlines",
            "paint": {
                "line-color": [
                    "match",
                    ["get", "k"],
                    "formline_depression",
                    rgb(depression_color),
                    rgb(BROWN),
                ],
                "line-width": width(brush_metres(1.5)),
                # In multiples of the line width, which is how the spec measures a dash.
                "line-dasharray": [6.0, 1.5],
            },
        },
        {
            "id": "knolls",
            "type": "circle",
            "source": "mapant",
            "source-layer": "knolls",
            "paint": {
                "circle-color": [
                    "match",
                    ["get", "k"],
                    ["udepression", "uglyudepression"],
                    rgb(depression_color),
                    rgb(BROWN),
                ],
                "circle-radius": width(render_px_metres(7.0) / 2.0),
            },
        },
        {
            "id": "blocks",
            "type": "fill",
            "source": "mapant",
            "source-layer": "blocks",
            "paint": {"fill-color": rgb(BLACK), "fill-antialias": False},
        },
        {
            # 1 is water, 2 is the black ground detail drawn from the same image.
            "id": "water",
            "type": "fill",
            "source": "mapant",
            "source-layer": "water",
            "paint": {
                "fill-color": [
                    "match",
                    ["get", "c"],
                    1,
                    rgb(KP_BLUE),
                    2,
                    rgb(BLACK),
                    rgb(KP_BLUE),
                ],
                "fill-antialias": False,
            },
        },
        {
            "id": "cliffs",
            "type": "line",
            "source": "mapant",
            "source-layer": "cliffs",
            "paint": {"line-color": rgb(BLACK), "line-width": width(render_px_metres(6.0))},
        },
    ]

    # The OSM line work and the fills karttapullautin puts on top: lakes and buildings.
    layers += area_layers(
        "osm_high", ("301",), extra_filter=["==", ["geometry-type"], "Polygon"]
    )
    layers.append(
        {
            # A building is its ground plan filled solid, in whatever colour the render used.
            "id": "shape-526",
            "metadata": {"isom:symbol": "521"},
            "type": "fill",
            "source": "mapant",
            "source-layer": "osm_high",
            "filter": ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "isom"], "526"]],
            "paint": {"fill-color": rgb(building_color)},
        }
    )

    # Every casing first, widest underneath, then every fill, so that a junction between two roads
    # of the same class has no black line across it -- and so a footpath crossing a road is drawn
    # over the road rather than into its casing.
    for casing in (True, False):
        specs = [
            (code, spec)
            for code, spec in ISOM_LINES.items()
            if not casing or spec.casing_mm > spec.width_mm
        ]
        for code, spec in sorted(specs, key=lambda item: -item[1].width_mm):
            layers.append(shape_line_layer(code, spec, casing, width))

    # What goes over the top of its own line: the black dashes of the railway.
    for code, spec in ISOM_LINES.items():
        if spec.over is not None:
            color, width_mm, dashes = spec.over
            layers.append(
                shape_line_layer(
                    code,
                    IsomLine(spec.symbol, color, width_mm, dashes=dashes),
                    False,
                    width,
                    suffix="-over",
                )
            )

    return {
        "version": 8,
        "name": title,
        # Relative, so the published tree works wherever it is hosted; the viewer resolves it the
        # same way it resolves the tile template.
        "sprite": "sprite",
        "metadata": {"mapant:generated-by": "mapant-nf make_vector_style.py"},
        "sources": {
            "mapant": {
                "type": "vector",
                "tiles": [tiles_url],
                "minzoom": base_zoom,
                "maxzoom": max_zoom,
                "bounds": list(bounds),
                "attribution": (
                    'Map data &copy; <a href="https://www.openstreetmap.org/copyright">'
                    "OpenStreetMap contributors</a>, LiDAR &copy; "
                    '<a href="https://geodaten.bayern.de">Bayerische Vermessungsverwaltung</a>, '
                    'rendered with <a href="https://github.com/karttapullautin/karttapullautin">'
                    "karttapullautin</a>"
                ),
            }
        },
        "layers": layers,
    }


def _pixels_per_metre(zoom: int, latitude: float) -> float:
    import math

    return (2**zoom) / (156543.03392804097 * math.cos(math.radians(latitude)))


VIEWER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.css">
<script src="https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.js"></script>
<style>
  html, body { margin: 0; height: 100%; }
  #map { height: 100%; }
  .legend {
    position: absolute; bottom: 12px; left: 12px; z-index: 1;
    background: rgba(255,255,255,.9); padding: 6px 8px; border-radius: 4px;
    font: 12px/1.4 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.3);
  }
</style>
</head>
<body>
<div id="map"></div>
<div class="legend">__LEGEND__</div>
<script>
  // The pyramid's own style, plus OSM's raster tiles underneath for the zoom levels below it --
  // the same arrangement as the raster viewer, and the reason no overview levels are built.
  fetch('style.json').then(r => r.json()).then(style => {
    // style.json keeps a relative tile template so the published tree works wherever it is
    // hosted, but MapLibre needs a resolvable URL. Concatenated rather than run through URL(),
    // which percent-encodes the {z}/{x}/{y} placeholders and asks the server for those literally.
    const base = location.href.replace(/[?#].*$/, '').replace(/[^/]*$/, '');
    const absolute = (url) => (/^[a-z][a-z0-9+.-]*:/i.test(url) ? url : base + url);
    style.sources.mapant.tiles = style.sources.mapant.tiles.map(absolute);
    if (style.sprite) style.sprite = absolute(style.sprite);
    style.sources.osm = {
      type: 'raster',
      tiles: ['__OSM_TILES__'],
      tileSize: 256,
      maxzoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    };
    style.layers.splice(1, 0, {
      id: 'osm-background', type: 'raster', source: 'osm', maxzoom: __BASE_ZOOM__
    });
    new maplibregl.Map({
      container: 'map',
      style: style,
      center: [__LON__, __LAT__],
      zoom: __DEFAULT_ZOOM__,
      maxZoom: __MAX_ZOOM__ + 4,
      // Keeps #zoom/lat/lon in the address bar, so a view of the map can be linked to.
      hash: true
    }).addControl(new maplibregl.NavigationControl());
  });
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parent-tiles", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--base-zoom", type=int, required=True)
    ap.add_argument("--max-zoom", type=int, required=True)
    ap.add_argument("--ini", type=Path, help="the effective pullauta.ini of the render")
    ap.add_argument("--tiles-url", default="{z}/{x}/{y}.pbf")
    ap.add_argument("--title", default="mapant")
    args = ap.parse_args(argv)

    west = south = east = north = None
    parents = 0
    with args.parent_tiles.open(newline="") as fh:
        for row in csv.DictReader(fh):
            b = mercantile.bounds(int(row["x"]), int(row["y"]), int(row["z"]))
            parents += 1
            west = b.west if west is None else min(west, b.west)
            south = b.south if south is None else min(south, b.south)
            east = b.east if east is None else max(east, b.east)
            north = b.north if north is None else max(north, b.north)

    if west is None:
        print(f"make_vector_style.py: {args.parent_tiles} has no rows", file=sys.stderr)
        return 1

    lon, lat = (west + east) / 2, (south + north) / 2
    style = build_style(
        tiles_url=args.tiles_url,
        base_zoom=args.base_zoom,
        max_zoom=args.max_zoom,
        bounds=(west, south, east, north),
        latitude=lat,
        ini=read_ini(args.ini),
        title=args.title,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "style.json").write_text(json.dumps(style, indent=1) + "\n")
    write_sprite(args.out_dir)

    # What a renderer needs before it will accept a bare directory of tiles as a source, and what
    # `pack_pmtiles` copies into the archive's header.
    metadata = {
        "name": args.title,
        "format": "pbf",
        "minzoom": args.base_zoom,
        "maxzoom": args.max_zoom,
        "bounds": f"{west},{south},{east},{north}",
        "center": f"{lon},{lat},{max(args.base_zoom, args.max_zoom - 5)}",
        "type": "overlay",
        "vector_layers": [
            {"id": layer["source-layer"], "fields": {}}
            for layer in style["layers"]
            if "source-layer" in layer
        ],
    }
    (args.out_dir / "metadata.json").write_text(json.dumps(metadata, indent=1) + "\n")

    legend = (
        f"<strong>{args.title}</strong><br>"
        f"vector tiles, zoom {args.base_zoom}&ndash;{args.max_zoom} "
        f"(overzoomed above) &middot; {parents:,} base tiles<br>"
        f"{west:.4f},{south:.4f} &rarr; {east:.4f},{north:.4f}"
    )
    html = VIEWER
    for key, value in {
        "__TITLE__": args.title,
        "__LEGEND__": legend,
        "__OSM_TILES__": OSM_TILES,
        "__LAT__": f"{lat:.6f}",
        "__LON__": f"{lon:.6f}",
        "__BASE_ZOOM__": str(args.base_zoom),
        "__MAX_ZOOM__": str(args.max_zoom),
        "__DEFAULT_ZOOM__": str(max(args.base_zoom, args.max_zoom - 5)),
    }.items():
        html = html.replace(key, value)
    (args.out_dir / "index.html").write_text(html)

    print(
        f"style.json, sprite.png, metadata.json and index.html"
        f" for zoom {args.base_zoom}-{args.max_zoom}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
