#!/usr/bin/env python3
"""
Cut one web-mercator parent tile's vector pyramid from karttapullautin's per-tile GeoJSON.

karttapullautin (`vectorvege=1`, `geojson_wgs84=1`) writes each tile's map as one GeoJSON file per
layer, in WGS84, already in its published form -- contours generalised and broken around the
knolls, cliff dashes, vegetation traced into polygons, OSM shapes matched to their ISOM codes -- and
run_pullauta.py bundles them per tile as `<tile>_vec/<tile>_<layer>.geojson.gz`. Every feature
carries `layer` (karttapullautin's class) and `isom` (its symbol).

The tiles follow the schema of isom-maplibre (https://github.com/MetsaApp/isom-maplibre), so that
its style draws them as they are:

* **One layer per table** -- `contours`, `cliffs`, `knolls_points`, `vegetation_areas`, `water`,
  `paths`, `manmade` -- chosen by the symbol, not by the file it came in: a lake from the OSM shapes
  and a stream are both `water`.
* **`isom_code`**, the ISOM 2017-2 symbol as the style spells it (`"403.000"`, `"101.001"`). The
  terrain is numbered in ISOM 2017-2 already; the OSM shapes carry the ISOM 2000 codes of the run's
  rules file, translated by OpenOrienteering Mapper's crosswalk (assets/isom2000-isom2017-2.crt),
  so a region with rules of its own still maps. `layer` and `isom` stay, for consumers that read
  them (mapant-bayern's OCAD export).
* **`coverage`**: the footprint of every tile that was rendered, the white paper under the map.

Each feature's zooms are decided here, from the feature alone, and written as tippecanoe's per
feature `minzoom`. One rule keeps the pyramid seamless, and it matters because it is cut one parent
at a time: **what appears at a zoom is declared, never negotiated.** tippecanoe's size-driven
thinning (`--drop-densest-as-needed` and friends) decides per tile what to leave out, so two
neighbouring parents disagree along their shared edge -- a lake drawn on one side of the line and
missing on the other. It is switched off; the table below decides instead.

Zooms are MapLibre's: 512 px tiles. The deepest zoom is cut at an extent of 8192, so it holds what
four 256 px tiles one zoom deeper would. One zoom above the parent's is an overview level with no
contour lines at all; each of the four parents under an overview tile cuts its own quarter, and
MERGE_PMTILES joins them.

Where a feature first appears is counted from the deepest zoom: 0 is the deepest zoom only, -1 from
one zoom shallower, and so on. The shallow zooms show the map as it reads at a glance: roads but not
tracks, and a vegetation patch only from the zoom it covers `--min-area-px` pixels at.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import subprocess
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Hashable
from dataclasses import dataclass, field
from pathlib import Path

import mercantile
import pyproj

#: Drawn at every zoom of the pyramid.
ALL_ZOOMS = -99

#: Where every vegetation patch is shown, whatever its size (see SmallAreas).
SMALL_AREAS_FROM = -1


@dataclass(frozen=True)
class Layer:
    """
    One of karttapullautin's outputs, and the zooms its features show at.

    `shown_from` is the zoom a feature first appears at, counted from the deepest one -- 0 is the
    deepest zoom only, -1 from one zoom shallower, ALL_ZOOMS every zoom the pyramid has -- by the
    feature's `isom`, with `default` for any code not listed.
    """

    name: str
    default: int = ALL_ZOOMS
    shown_from: dict[str, int] = field(default_factory=dict)

    @property
    def osm(self) -> bool:
        """The OSM shapes, numbered in ISOM 2000 by the rules file; the rest is ISOM 2017-2."""
        return self.name.startswith("osm_")

    def shown_from_of(self, isom: str) -> int:
        return self.shown_from.get(isom, self.default)


#: Where each shape is worth drawing, by the ISOM code karttapullautin matched. The network a
#: runner navigates by -- roads, railways, streams, lakes -- is on every zoom or nearly, the tracks
#: from a zoom deeper than the small roads; the rest is detail that only reads close up. `T` is
#: karttapullautin's bridge/tunnel variant of a code.
OSM_SHOWN_FROM: dict[str, int] = {
    "306": ALL_ZOOMS,  # watercourse
    "301": ALL_ZOOMS,  # lake
    "301.1": ALL_ZOOMS,  # lake bank line (karttapullautin's outline of a 301 area)
    "502": ALL_ZOOMS,  # wide road
    "502T": ALL_ZOOMS,
    "503": ALL_ZOOMS,  # large road
    "503T": ALL_ZOOMS,
    "504": -4,  # road
    "504T": -4,
    "505": -3,  # vehicle track
    "505T": -3,
    "515": ALL_ZOOMS,  # railway
    "401": ALL_ZOOMS,  # open land
    "401.1": ALL_ZOOMS,  # its edge
    "310": ALL_ZOOMS,  # marsh
    "527": ALL_ZOOMS,  # settlement
    "529": -1,  # paved area
    "529.1": -1,
    "507": -1,  # small path
    "507T": -1,
    "526": -1,  # building
    "524": 0,  # fence
    "516": 0,  # power line
    "414": 0,  # black line
}

#: karttapullautin's outputs, by the name in their file names.
LAYERS: tuple[Layer, ...] = (
    Layer("yellow"),
    Layer("vegetation"),
    Layer("undergrowth", default=-2),
    Layer("osm_areas", default=-1, shown_from=OSM_SHOWN_FROM),
    # The index contours carry the shape of the ground and belong on every zoom but the overview;
    # the plain ones only stop being a brown wash once a tile covers about a kilometre.
    Layer("contours", default=-1, shown_from={"102": ALL_ZOOMS}),
    Layer("formlines", default=0),
    Layer("dotknolls", default=0),
    Layer("cliffs", default=-1),
    Layer("osm_lines", default=-1, shown_from=OSM_SHOWN_FROM),
)

#: The tables, in the order they are written: the order a style that follows the tile draws them
#: in, area fills first. `coverage` is the paper under everything.
TABLES = (
    "coverage",
    "vegetation_areas",
    "water",
    "manmade",
    "contours",
    "cliffs",
    "knolls_points",
    "paths",
)

#: Tables the overview level leaves out: lines that would only be a brown wash at that scale.
NOT_IN_OVERVIEW = frozenset({"contours"})

#: ISOM 2017-2 variants that isom-maplibre draws as their main symbol, or numbers otherwise.
STYLE_CODES = {
    "101.1": "101.001",  # slope line
    "301.1": "301.000",  # uncrossable body of water, full colour
    "301.4": "301.000",  # its bank line
    "501.1": "501.000",  # paved area
    "502.1": "502.000",  # wide road, double track
}

#: karttapullautin's own codes that no ISOM 2000 table has: the outline it strokes round an area
#: is `<code>.1`. A field's edge is drawn as a distinct cultivation boundary.
KP_CODES = {"401.1": "415"}


@dataclass(frozen=True)
class ZoomPlan:
    """The zooms this parent is cut to: an overview level above `base`, then `base`..`max`."""

    base: int
    max: int

    @property
    def overview(self) -> int:
        return max(0, self.base - 1)

    def minzoom(self, shown_from: int, table: str) -> int:
        """The zoom a feature first appears at, from `shown_from` counted from the deepest zoom."""
        shallowest = self.base if table in NOT_IN_OVERVIEW else self.overview
        return min(self.max, max(shallowest, self.max + shown_from))


def read_crosswalk(path: Path) -> dict[str, str]:
    """ISOM 2000 code -> ISOM 2017-2 code, from a Mapper .crt (`<new> <old>` per line)."""
    table: dict[str, str] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and not line.startswith("#"):
            new, old = parts
            table.setdefault(old, new)
    return table


def style_code(code: str) -> str:
    """An ISOM 2017-2 code as isom-maplibre spells it: `NNN.NNN`, the variant in the decimals."""
    if code in STYLE_CODES:
        return STYLE_CODES[code]
    major, _, variant = code.partition(".")
    if not major.isdigit() or not (variant == "" or variant.isdigit()):
        return code
    return f"{int(major):03d}.{int(variant or 0):03d}"


def table_for(code: str, geometry_type: str) -> str:
    """The table a symbol belongs in, from its ISOM 2017-2 number."""
    major = code.split(".")[0]
    number = int(major) if major.isdigit() else 0
    if number < 200:
        return "knolls_points" if geometry_type in ("Point", "MultiPoint") else "contours"
    if number < 300:
        return "cliffs"
    if number < 400:
        return "water"
    if number < 500:
        return "vegetation_areas"
    if 502 <= number <= 508:
        return "paths"
    return "manmade"


class Classifier:
    """isom_code and table of each feature, from the file it came in and its own `isom`."""

    def __init__(self, crosswalk: dict[str, str]) -> None:
        self.crosswalk = crosswalk
        self.unknown: Counter[str] = Counter()

    def iso2017(self, layer: Layer, isom: str) -> str:
        if not layer.osm:
            return isom
        base = isom.removesuffix("T")
        code = KP_CODES.get(base) or self.crosswalk.get(base)
        if code is None:
            # A rules file is free to use a code nobody translated. The feature keeps its own
            # number rather than vanishing, and the log says which.
            self.unknown[base] += 1
            return base
        return code

    def classify(self, layer: Layer, feature: dict) -> tuple[str, str]:
        isom = str(feature.get("properties", {}).get("isom", ""))
        code = self.iso2017(layer, isom)
        return table_for(code, feature["geometry"]["type"]), style_code(code)


def bundle_files(in_dir: Path) -> list[tuple[str, Layer, Path]]:
    """Every (tile, layer, file) under the bundles in `in_dir`."""
    bundles = sorted(p for p in in_dir.glob("*_vec") if p.is_dir())
    found = []
    for bundle in bundles:
        stem = bundle.name.removesuffix("_vec")
        for layer in LAYERS:
            for suffix in (".geojson.gz", ".geojson"):
                path = bundle / f"{stem}_{layer.name}{suffix}"
                # an empty file is the stub run's placeholder, and nothing that parses
                if path.is_file() and path.stat().st_size > 0:
                    found.append((stem, layer, path))
                    break
    return found


def read_collection(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as fh:
        return json.load(fh).get("features") or []


Box = tuple[str, float, float, float, float]


def tile_boxes(parent_tiles: Path, stems: set[str]) -> dict[str, Box]:
    """Each rendered tile's CRS and square in it, from the plan."""
    boxes: dict[str, Box] = {}
    with parent_tiles.open(newline="") as fh:
        for row in csv.DictReader(fh):
            if row["tile"] in stems:
                boxes[row["tile"]] = (row["crs"], *(float(row[k]) for k in ("min_x", "min_y", "max_x", "max_y")))
    return boxes


def footprints(parent_tiles: Path, stems: set[str]) -> list[dict]:
    """
    The rendered tiles' squares in WGS84, as `coverage` features. Densified, because a UTM square's
    edges are curves in longitude/latitude; two neighbours share their edge point for point.
    """
    boxes = tile_boxes(parent_tiles, stems)
    features = []
    transformers: dict[str, pyproj.Transformer] = {}
    steps = 20
    for stem in sorted(boxes):
        crs, x0, y0, x1, y1 = boxes[stem]
        tr = transformers.setdefault(crs, pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True))
        ring = (
            [(x0 + (x1 - x0) * i / steps, y0) for i in range(steps)]
            + [(x1, y0 + (y1 - y0) * i / steps) for i in range(steps)]
            + [(x1 - (x1 - x0) * i / steps, y1) for i in range(steps)]
            + [(x0, y1 - (y1 - y0) * i / steps) for i in range(steps)]
        )
        lon, lat = tr.transform([p[0] for p in ring], [p[1] for p in ring])
        coords = [[round(a, 7), round(b, 7)] for a, b in zip(lon, lat)]
        coords.append(coords[0])
        features.append(
            {
                "type": "Feature",
                "properties": {"tile": stem},
                "geometry": {"type": "Polygon", "coordinates": [coords]},
            }
        )
    return features


def polygons(geometry: dict) -> list:
    """The polygons of a Polygon or MultiPolygon, as lists of rings; nothing for other types."""
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


#: karttapullautin's outputs that hold vegetation areas, read twice: once to measure the patches.
AREA_LAYERS = frozenset({"yellow", "vegetation", "undergrowth", "osm_areas"})

#: A tile edge in a tile's CRS: (crs, "x" or "y", the coordinate), e.g. the line x = 594000.
Line = tuple[str, str, float]


class SmallAreas:
    """
    The zoom a vegetation patch first appears at: the shallowest one it covers `min_px` pixels of,
    holes taken out. The same size on screen at every zoom is a quarter of the ground area one zoom
    deeper, so a zoom out drops more: with 16 px at 47 deg N, patches under ~4.3 ha at z10, ~1.1 ha
    at z11, ~0.27 ha at z12 and ~0.07 ha at z13. Every patch is shown from SMALL_AREAS_FROM.

    A patch is measured whole, not per tile. karttapullautin crops each tile's polygons on the
    tile's edge, so a forest across four tiles comes as four pieces, each cut along a stretch of a
    shared edge. Pieces of one symbol whose cuts overlap on the same edge are one patch, and their
    areas add up; judging each piece alone would bite notches out of the forest along every tile
    edge. A patch cut on an edge with no tile of this parent behind it may go on where it cannot be
    seen, so it is never judged small.

    Two passes: `add` every polygon, then `first_zoom` for each.
    """

    #: How close to its tile's edge a vertex counts as on it; karttapullautin crops on the edge,
    #: and the output is rounded to 1e-7 deg, about a centimetre.
    EDGE_M = 0.5
    #: How far two cuts on the same edge may miss each other and still join: the neighbours trace
    #: their rasters independently, so a boundary crosses the edge at about, not exactly, one point.
    JOIN_M = 2.0

    def __init__(self, min_px: float, zoom: int, boxes: dict[str, Box]):
        self.min_px = min_px
        self.zoom = zoom
        self.world = 512 * 2**zoom
        self.boxes = boxes
        self.transformers: dict[str, pyproj.Transformer] = {}
        self.inner: dict[str, tuple[float, float, float, float]] = {}
        self.area: dict[Hashable, float] = {}
        self.cuts: dict[tuple[Line, str], list[tuple[float, float, str, Hashable]]] = defaultdict(list)
        self.parent: dict[Hashable, Hashable] = {}
        self.unseen: set[Hashable] = set()  # may go on where this parent cannot see
        self.resolved: dict[Hashable, int | None] | None = None

    def area_px(self, geometry: dict) -> float:
        """The area in pixels of the parent's zoom; each zoom deeper has four times as many."""
        def ring_area(ring: list) -> float:
            xs, ys = [], []
            for lon, lat, *_ in ring:
                xs.append((lon + 180) / 360 * self.world)
                s = math.sin(math.radians(lat))
                ys.append((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * self.world)
            return abs(sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i] for i in range(len(xs) - 1))) / 2

        return sum(ring_area(rings[0]) - sum(ring_area(h) for h in rings[1:])
                   for rings in polygons(geometry) if rings)

    def inner_lonlat(self, stem: str) -> tuple[float, float, float, float]:
        """A lon/lat box inside the tile, 10 m clear of its edges: nothing in it is cut."""
        if stem not in self.inner:
            crs, x0, y0, x1, y1 = self.boxes[stem]
            to_wgs84 = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            lon, lat = to_wgs84.transform([x0 + 10, x1 - 10, x0 + 10, x1 - 10], [y0 + 10, y0 + 10, y1 - 10, y1 - 10])
            self.inner[stem] = (max(lon[0], lon[2]), max(lat[0], lat[1]), min(lon[1], lon[3]), min(lat[2], lat[3]))
        return self.inner[stem]

    def edge_cuts(self, stem: str, geometry: dict) -> list[tuple[Line, float, float]]:
        """The stretches of its tile's edge the polygon was cut along."""
        w, s, e_, n = self.inner_lonlat(stem)
        if all(w < p[0] < e_ and s < p[1] < n for rings in polygons(geometry) for p in rings[0]):
            return []
        crs, x0, y0, x1, y1 = self.boxes[stem]
        tr = self.transformers.setdefault(crs, pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True))
        e = self.EDGE_M
        sides = ((("x", x0), 0), (("x", x1), 0), (("y", y0), 1), (("y", y1), 1))
        cuts = []
        for rings in polygons(geometry):
            for ring in rings:
                pts = list(zip(*tr.transform([p[0] for p in ring], [p[1] for p in ring])))
                for a, b in zip(pts, pts[1:]):
                    for (axis, value), i in sides:
                        if abs(a[i] - value) < e and abs(b[i] - value) < e:
                            lo, hi = sorted((a[1 - i], b[1 - i]))
                            cuts.append(((crs, axis, value), lo, hi))
        return cuts

    def add(self, key: Hashable, stem: str, symbol: str, geometry: dict) -> None:
        if self.min_px <= 0 or not polygons(geometry):
            return
        self.area[key] = self.area_px(geometry)
        self.parent[key] = key
        if stem not in self.boxes:
            self.unseen.add(key)  # a tile without a footprint
            return
        for line, lo, hi in self.edge_cuts(stem, geometry):
            self.cuts[(line, symbol)].append((lo, hi, stem, key))

    def find(self, key: Hashable) -> Hashable:
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: Hashable, b: Hashable) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def shared_edges(self) -> dict[Line, list[tuple[float, float]]]:
        """Per edge line, the stretches with a tile of this parent on both sides."""
        stretches: dict[Line, list[tuple[float, float, str]]] = defaultdict(list)
        for stem, (crs, x0, y0, x1, y1) in self.boxes.items():
            stretches[(crs, "x", x0)].append((y0, y1, stem))
            stretches[(crs, "x", x1)].append((y0, y1, stem))
            stretches[(crs, "y", y0)].append((x0, x1, stem))
            stretches[(crs, "y", y1)].append((x0, x1, stem))
        shared: dict[Line, list[tuple[float, float]]] = defaultdict(list)
        for line, spans in stretches.items():
            for i, (lo, hi, a) in enumerate(spans):
                for lo2, hi2, b in spans[i + 1:]:
                    if a != b and min(hi, hi2) > max(lo, lo2):
                        shared[line].append((max(lo, lo2), min(hi, hi2)))
        return shared

    def resolve(self) -> None:
        shared = self.shared_edges()
        for (line, _symbol), cuts in self.cuts.items():
            cuts.sort()
            active: list[tuple[float, float, str, Hashable]] = []
            for lo, hi, stem, key in cuts:
                if not any(s_lo - self.EDGE_M <= lo and hi <= s_hi + self.EDGE_M for s_lo, s_hi in shared[line]):
                    self.unseen.add(key)  # cut where no tile of this parent continues it
                active = [c for c in active if c[1] + self.JOIN_M >= lo]
                for _lo, _hi, other_stem, other in active:
                    if other_stem != stem:
                        self.union(key, other)
                active.append((lo, hi, stem, key))
        total: dict[Hashable, float] = defaultdict(float)
        judged: dict[Hashable, bool] = {}
        for key, area in self.area.items():
            root = self.find(key)
            total[root] += area
            judged[root] = judged.get(root, True) and key not in self.unseen
        self.resolved = {}
        for key in self.area:
            root = self.find(key)
            zoom = None
            if judged[root] and total[root] > 0:
                zoom = self.zoom + max(-self.zoom, math.ceil(math.log(self.min_px / total[root], 4)))
                if zoom <= self.zoom - 1:  # on the overview anyway
                    zoom = None
            self.resolved[key] = zoom

    def first_zoom(self, key: Hashable) -> int | None:
        """The zoom the patch covers `min_px` pixels from; None if its size does not matter."""
        if self.resolved is None:
            self.resolve()
        return self.resolved.get(key)


def write_tables(
    files: list[tuple[str, Layer, Path]],
    coverage: list[dict],
    plan: ZoomPlan,
    classifier: Classifier,
    out_dir: Path,
    small: SmallAreas | None = None,
) -> list[tuple[str, Path, int]]:
    """
    Sort every feature into its table, as newline-delimited GeoJSON with tippecanoe's per-feature
    `minzoom`. Returns (table, file, count) for the tables that have features, in TABLES order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    handles = {}
    counts: Counter[str] = Counter()

    def write(table: str, feature: dict, minzoom: int) -> None:
        if table not in handles:
            handles[table] = (out_dir / f"{table}.geojsonl").open("w")
        feature["tippecanoe"] = {"minzoom": minzoom}
        handles[table].write(json.dumps(feature, separators=(",", ":")) + "\n")
        counts[table] += 1

    if small:
        for stem, layer, path in files:
            if layer.name in AREA_LAYERS:
                for i, feature in enumerate(read_collection(path)):
                    if feature.get("geometry") and classifier.classify(layer, feature)[0] == "vegetation_areas":
                        symbol = f"{layer.name}:{feature.get('properties', {}).get('isom', '')}"
                        small.add((stem, layer.name, i), stem, symbol, feature["geometry"])
    try:
        for feature in coverage:
            write("coverage", feature, plan.overview)
        for stem, layer, path in files:
            for i, feature in enumerate(read_collection(path)):
                if not feature.get("geometry"):
                    continue
                table, code = classifier.classify(layer, feature)
                properties = feature.setdefault("properties", {})
                properties["isom_code"] = code
                shown_from = layer.shown_from_of(str(properties.get("isom", "")))
                minzoom = plan.minzoom(shown_from, table)
                if table == "vegetation_areas" and small:
                    first = small.first_zoom((stem, layer.name, i))
                    if first is not None:
                        minzoom = max(minzoom, min(first, plan.minzoom(SMALL_AREAS_FROM, table)))
                write(table, feature, minzoom)
    finally:
        for fh in handles.values():
            fh.close()
    return [(t, out_dir / f"{t}.geojsonl", counts[t]) for t in TABLES if counts[t]] + [
        (t, out_dir / f"{t}.geojsonl", n) for t, n in counts.items() if t not in TABLES
    ]


def tippecanoe_command(
    tables: list[tuple[str, Path, int]],
    output: Path,
    parent: mercantile.Tile,
    max_zoom: int,
    buffer: int,
) -> list[str]:
    """The tippecanoe invocation that cuts every zoom of the parent from the table files."""
    plan = ZoomPlan(base=parent.z, max=max_zoom)
    bounds = mercantile.bounds(parent)
    command = [
        "tippecanoe",
        "--force",
        f"--output={output}",
        f"--minimum-zoom={plan.overview}",
        f"--maximum-zoom={max_zoom}",
        # 512 px tiles: an extent of 8192 at every zoom, so a tile keeps the precision of the
        # four 4096 tiles a 256 px pyramid would have one zoom deeper.
        "--full-detail=13",
        "--low-detail=13",
        # Only this parent's own area. tippecanoe still writes the tiles just outside it that its
        # buffer reaches into, holding this parent's side of the border; MERGE_PMTILES merges them
        # with the neighbour's copy of the same tile, which holds the other side. The overview
        # tile over four parents is written by each of them, a quarter each, the same way.
        f"--clip-bounding-box={bounds.west},{bounds.south},{bounds.east},{bounds.north}",
        f"--buffer={buffer}",
        # Nothing is left out to fit a budget. Every one of these decisions is made per tile from
        # what happens to be in it, so two parents cutting the same zoom disagree about what the
        # map contains -- which is visible as a seam along their shared edge. What each zoom shows
        # is decided per feature instead (its `minzoom`), which depends only on the feature.
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--drop-rate=1",
        # Two shades of green share their boundary vertex for vertex (karttapullautin traces them
        # from one grid). Simplifying each polygon on its own would move that boundary twice and
        # open a sliver of white paper between them; these keep it one line.
        "--detect-shared-borders",
        "--no-simplification-of-shared-nodes",
        # The deepest zoom is what every deeper view is overzoomed from, so it keeps its geometry
        # as karttapullautin wrote it. Simplified "to one tile unit", it lost the rounded
        # contours' vertices down to ~3 m segments, which show as corners at z17.
        "--simplify-only-low-zooms",
        "--attribute-type=elevation:float",
        "--attribute-type=shade:int",
        "--attribute-type=isom_code:string",
        "--attribute-type=isom:string",
        "--no-tile-stats",
        # One line of progress per tile would be thousands of lines in the task log.
        "--no-progress-indicator",
    ]
    for table, path, _count in tables:
        command.append(f"--named-layer={table}:{path}")
    return command


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("in_dir", type=Path, help="directory holding the <tile>_vec bundles")
    ap.add_argument("output", type=Path, help="the .pmtiles archive to write")
    ap.add_argument("--parent", nargs=3, type=int, required=True, metavar=("Z", "X", "Y"))
    ap.add_argument("--max-zoom", type=int, required=True)
    ap.add_argument("--crosswalk", type=Path, required=True, help="ISOM 2000 -> 2017-2 .crt")
    ap.add_argument("--parent-tiles", type=Path, required=True,
                    help="parent_tiles.csv from plan_grids.py, for the tiles' footprints")
    ap.add_argument("--buffer", type=int, default=4, help="tile buffer in 1/256 of a tile")
    ap.add_argument("--min-area-px", type=float, default=16,
                    help="a vegetation polygon is shown from the zoom it covers this many pixels at, and "
                         "from SMALL_AREAS_FROM whatever its size (0: at every zoom)")
    ap.add_argument("--work-dir", type=Path, default=Path("tables"))
    args = ap.parse_args(argv)

    z, x, y = args.parent
    parent = mercantile.Tile(x=x, y=y, z=z)
    if args.max_zoom < z:
        print(f"make_vector_tiles.py: --max-zoom {args.max_zoom} is below the parent zoom {z}",
              file=sys.stderr)
        return 1

    files = bundle_files(args.in_dir)
    if not files:
        print("no vector files here; nothing to cut")
        return 0
    stems = {stem for stem, _, _ in files}
    classifier = Classifier(read_crosswalk(args.crosswalk))
    plan = ZoomPlan(base=z, max=args.max_zoom)
    small = SmallAreas(args.min_area_px, z, tile_boxes(args.parent_tiles, stems))
    tables = write_tables(files, footprints(args.parent_tiles, stems), plan, classifier, args.work_dir, small)
    print(f"{len(stems)} tile(s), {len(files)} file(s) -> {z}/{x}/{y}, z{plan.overview}..z{args.max_zoom}: "
          + ", ".join(f"{t} {n}" for t, _, n in tables))
    for code, n in sorted(classifier.unknown.items()):
        print(f"  warning: no ISOM 2017-2 code for rules-file code {code} ({n} feature(s)); "
              f"kept as it is", file=sys.stderr)

    command = tippecanoe_command(tables, args.output, parent, args.max_zoom, args.buffer)
    print("  " + " ".join(command[:6]) + " ...", flush=True)
    subprocess.run(command, check=True)
    for _, path, _ in tables:
        path.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
