#!/usr/bin/env python3
"""
Cut one web-mercator parent tile's vector pyramid from karttapullautin's per-tile GeoJSON.

karttapullautin (`vectorvege=1`, `geojson_wgs84=1`) writes each tile's map as one GeoJSON file per
layer, in WGS84, already in its published form -- contours generalised and broken around the
knolls, cliff dashes chained into cliff lines, vegetation traced into polygons, OSM shapes matched
to their ISOM codes -- and run_pullauta.py bundles them per tile as `<tile>_vec/<tile>_<layer>
.geojson.gz`. Every feature carries `layer` (karttapullautin's class) and `isom` (its symbol).

This script does not open them. It hands every file to tippecanoe as it is, under the layer its
name says, and tells tippecanoe what each zoom may show. The only thing it does with tippecanoe's
output is remove the tiles that belong to another parent.

The layer order below is karttapullautin's own compositing order (`src/render.rs`), because in a
vector tile the drawing order is the layer order: the area fills, the contours, the point and line
symbols, and the OSM line work over everything.

One rule keeps the pyramid seamless, and it matters because it is cut one parent at a time:
**what appears at a zoom is declared, never negotiated.** tippecanoe's size-driven thinning
(`--drop-densest-as-needed` and friends) decides per tile what to leave out, so two neighbouring
parents disagree along their shared edge -- a lake drawn on one side of the line and missing on the
other. It is switched off; instead a feature filter decides from the feature's own `isom` and the
zoom alone, from the table below.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import mercantile

#: A layer that is drawn at every zoom of the pyramid.
ALL_ZOOMS = 99


@dataclass(frozen=True)
class Layer:
    """
    One vector tile layer: a karttapullautin output, and the zooms its features show at.

    `levels` is how many zooms below the deepest one a feature is still drawn -- 0 is the deepest
    zoom only, ALL_ZOOMS every zoom the pyramid has -- by the feature's `isom`, with `default` for
    any code not listed.
    """

    name: str
    default: int = ALL_ZOOMS
    levels: dict[str, int] = field(default_factory=dict)


#: Where each shape is worth drawing, by the ISOM code karttapullautin matched. The network a
#: runner navigates by -- roads, tracks, railways, streams, lakes -- is on every zoom; the rest is
#: detail that only reads close up. `T` is karttapullautin's bridge/tunnel variant of a code.
OSM_LEVELS: dict[str, int] = {
    "306": ALL_ZOOMS,  # watercourse
    "301": ALL_ZOOMS,  # lake
    "301.1": ALL_ZOOMS,  # lake bank line
    "502": ALL_ZOOMS,  # wide road
    "502T": ALL_ZOOMS,
    "503": ALL_ZOOMS,  # large road
    "503T": ALL_ZOOMS,
    "504": ALL_ZOOMS,  # road
    "504T": ALL_ZOOMS,
    "505": ALL_ZOOMS,  # vehicle track
    "505T": ALL_ZOOMS,
    "515": ALL_ZOOMS,  # railway
    "401": ALL_ZOOMS,  # open land
    "310": ALL_ZOOMS,  # marsh
    "527": ALL_ZOOMS,  # settlement
    "529": 1,  # paved area
    "529.1": 1,
    "507": 1,  # small path
    "507T": 1,
    "526": 1,  # building
    "524": 0,  # fence
    "516": 0,  # power line
    "414": 0,  # black line
}

#: Bottom to top. The names are karttapullautin's output names, so a layer in a tile is exactly
#: one kind of file it wrote.
LAYERS: tuple[Layer, ...] = (
    Layer("yellow"),
    Layer("vegetation"),
    Layer("undergrowth", default=2),
    Layer("osm_areas", default=1, levels=OSM_LEVELS),
    # The index contours carry the shape of the ground and belong on every zoom; the plain ones
    # only stop being a brown wash once a tile covers about a kilometre.
    Layer("contours", default=1, levels={"102": ALL_ZOOMS}),
    Layer("formlines", default=0),
    Layer("dotknolls", default=0),
    Layer("cliffs", default=1),
    Layer("osm_lines", default=1, levels=OSM_LEVELS),
)


@dataclass(frozen=True)
class ZoomPlan:
    """The zooms this parent is cut to."""

    base: int
    max: int

    def minzoom(self, levels: int) -> int:
        """The zoom a feature that survives `levels` zoom-outs first appears at."""
        return max(self.base, self.max - levels)

    def feature_filter(self, layer: Layer) -> list | None:
        """
        A tippecanoe feature filter drawing each of the layer's features from its own minzoom, or
        None when every feature is drawn at every zoom.
        """
        default = self.minzoom(layer.default)
        by_zoom: dict[int, list[str]] = {}
        for code, levels in sorted(layer.levels.items()):
            by_zoom.setdefault(self.minzoom(levels), []).append(code)
        if default <= self.base and all(z <= self.base for z in by_zoom):
            return None

        def from_zoom(zoom: int, condition: list | None) -> list:
            clauses = [c for c in (condition, [">=", "$zoom", zoom] if zoom > self.base else None) if c]
            if not clauses:
                return ["all"]
            return clauses[0] if len(clauses) == 1 else ["all", *clauses]

        listed = sorted(layer.levels)
        branches = [from_zoom(z, ["in", "isom", *codes]) for z, codes in sorted(by_zoom.items())]
        branches.append(from_zoom(default, ["!in", "isom", *listed] if listed else None))
        return branches[0] if len(branches) == 1 else ["any", *branches]


def layer_files(in_dir: Path) -> list[tuple[str, Path]]:
    """Every (layer, file) under the bundles in `in_dir`, in layer order."""
    bundles = sorted(p for p in in_dir.glob("*_vec") if p.is_dir())
    found = []
    for layer in LAYERS:
        for bundle in bundles:
            stem = bundle.name.removesuffix("_vec")
            for suffix in (".geojson.gz", ".geojson"):
                path = bundle / f"{stem}_{layer.name}{suffix}"
                # an empty file is the stub run's placeholder, and nothing tippecanoe can parse
                if path.is_file() and path.stat().st_size > 0:
                    found.append((layer.name, path))
                    break
    return found


def tippecanoe_command(
    files: list[tuple[str, Path]],
    out_dir: Path,
    parent: mercantile.Tile,
    max_zoom: int,
    buffer: int,
) -> list[str]:
    """The tippecanoe invocation that cuts every zoom of the parent from the layer files."""
    plan = ZoomPlan(base=parent.z, max=max_zoom)
    filters = {layer.name: f for layer in LAYERS if (f := plan.feature_filter(layer)) is not None}
    bounds = mercantile.bounds(parent)
    command = [
        "tippecanoe",
        "--force",
        f"--output-to-directory={out_dir}",
        f"--minimum-zoom={parent.z}",
        f"--maximum-zoom={max_zoom}",
        # Uncompressed: the pyramid is published as plain files for a static host, which serves them
        # without a Content-Encoding header, and a renderer will not gunzip what is not announced.
        # Packing into PMTiles later compresses them there, where the reader does know.
        "--no-tile-compression",
        # Only this parent's own area, so the subtrees of two tasks never overlap.
        f"--clip-bounding-box={bounds.west},{bounds.south},{bounds.east},{bounds.north}",
        f"--buffer={buffer}",
        # Nothing is left out to fit a budget. Every one of these decisions is made per tile from
        # what happens to be in it, so two parents cutting the same zoom disagree about what the
        # map contains -- which is visible as a seam along their shared edge. What each zoom shows
        # is decided by the feature filter instead, which depends only on the feature.
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--drop-rate=1",
        # Two shades of green share their boundary vertex for vertex (karttapullautin traces them
        # from one grid). Simplifying each polygon on its own would move that boundary twice and
        # open a sliver of white paper between them; these keep it one line.
        "--detect-shared-borders",
        "--no-simplification-of-shared-nodes",
        "--attribute-type=elevation:float",
        "--attribute-type=shade:int",
        "--no-tile-stats",
        # One line of progress per tile would be thousands of lines in the task log.
        "--no-progress-indicator",
    ]
    if filters:
        command.append(f"--feature-filter={json.dumps(filters, separators=(',', ':'))}")
    for layer, path in files:
        command.append(f"--named-layer={layer}:{path}")
    return command


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
    ap.add_argument("--max-zoom", type=int, required=True)
    ap.add_argument("--buffer", type=int, default=8, help="tile buffer in 1/256 of a tile")
    args = ap.parse_args(argv)

    z, x, y = args.parent
    parent = mercantile.Tile(x=x, y=y, z=z)
    if args.max_zoom < z:
        print(f"make_vector_tiles.py: --max-zoom {args.max_zoom} is below the parent zoom {z}",
              file=sys.stderr)
        return 1

    files = layer_files(args.in_dir)
    if not files:
        print("no vector files here; nothing to cut")
        return 0
    counts: dict[str, int] = {}
    for layer, _ in files:
        counts[layer] = counts.get(layer, 0) + 1
    print(f"{len(files)} file(s) -> {z}/{x}/{y} .. z{args.max_zoom}: "
          + ", ".join(f"{layer} x{n}" for layer, n in counts.items()))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    command = tippecanoe_command(files, args.out_dir, parent, args.max_zoom, args.buffer)
    print("  " + " ".join(command[:6]) + " ...", flush=True)
    subprocess.run(command, check=True)
    kept, pruned = prune_foreign_tiles(args.out_dir, parent)
    print(f"{kept} tile(s) written, {pruned} outside {z}/{x}/{y} pruned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
