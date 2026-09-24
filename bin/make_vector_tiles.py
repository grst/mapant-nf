#!/usr/bin/env python3
"""
Cut one web-mercator parent tile's vector pyramid from karttapullautin's per-tile vectors.

The counterpart of MAKE_TILES: same one-task-per-parent shape, same disjoint subtrees that assemble
into one pyramid with no merge step, but the output is Mapbox vector tiles instead of images.

Three kinds of input. Two are produced by karttapullautin (`output_geojson=1`, `vege_bitmode=1`)
and bundled per tile by run_pullauta.py:

  <tile>.geojson.gz   the lines and points it drew -- contours, formlines, cliffs and knolls
  <tile>_*_bit.png    the classified rasters for the layers it has no vectors for at all --
                      vegetation, undergrowth, water and blocks -- which are traced into polygons
                      here, per class, at the 1 m resolution they are drawn at

and the third is the OSM shapes, matched to their ISOM codes by `osm_shapes.py` from the same
archives and the same rules file karttapullautin used to be given. They arrive once for the whole
parent rather than once per square kilometre, so they are clipped here to the ground the render
actually covered -- the union of the tile bounds -- and not to each tile in turn.

The layer order below is karttapullautin's own compositing order (`src/render.rs`), because in a
vector tile the drawing order *is* the layer order, and the OSM layers keep the two places that
order gives them: the area fills under the contours, the line work over everything.

Two rules keep the pyramid seamless, and both matter because it is cut one parent at a time:

* **What appears at a zoom is declared, never negotiated.** tippecanoe's size-driven thinning
  (`--drop-densest-as-needed` and friends) decides per tile what to leave out, so two neighbouring
  parents disagree along their shared edge -- a lake drawn on one side of the line and missing on
  the other. It is switched off here; instead every feature carries the zoom it appears at, from
  the table below. An overview tile shows index contours, vegetation, water and the road network;
  form lines, knolls and buildings only appear once a tile covers little enough ground for them
  to mean something.
* **Area boundaries are shared, not approximated.** karttapullautin's vegetation is a raster of
  class numbers, so two shades of green meet along a pixel edge that belongs to both. Simplifying
  each traced polygon on its own moves that edge twice and leaves a sliver of white paper between
  them, which is exactly what a border between two greens must not do. Generalisation happens on
  the raster instead -- one mode-downsampled grid per zoom -- and what smoothing is left is done
  with shapely's coverage simplifier, which is only allowed to drop vertices, never move them.
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import json
import math
import subprocess
import sys
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import mercantile
import numpy as np
import rasterio.features
import rasterio.transform
import shapely
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Polygon, box, mapping, shape

WGS84 = "EPSG:4326"

#: Metres of ground per screen pixel at zoom 0 on the equator, for 256 px tiles.
EQUATOR_RESOLUTION = 156543.03392804097

#: How much of a raster pixel a traced boundary may be moved by the coverage simplifier. It only
#: takes redundant vertices off a straight run of pixel edges; the shape stays karttapullautin's,
#: and a boundary between two classes stays a single line shared by both.
SIMPLIFY_PIXELS = 0.5

#: A layer that is drawn at every zoom of the pyramid.
ALL_ZOOMS = 99

#: ISOM codes karttapullautin composites *under* the contours (its `low.png`): the area fills. Every
#: other code goes over the top (`high.png`), including lake fills, which is where it puts them.
OSM_LOW_CODES = frozenset({"401", "310", "527", "529"})

#: Classes that are only candidates for form lines. `render.rs` draws them filtered by slope and
#: writes exactly the survivors to `formlines.dxf.bin`, so with formline=2 they are already on the
#: map as the `formlines` layer -- drawing them again unfiltered puts nearly twice as many lines on
#: it. With formline=0 no formlines are generated and these are drawn in full, so they are kept.
INTERMED_CLASSES = frozenset(
    {
        "contour_intermed",
        "contour_index_intermed",
        "depression_intermed",
        "depression_index_intermed",
    }
)


@dataclass(frozen=True)
class Layer:
    """One vector tile layer: where its features come from, what they carry, and when they show."""

    name: str
    kind: str  # line | point | area
    classes: frozenset[str] = frozenset()  # karttapullautin layer names
    raster: str | None = None  # suffix of the classified raster to trace
    osm: str | None = None  # "low" or "high"
    #: Zoom levels below the deepest one at which this layer is still drawn. 0 means the deepest
    #: zoom only, ALL_ZOOMS means every zoom the pyramid has.
    levels: int = ALL_ZOOMS


#: Bottom to top, exactly as `render.rs` composites them: vegetation, undergrowth, the OSM area
#: fills, the curves, dot knolls, blocks, water and black detail, cliffs, and the OSM line work.
LAYERS: tuple[Layer, ...] = (
    Layer("vegetation", "area", raster="_vege_bit"),
    Layer("undergrowth", "area", raster="_undergrowth_bit", levels=2),
    Layer("osm_low", "area", osm="low"),
    Layer(
        "contours",
        "line",
        classes=frozenset(
            {
                "contour",
                "contour_index",
                "depression",
                "depression_index",
                "slope_line",
                "small_depression",
            }
        ),
    ),
    Layer("formlines", "line", classes=frozenset({"formline", "formline_depression"}), levels=0),
    Layer(
        "knolls",
        "point",
        classes=frozenset({"dotknoll", "udepression", "uglydotknoll", "uglyudepression", "1010"}),
        levels=0,
    ),
    Layer("blocks", "area", raster="_blocks_bit", levels=1),
    Layer("water", "area", raster="_water_bit"),
    Layer("cliffs", "line", classes=frozenset({"cliff2", "cliff3", "cliff4"}), levels=0),
    Layer("osm_high", "line", osm="high"),
)

#: Where a curve is worth drawing. The index contours carry the shape of the ground and belong on
#: every zoom; the plain ones only stop being a brown wash once a tile covers about a kilometre;
#: the slope lines and the single-contour depressions are ticks a few metres long.
CLASS_LEVELS: dict[str, int] = {
    "contour": 1,
    "depression": 1,
    "slope_line": 0,
    "small_depression": 0,
}

#: Where each shape is worth drawing, by the ISOM code karttapullautin matched. The network a
#: runner navigates by -- roads, tracks, railways, streams, lakes -- is on every zoom; the rest is
#: detail that only reads close up.
OSM_LEVELS: dict[str, int] = {
    "306": ALL_ZOOMS,  # watercourse
    "301": ALL_ZOOMS,  # lake
    "301.1": ALL_ZOOMS,  # lake bank line
    "503": ALL_ZOOMS,  # large road
    "504": ALL_ZOOMS,  # road
    "505": ALL_ZOOMS,  # vehicle track
    "515": ALL_ZOOMS,  # railway
    "401": ALL_ZOOMS,  # open land
    "310": ALL_ZOOMS,  # marsh
    "527": ALL_ZOOMS,  # settlement
    "529": 1,  # paved area
    "529.1": 1,
    "507": 1,  # small path
    "526": 1,  # building
    "524": 0,  # fence
    "516": 0,  # power line
    "414": 0,  # black line
}
DEFAULT_OSM_LEVELS = 1

#: The cliff hatching is a texture, not a set of features: karttapullautin draws it as well over a
#: hundred thousand two-point ticks per square kilometre of alpine terrain, more than everything
#: else on the map put together. One tick in this many survives each zoom out, by a hash of the tick,
#: so the sample is the same whichever parent happens to cut the tile -- and nested, so a tick
#: drawn on an overview tile is also drawn on every deeper one.
CLIFF_THINNING = 16
CLIFF_LEVELS = 2


@dataclass
class LayerFile:
    """The newline-delimited GeoJSON being accumulated for one layer."""

    layer: Layer
    path: Path
    handle: object
    features: int = 0

    def write(self, geometry: dict, properties: dict, zooms: dict | None = None) -> None:
        feature = {"type": "Feature", "geometry": geometry, "properties": properties}
        if zooms:
            # tippecanoe reads this member off the feature itself, which is what makes the zoom a
            # property of the data rather than of the tile it lands in.
            feature["tippecanoe"] = zooms
        json.dump(feature, self.handle, separators=(",", ":"))
        self.handle.write("\n")
        self.features += 1


def read_formline_mode(ini: Path | None) -> float:
    """
    The `formline` setting of the render, which decides whether the form line candidates are
    already represented by the formlines layer.
    """
    if ini is None or not ini.is_file():
        return 2.0
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string("[pullauta]\n" + ini.read_text())
    try:
        return float(cp["pullauta"].get("formline", "2"))
    except ValueError:
        return 2.0


def layer_for_class(name: str, formline_mode: float) -> str | None:
    """Which vector tile layer a karttapullautin layer name belongs in."""
    if name in INTERMED_CLASSES:
        return None if formline_mode == 2.0 else "contours"
    for layer in LAYERS:
        if name in layer.classes:
            return layer.name
    # `cont` (the basemap contours) is deliberately absent: those are a separate product and are
    # not part of the map that was rendered.
    return None


@dataclass(frozen=True)
class ZoomPlan:
    """The zooms this parent is cut to, and what each of them is allowed to show."""

    base: int
    max: int
    latitude: float

    def minzoom(self, levels: int) -> int:
        """The zoom a feature that survives `levels` zoom-outs first appears at."""
        return max(self.base, self.max - levels)

    def hint(self, levels: int) -> dict | None:
        """The `tippecanoe` member for such a feature, or None when it is drawn everywhere."""
        minzoom = self.minzoom(levels)
        return None if minzoom <= self.base else {"minzoom": minzoom}

    def resolution(self, zoom: int) -> float:
        """Metres of ground per screen pixel at this zoom, at this parent's latitude."""
        return EQUATOR_RESOLUTION * math.cos(math.radians(self.latitude)) / 2**zoom

    def generalisation(self, zoom: int, pixel_size: float) -> int:
        """
        How many raster pixels to merge into one before tracing, for this zoom.

        The deepest zoom keeps the raster as it is: that is the resolution the map was rendered at,
        and the one the OCAD export reads. Above it, a pixel is merged up to about the size of a
        screen pixel, which is the same thing a smaller-scale map does -- generalise rather than
        draw detail nobody can see, and pay for neither the vertices nor the noise.
        """
        if zoom >= self.max:
            return 1
        factor = self.resolution(zoom) / pixel_size
        return 2 ** max(0, round(math.log2(factor))) if factor > 1 else 1


def texture_levels(coordinates: list) -> int:
    """
    How many zoom levels below the deepest one a cliff tick survives.

    Keyed on the tick's own position to a decimetre, so the decision travels with the feature: the
    two parents that share a border take the same sample of the hatching, and a tick that appears
    on an overview tile appears on the deeper ones too.
    """
    first = coordinates[0]
    digest = zlib.crc32(f"{first[0]:.1f},{first[1]:.1f}".encode())
    levels = 0
    while levels < CLIFF_LEVELS and digest % CLIFF_THINNING ** (levels + 1) == 0:
        levels += 1
    return levels


def reproject(geometry: dict, transformer: Transformer) -> dict | None:
    """Reproject a Point, LineString or Polygon from the source CRS to WGS84, rounded to ~1 cm."""
    kind = geometry["type"]
    coords = geometry["coordinates"]

    def points(pairs):
        xs, ys = zip(*[(p[0], p[1]) for p in pairs], strict=False)
        lons, lats = transformer.transform(xs, ys)
        return [[round(lon, 7), round(lat, 7)] for lon, lat in zip(lons, lats, strict=True)]

    if kind == "Point":
        lon, lat = transformer.transform(coords[0], coords[1])
        return {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]}
    if kind == "LineString":
        return {"type": "LineString", "coordinates": points(coords)}
    if kind == "Polygon":
        return {"type": "Polygon", "coordinates": [points(ring) for ring in coords]}
    return None


def route_features(
    bundle: Path,
    files: dict[str, LayerFile],
    transformer: Transformer,
    formline_mode: float,
    plan: ZoomPlan,
):
    """
    Split one tile's GeoJSON into the per-layer files tippecanoe reads.

    Returns the tile's own bounds, which is the ground this square kilometre of the render
    covered; the OSM shapes are cut to the union of them at the end.
    """
    geojson = next(bundle.glob("*.geojson.gz"), None)
    if geojson is None:
        return None

    with gzip.open(geojson, "rt") as fh:
        collection = json.load(fh)

    # The collection's bbox is the tile karttapullautin cropped its own layers to.
    bounds = collection.get("bbox")
    tile_box = box(*bounds) if bounds and len(bounds) == 4 else None

    for feature in collection.get("features", ()):
        geometry = feature.get("geometry")
        properties = feature.get("properties", {})
        if not geometry:
            continue

        klass = properties.get("layer")
        name = layer_for_class(klass, formline_mode) if klass else None
        target = files.get(name) if name else None
        if target is None:
            continue

        out = reproject(geometry, transformer)
        if out is None:
            continue
        props = {"k": klass}
        if properties.get("elevation") is not None:
            props["e"] = properties["elevation"]
        if name == "cliffs":
            levels = texture_levels(geometry["coordinates"])
        else:
            levels = CLASS_LEVELS.get(klass, target.layer.levels)
        target.write(out, props, plan.hint(levels))

    return tile_box


def route_osm(
    path: Path,
    files: dict[str, LayerFile],
    transformer: Transformer,
    plan: ZoomPlan,
    covered,
) -> None:
    """
    Route the matched OSM shapes into the two OSM layers, clipped to the rendered ground.

    `covered` is the union of the bounds of the tiles this parent was cut from. The clip is what
    keeps a road from running out past the LiDAR into ground the map does not describe -- the
    rendered images stop at the same line -- and it is also why a shape that crosses a tile border
    is one feature here rather than one per tile.
    """
    if covered is None or covered.is_empty:
        return

    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            feature = json.loads(line)
            isom = feature["properties"].get("isom")
            geometry = feature.get("geometry")
            if not isom or not geometry:
                continue
            target = files.get("osm_low" if isom in OSM_LOW_CODES else "osm_high")
            if target is None:
                continue
            hint = plan.hint(OSM_LEVELS.get(isom.rstrip("T"), DEFAULT_OSM_LEVELS))
            for clipped in clip_to(geometry, covered):
                out = reproject(clipped, transformer)
                if out is not None:
                    target.write(out, {"isom": isom}, hint)


def clip_to(geometry: dict, region) -> list[dict]:
    """
    Cut a geometry to a region, returning the simple pieces that survive.

    A clipped line can fall apart into several, and a clipped polygon into several polygons, so
    this returns a list; an empty one means the geometry missed the region entirely.
    """
    if region is None:
        return [geometry]

    piece = shape(geometry).intersection(region)
    if piece.is_empty:
        return []

    parts = list(getattr(piece, "geoms", [piece]))
    return [
        mapping(part)
        for part in parts
        if not part.is_empty and part.geom_type in {"Point", "LineString", "Polygon"}
    ]


def read_world_file(pgw: Path) -> tuple[float, float, float, float]:
    """
    A world file's pixel size and the north-west *corner* of its raster.

    The file gives the centre of the north-west pixel, which is half a pixel away from where a
    geotransform starts. Taking it as the corner shifts everything traced from the raster by half
    a metre, and shifts it away from the vectors karttapullautin drew over the same ground.
    """
    numbers = [float(value) for value in pgw.read_text().split()[:6]]
    x_size, _, _, y_size, centre_x, centre_y = numbers
    return x_size, y_size, centre_x - x_size / 2.0, centre_y - y_size / 2.0


def downsample_mode(array: np.ndarray, factor: int) -> np.ndarray:
    """
    Merge every `factor` x `factor` block of classes into the class most of it is.

    Generalising the raster and then tracing it, rather than tracing and then simplifying, is what
    keeps the classes a partition of the ground at every zoom: blocks tile the raster exactly, so
    two classes still meet along one shared edge and nothing opens up between them.
    """
    if factor <= 1:
        return array

    height = -(-array.shape[0] // factor) * factor
    width = -(-array.shape[1] // factor) * factor
    padded = np.zeros((height, width), dtype=array.dtype)
    padded[: array.shape[0], : array.shape[1]] = array

    blocks = padded.reshape(height // factor, factor, width // factor, factor)
    # Descending, so a block split evenly between a class and the paper becomes the class: the map
    # keeps its features as it is generalised rather than dissolving them.
    values = np.unique(array)[::-1]
    counts = np.stack([(blocks == value).sum(axis=(1, 3)) for value in values], axis=-1)
    return values[counts.argmax(axis=-1)].astype(array.dtype)


def traced_areas(
    array: np.ndarray, transform: rasterio.transform.Affine, tolerance: float
) -> Iterator[tuple[int, Polygon]]:
    """
    Polygons per class from one of karttapullautin's classified rasters.

    Every distinct pixel value is a class of its own -- vegetation shade, water against black
    ground detail -- so each becomes a mask and each mask a set of polygons. The result is a
    coverage: the polygons of the different classes tile the ground without overlapping, and the
    simplifier is told so, so the boundary two classes share stays one line.
    """
    values: list[int] = []
    polygons: list[Polygon] = []
    for value in np.unique(array):
        if value == 0:  # background: the paper shows through
            continue
        mask = (array == value).astype(np.uint8)
        for geometry, _ in rasterio.features.shapes(
            mask, mask=mask.astype(bool), transform=transform
        ):
            rings = geometry["coordinates"]
            polygon = Polygon(rings[0], rings[1:])
            if not polygon.is_empty and polygon.is_valid:
                values.append(int(value))
                polygons.append(polygon)

    if tolerance > 0 and polygons and hasattr(shapely, "coverage_simplify"):
        # Not `Polygon.simplify`: that treats each polygon on its own and moves the boundary
        # between two of them twice, leaving white paper in the gap. The coverage simplifier
        # simplifies a shared edge once, for both sides, and only ever drops vertices.
        #
        # It needs shapely 2.1. On an older one the polygons go out with a vertex on every pixel
        # corner, which is bigger but no less correct -- the shapes come off the raster either way,
        # so this is the only thing in here that is an optimisation rather than the map.
        polygons = list(shapely.coverage_simplify(np.array(polygons), tolerance))

    for value, polygon in zip(values, polygons, strict=True):
        if not polygon.is_empty and polygon.geom_type == "Polygon":
            yield value, polygon


def route_rasters(
    bundle: Path, files: dict[str, LayerFile], transformer: Transformer, plan: ZoomPlan
) -> None:
    """Trace this tile's classified rasters into their layers, once per zoom they are drawn at."""
    for layer in LAYERS:
        if layer.raster is None:
            continue
        target = files.get(layer.name)
        if target is None:
            continue
        png = next(bundle.glob(f"*{layer.raster}.png"), None)
        if png is None:
            continue
        pgw = png.with_suffix(".pgw")
        if not pgw.is_file():
            continue

        array = np.array(Image.open(png))
        x_size, y_size, corner_x, corner_y = read_world_file(pgw)

        for zoom in range(plan.minzoom(layer.levels), plan.max + 1):
            factor = plan.generalisation(zoom, abs(x_size))
            transform = rasterio.transform.Affine(
                x_size * factor, 0.0, corner_x, 0.0, y_size * factor, corner_y
            )
            # Everything below the deepest zoom is a generalisation of its own, so it is drawn at
            # that zoom and no other; the deepest one has no ceiling, which is what lets a viewer
            # overzoom past the tiles that were cut.
            zooms = {"minzoom": zoom} if zoom == plan.max else {"minzoom": zoom, "maxzoom": zoom}
            tolerance = SIMPLIFY_PIXELS * abs(x_size) * factor
            for value, polygon in traced_areas(
                downsample_mode(array, factor), transform, tolerance
            ):
                out = reproject(mapping(polygon), transformer)
                if out is not None:
                    target.write(out, {"c": value}, zooms)


def run_tippecanoe(
    files: list[LayerFile],
    out_dir: Path,
    parent: mercantile.Tile,
    max_zoom: int,
    buffer: int,
) -> None:
    """Cut every zoom of the parent from the per-layer files."""
    bounds = mercantile.bounds(parent)
    command = [
        "tippecanoe",
        "--force",
        f"--output-to-directory={out_dir}",
        f"--minimum-zoom={parent.z}",
        f"--maximum-zoom={max_zoom}",
        # The input is newline-delimited, which is what lets tippecanoe read it with every core.
        "--read-parallel",
        # Uncompressed: the pyramid is published as plain files for a static host, which serves them
        # without a Content-Encoding header, and a renderer will not gunzip what is not announced.
        # Packing into PMTiles later compresses them there, where the reader does know.
        "--no-tile-compression",
        # Only this parent's own area, so the subtrees of two tasks never overlap.
        f"--clip-bounding-box={bounds.west},{bounds.south},{bounds.east},{bounds.north}",
        f"--buffer={buffer}",
        # A cliff tick is one two-point line carrying nothing but its class, and there are ~170k of
        # them per square kilometre of alpine terrain, so per-feature overhead is most of the tile:
        # merging the ones that share their attributes into multi-geometries cuts it roughly in
        # half.
        "--coalesce",
        # Nothing is left out to fit a budget. Every one of these decisions is made per tile from
        # what happens to be in it, so two parents cutting the same zoom disagree about what the
        # map contains -- which is visible as a seam along their shared edge, and as content that
        # appears and disappears as you pan. What each zoom shows is decided by the zoom plan
        # above instead, which depends only on the feature.
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--drop-rate=1",
        # A vertex that two features share is a boundary between them; simplifying it away on one
        # side only would open a gap.
        "--no-simplification-of-shared-nodes",
        "--attribute-type=e:float",
        "--no-tile-stats",
        # One line of progress per tile would be thousands of lines in the task log.
        "--no-progress-indicator",
    ]
    for layer_file in files:
        command += [f"--named-layer={layer_file.layer.name}:{layer_file.path}"]

    print("  " + " ".join(command[:8]) + " ...", flush=True)
    subprocess.run(command, check=True)


def prune_foreign_tiles(out_dir: Path, parent: mercantile.Tile) -> tuple[int, int]:
    """
    Delete tiles outside this parent, and the metadata tippecanoe writes per run.

    --clip-bounding-box clips the geometry, but a tile just outside the parent whose buffer reaches
    into it is still written. Left in place, two tasks would publish the same tile path with
    different contents.
    """
    kept = pruned = 0
    for tile in sorted(out_dir.rglob("*.pbf")):
        z, x, y = int(tile.parent.parent.name), int(tile.parent.name), int(tile.stem)
        shift = z - parent.z
        if shift < 0 or (x >> shift, y >> shift) != (parent.x, parent.y):
            tile.unlink()
            pruned += 1
        else:
            kept += 1

    # Per-parent metadata would collide in the published pyramid; VECTOR_VIEWER writes the one that
    # describes the whole run.
    metadata = out_dir / "metadata.json"
    if metadata.is_file():
        metadata.unlink()

    for directory in sorted(out_dir.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    return kept, pruned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("in_dir", type=Path, help="directory holding the <tile>_vec bundles")
    ap.add_argument("out_dir", type=Path, help="where the z/x/y.pbf tree is written")
    ap.add_argument("--parent", nargs=3, type=int, required=True, metavar=("Z", "X", "Y"))
    ap.add_argument("--proj", required=True, help="CRS of the karttapullautin output, e.g. EPSG:25832")
    ap.add_argument("--max-zoom", type=int, required=True)
    ap.add_argument("--ini", type=Path, help="the effective pullauta.ini of the render")
    ap.add_argument(
        "--osm",
        type=Path,
        help="GeoJSONL of ISOM-matched OSM shapes from osm_shapes.py; without it the pyramid is "
        "the LiDAR alone",
    )
    ap.add_argument("--buffer", type=int, default=8, help="tile buffer in 1/256 of a tile")
    ap.add_argument("--work-dir", type=Path, default=Path("vector_layers"))
    args = ap.parse_args(argv)

    z, x, y = args.parent
    parent = mercantile.Tile(x=x, y=y, z=z)
    if args.max_zoom < z:
        print(f"make_vector_tiles.py: --max-zoom {args.max_zoom} is below the parent zoom {z}",
              file=sys.stderr)
        return 1

    bundles = sorted(p for p in args.in_dir.glob("*_vec") if p.is_dir())
    if not bundles:
        print("no vector bundles here; nothing to cut")
        return 0

    if not hasattr(shapely, "coverage_simplify"):
        print(
            f"make_vector_tiles.py: shapely {shapely.__version__} has no coverage simplifier"
            " (2.1 added it); tracing without one, which only costs vertices",
            file=sys.stderr,
        )

    formline_mode = read_formline_mode(args.ini)
    transformer = Transformer.from_crs(args.proj, WGS84, always_xy=True)
    bounds = mercantile.bounds(parent)
    plan = ZoomPlan(base=z, max=args.max_zoom, latitude=(bounds.south + bounds.north) / 2.0)

    args.work_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, LayerFile] = {}
    handles = []
    try:
        for layer in LAYERS:
            path = args.work_dir / f"{layer.name}.geojsonl"
            handle = path.open("w")
            handles.append(handle)
            files[layer.name] = LayerFile(layer=layer, path=path, handle=handle)

        print(f"{len(bundles)} tile(s) -> {z}/{x}/{y} .. z{args.max_zoom}, formline={formline_mode}")
        # The tiles' own bounds, which is the only record of how far the render reached: the OSM
        # extract covers the whole grid plus its buffer, and nothing may be drawn past the LiDAR.
        covered = []
        for bundle in bundles:
            tile_box = route_features(bundle, files, transformer, formline_mode, plan)
            if tile_box is not None:
                covered.append(tile_box)
            route_rasters(bundle, files, transformer, plan)

        if args.osm and args.osm.is_file():
            region = shapely.union_all(covered) if covered else None
            route_osm(args.osm, files, transformer, plan, region)
    finally:
        for handle in handles:
            handle.close()

    populated = [files[layer.name] for layer in LAYERS if files[layer.name].features]
    for layer_file in populated:
        first = plan.minzoom(layer_file.layer.levels)
        print(f"  {layer_file.layer.name:12s} {layer_file.features:8d} features  from z{first}")
    if not populated:
        print("no features in any layer; nothing to cut")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    run_tippecanoe(populated, args.out_dir, parent, args.max_zoom, args.buffer)
    kept, pruned = prune_foreign_tiles(args.out_dir, parent)
    print(f"{kept} tile(s) written, {pruned} outside {z}/{x}/{y} pruned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
