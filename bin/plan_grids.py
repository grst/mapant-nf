#!/usr/bin/env python3
"""
Turn a CSV of LiDAR tiles into a processing plan: which tiles are rendered together, which are
only there to supply neighbouring points, and which web-mercator tile each render feeds.

Everything geometric in this pipeline lives here, on purpose. Nextflow's strict (v2) parser makes
non-trivial logic in a workflow body awkward, and the interesting decisions below -- how wide the
halo has to be, how to get a gap-free lon/lat envelope out of a UTM box -- are exactly the ones
worth unit-testing. See tests/test_plan_grids.py.

The one number that drives the whole design
-------------------------------------------
karttapullautin's batch mode (src/process.rs::batch_process, v2.13.0) renders each tile from the
points of *every* laz file in its input folder that overlaps the tile's own bounding box expanded
by 127 m, then crops the result back to the tile. So:

  * A tile rendered with all neighbours within 127 m present is bit-identical to the same tile
    rendered as part of any larger batch. There is no "edge artifact" left to trade against
    batch size.
  * With 1 km tiles that means a single ring of neighbours suffices, and `grid_size` is purely a
    download/IO amortisation knob: bigger grids re-download proportionally fewer ring tiles.

That is why the halo here is computed as a distance (--halo-m) rather than as a count of rings.

Outputs (all relative to --outdir)
----------------------------------
grids/<grid_id>.csv   one row per laz file the grid needs, role=core|halo
grids.csv             one row per grid: crs, counts, bbox in CRS units and in lon/lat
parent_tiles.csv      long form (tile, parent, z, x, y, crs, n_core) -- the tiling fan-out
osm_chunks/<id>.json  ready-made `osmium extract --config` files, several grids per pass
plan_summary.txt      the numbers you want before starting a multi-terabyte run
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import mercantile
import pyproj
from shapely.geometry import box
from shapely.ops import unary_union
from shapely.strtree import STRtree

WGS84 = "EPSG:4326"

# Web mercator is only defined between these latitudes; mercantile raises outside them.
MERCATOR_LAT_LIMIT = 85.0511287798066

REQUIRED_COLUMNS = (
    "tile",
    "url",
    "size_bytes",
    "sha256",
    "crs",
    "min_x",
    "min_y",
    "max_x",
    "max_y",
)
LONLAT_COLUMNS = ("min_lon", "min_lat", "max_lon", "max_lat")

GRID_CSV_COLUMNS = (
    "tile",
    "url",
    "sha256",
    "size_bytes",
    "role",
    "crs",
    "min_x",
    "min_y",
    "max_x",
    "max_y",
)


class PlanError(Exception):
    """A problem with the input that the user has to fix; reported without a traceback."""


@dataclass(slots=True)
class Tile:
    tile: str
    url: str
    sha256: str
    size_bytes: int
    crs: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    # Filled in by derive_lonlat(); the WGS84 envelope of the projected box.
    min_lon: float = math.nan
    min_lat: float = math.nan
    max_lon: float = math.nan
    max_lat: float = math.nan

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def geometry(self):
        return box(self.min_x, self.min_y, self.max_x, self.max_y)


# ---------------------------------------------------------------------------
# Reading and checking the input
# ---------------------------------------------------------------------------
def read_tiles(path: Path) -> list[Tile]:
    """
    Read the tiles CSV.

    Only the columns in the contract are touched; anything else in the file is ignored, so a
    source-specific extra column costs nothing. (Bavaria's export has a `units` column that
    actually holds the Regierungsbezirk name.)
    """
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise PlanError(f"{path} is empty")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise PlanError(
                f"{path} is missing required column(s): {', '.join(missing)}\n"
                f"found: {', '.join(reader.fieldnames)}\n"
                f"see assets/schema_tiles.json for the contract"
            )
        has_lonlat = all(c in reader.fieldnames for c in LONLAT_COLUMNS)

        tiles: list[Tile] = []
        for lineno, row in enumerate(reader, start=2):
            try:
                t = Tile(
                    tile=row["tile"].strip(),
                    url=row["url"].strip(),
                    sha256=row["sha256"].strip().lower(),
                    size_bytes=int(row["size_bytes"]),
                    crs=row["crs"].strip(),
                    min_x=float(row["min_x"]),
                    min_y=float(row["min_y"]),
                    max_x=float(row["max_x"]),
                    max_y=float(row["max_y"]),
                )
                if has_lonlat:
                    t.min_lon = float(row["min_lon"])
                    t.min_lat = float(row["min_lat"])
                    t.max_lon = float(row["max_lon"])
                    t.max_lat = float(row["max_lat"])
            except (TypeError, ValueError) as exc:
                raise PlanError(f"{path}:{lineno}: {exc}") from exc

            # nf-schema checks types and patterns; it cannot express "max beyond min", and a
            # degenerate box would silently produce a grid that renders nothing.
            if t.width <= 0 or t.height <= 0:
                raise PlanError(
                    f"{path}:{lineno}: tile {t.tile!r} has a degenerate bbox "
                    f"({t.min_x},{t.min_y})-({t.max_x},{t.max_y})"
                )
            tiles.append(t)

    if not tiles:
        raise PlanError(f"{path} has a header but no data rows")
    return tiles


def validate_against_schema(path: Path, schema_path: Path) -> None:
    """
    Validate every row against assets/schema_tiles.json.

    This duplicates what nf-schema does in the workflow, and exists for the case where the row
    count makes doing it there too slow: same schema file, so there is only one definition of the
    contract either way. Off by default (--schema is optional).
    """
    import jsonschema  # imported lazily: only needed on this path

    schema = json.loads(schema_path.read_text())
    item_schema = schema.get("items", schema)
    validator = jsonschema.Draft202012Validator(item_schema)

    numeric = {
        f
        for f, spec in item_schema.get("properties", {}).items()
        if spec.get("type") in {"number", "integer"}
    }
    errors = 0
    with path.open(newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            # CSV has no types; coerce the fields the schema declares numeric, and let a
            # non-numeric value be reported by the schema as a type error rather than crashing.
            coerced = {}
            for key, value in row.items():
                if key is None or value is None or value == "":
                    continue
                if key in numeric:
                    try:
                        coerced[key] = int(value) if item_schema["properties"][key]["type"] == "integer" else float(value)
                    except ValueError:
                        coerced[key] = value
                else:
                    coerced[key] = value
            for err in validator.iter_errors(coerced):
                errors += 1
                if errors <= 20:
                    print(f"{path}:{lineno}: {err.message}", file=sys.stderr)
    if errors:
        raise PlanError(f"{errors} row(s) in {path} violate {schema_path}")


def require_single_crs(tiles: list[Tile]) -> str:
    """
    Refuse input that mixes coordinate reference systems.

    Grids are per-CRS by construction -- karttapullautin compares raw header coordinates when it
    decides which files overlap a tile's 127 m box, so mixing UTM zones inside one grid would
    produce garbage rather than an error. Handling a genuinely multi-zone dataset needs a warp
    step (reproject each foreign-CRS render onto the target lattice *before* tiling, because k2t
    writes opaque white for nodata and two runs cannot be alpha-composited). That is not
    implemented, so this fails loudly instead of quietly producing a broken map.
    """
    counts = Counter(t.crs for t in tiles)
    if len(counts) == 1:
        return next(iter(counts))
    detail = "\n".join(f"  {crs}: {n} tiles" for crs, n in counts.most_common())
    raise PlanError(
        "the selected tiles span more than one CRS:\n"
        f"{detail}\n"
        "Per-CRS reprojection is not implemented. Restrict the input to one CRS with "
        "--bbox / --tile-regex, or reproject the sources first."
    )


def derive_lonlat(tiles: list[Tile]) -> None:
    """
    Fill in the WGS84 envelope of each tile, unless the CSV already supplied one.

    `transform_bounds(densify_pts=...)` rather than transforming the four corners: a UTM box's
    edges are curves in lon/lat, so the corner-only envelope is *smaller* than the true one. Tiles
    would then be assigned to fewer web-mercator parents than they actually cover, and the missing
    assignments show up as seams in the finished map. Erring large is free -- k2t re-filters with
    exact geometry -- so densify and take the wider answer.

    Grouped by CRS because this runs before any CRS check: a multi-CRS CSV is only rejected once
    the region filter has had its chance to narrow the selection to a single zone.
    """
    todo = [t for t in tiles if math.isnan(t.min_lon)]
    if not todo:
        return
    by_crs: dict[str, list[Tile]] = defaultdict(list)
    for t in todo:
        by_crs[t.crs].append(t)
    for crs, group in by_crs.items():
        tr = pyproj.Transformer.from_crs(crs, WGS84, always_xy=True)
        for t in group:
            t.min_lon, t.min_lat, t.max_lon, t.max_lat = tr.transform_bounds(
                t.min_x, t.min_y, t.max_x, t.max_y, densify_pts=21
            )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in text.replace(";", ",").split(",")]
    if len(parts) != 4:
        raise PlanError(f"--bbox needs 4 comma-separated numbers, got {text!r}")
    try:
        minx, miny, maxx, maxy = (float(p) for p in parts)
    except ValueError as exc:
        raise PlanError(f"--bbox: {exc}") from exc
    if minx >= maxx or miny >= maxy:
        raise PlanError(f"--bbox is empty or inverted: {text!r}")
    return minx, miny, maxx, maxy


def select_tiles(
    tiles: list[Tile],
    *,
    bbox: tuple[float, float, float, float] | None,
    bbox_crs: str | None,
    tile_regex: str | None,
    min_laz_bytes: int,
) -> tuple[list[Tile], list[Tile], list[Tile]]:
    """
    Apply the region filter. Returns (selected, dropped_by_filter, dropped_as_too_small).

    A tile is selected when its bbox *intersects* the region, so a region that cuts through tiles
    still renders them whole rather than leaving a ragged edge.
    """
    kept = tiles
    dropped: list[Tile] = []

    if tile_regex is not None:
        pattern = re.compile(tile_regex)
        matched = [t for t in kept if pattern.search(t.tile)]
        dropped += [t for t in kept if not pattern.search(t.tile)]
        kept = matched

    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        if bbox_crs and bbox_crs.lower() not in {"lonlat", "wgs84", WGS84.lower()}:
            # bbox given in the tiles' own CRS: compare projected coordinates directly.
            def inside(t: Tile) -> bool:
                return t.min_x < maxx and t.max_x > minx and t.min_y < maxy and t.max_y > miny
        else:
            def inside(t: Tile) -> bool:
                return (
                    t.min_lon < maxx and t.max_lon > minx and t.min_lat < maxy and t.max_lat > miny
                )

        selected = [t for t in kept if inside(t)]
        dropped += [t for t in kept if not inside(t)]
        kept = selected

    # Undersized tiles are dropped from the *core* only. They stay eligible as halo, because a
    # near-empty laz is still a legitimate source of neighbouring points, and excluding it would
    # degrade the renders of its neighbours as well as skipping itself.
    too_small: list[Tile] = []
    if min_laz_bytes > 0:
        too_small = [t for t in kept if t.size_bytes < min_laz_bytes]
        kept = [t for t in kept if t.size_bytes >= min_laz_bytes]

    if not kept:
        raise PlanError(
            "the region filter selected no tiles "
            f"(bbox={bbox}, bbox_crs={bbox_crs}, tile_regex={tile_regex!r}, "
            f"min_laz_bytes={min_laz_bytes})"
        )
    return kept, dropped, too_small


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------
def infer_tile_size(tiles: list[Tile]) -> tuple[float, float, bool]:
    """
    Infer the lattice cell size from the tiles themselves, as (width, height, uniform).

    The modal size, not the mean: a handful of clipped tiles at a dataset's edge must not shift
    the lattice everyone else is placed on.
    """
    widths = Counter(round(t.width, 3) for t in tiles)
    heights = Counter(round(t.height, 3) for t in tiles)
    w = widths.most_common(1)[0][0]
    h = heights.most_common(1)[0][0]
    uniform = len(widths) == 1 and len(heights) == 1
    if w <= 0 or h <= 0:
        raise PlanError(f"inferred a non-positive tile size ({w} x {h})")
    return w, h, uniform


def grid_id_for(crs: str, ix: int, iy: int, grid_size: int) -> str:
    """
    Name a grid from absolute lattice coordinates, never from the dataset's extent.

    If the block index were relative to the first tile in the selection, changing the region
    filter would renumber every grid and invalidate the whole `-resume` cache. Absolute indices
    make a grid's identity a pure function of the tiles in it and `grid_size`.
    """
    epsg = crs.split(":")[-1]
    return f"grid_{epsg}_{ix // grid_size}_{iy // grid_size}"


def build_grids(
    core_tiles: list[Tile],
    halo_pool: list[Tile],
    *,
    crs: str,
    grid_size: int,
    halo_m: float,
) -> dict[str, dict[str, list[Tile]]]:
    """
    Partition `core_tiles` into grids, and attach each grid's halo drawn from `halo_pool`.

    Core assignment is a lattice block; the halo is a distance query, which is what makes this
    work for datasets with holes, ragged edges or non-uniform tile sizes. The halo is the union of
    each core tile's own 127 m box, which is exactly the set of files karttapullautin will read
    -- mitred joins because pullauta expands a *rectangle* (minx-127 .. maxx+127), so a square
    corner, not a rounded one, is the faithful shape.

    `halo_pool` is deliberately the *whole* dataset rather than the region-filtered selection.
    Halo membership decides what points a render can see, so drawing it from the selection would
    make a `--bbox` run produce different pixels for the same tile than a full run does -- the
    filtered run's edge tiles would silently lose their neighbours. Keeping the pool global is
    what makes a small test region a faithful sample of the real thing, and it is what the
    grid-independence test in tests/ relies on.
    """
    tile_w, tile_h, _ = infer_tile_size(core_tiles)

    blocks: dict[str, list[Tile]] = defaultdict(list)
    for t in core_tiles:
        ix = math.floor(t.min_x / tile_w)
        iy = math.floor(t.min_y / tile_h)
        blocks[grid_id_for(crs, ix, iy, grid_size)].append(t)

    tree = STRtree([t.geometry() for t in halo_pool])

    grids: dict[str, dict[str, list[Tile]]] = {}
    for grid_id, core in sorted(blocks.items()):
        core_names = {t.tile for t in core}
        reach = unary_union([t.geometry() for t in core]).buffer(
            halo_m, join_style="mitre", cap_style="square"
        )
        halo = []
        for idx in tree.query(reach):
            cand = halo_pool[idx]
            if cand.tile in core_names:
                continue
            # query() is bbox-based and therefore approximate; confirm a real overlap so a tile
            # that merely touches the halo boundary is not downloaded for nothing. pullauta's own
            # test is a strict inequality, so a zero-area touch contributes no points either.
            if cand.geometry().intersection(reach).area > 0:
                halo.append(cand)
        grids[grid_id] = {
            "core": sorted(core, key=lambda t: (t.min_x, t.min_y)),
            "halo": sorted(halo, key=lambda t: (t.min_x, t.min_y)),
        }
    return grids


# ---------------------------------------------------------------------------
# Web-mercator parents
# ---------------------------------------------------------------------------
def parent_tiles_for(tile: Tile, base_zoom: int) -> list[mercantile.Tile]:
    south = max(tile.min_lat, -MERCATOR_LAT_LIMIT)
    north = min(tile.max_lat, MERCATOR_LAT_LIMIT)
    if south >= north:
        return []
    return list(mercantile.tiles(tile.min_lon, south, tile.max_lon, north, zooms=[base_zoom]))


# ---------------------------------------------------------------------------
# Writing the plan
# ---------------------------------------------------------------------------
def write_grid_csvs(outdir: Path, grids: dict[str, dict[str, list[Tile]]]) -> None:
    gdir = outdir / "grids"
    gdir.mkdir(parents=True, exist_ok=True)
    for grid_id, parts in grids.items():
        with (gdir / f"{grid_id}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(GRID_CSV_COLUMNS)
            for role in ("core", "halo"):
                for t in parts[role]:
                    w.writerow(
                        [
                            t.tile,
                            t.url,
                            t.sha256,
                            t.size_bytes,
                            role,
                            t.crs,
                            f"{t.min_x:.6f}",
                            f"{t.min_y:.6f}",
                            f"{t.max_x:.6f}",
                            f"{t.max_y:.6f}",
                        ]
                    )


def write_grid_index(outdir: Path, grids: dict[str, dict[str, list[Tile]]], crs: str) -> None:
    with (outdir / "grids.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "grid_id", "crs", "n_core", "n_halo", "bytes_total",
                "min_x", "min_y", "max_x", "max_y",
                "min_lon", "min_lat", "max_lon", "max_lat",
            ]
        )
        for grid_id, parts in grids.items():
            everything = parts["core"] + parts["halo"]
            w.writerow(
                [
                    grid_id,
                    crs,
                    len(parts["core"]),
                    len(parts["halo"]),
                    sum(t.size_bytes for t in everything),
                    f"{min(t.min_x for t in everything):.6f}",
                    f"{min(t.min_y for t in everything):.6f}",
                    f"{max(t.max_x for t in everything):.6f}",
                    f"{max(t.max_y for t in everything):.6f}",
                    f"{min(t.min_lon for t in everything):.7f}",
                    f"{min(t.min_lat for t in everything):.7f}",
                    f"{max(t.max_lon for t in everything):.7f}",
                    f"{max(t.max_lat for t in everything):.7f}",
                ]
            )


def write_parent_tiles(
    outdir: Path, grids: dict[str, dict[str, list[Tile]]], *, crs: str, base_zoom: int
) -> tuple[int, int]:
    """
    Write the tile -> web-mercator-parent mapping, and return (n_parents, n_rows).

    The `tile` column holds the tile *stem* -- no .laz suffix -- because that is what
    karttapullautin names its outputs after (`<stem>.png`, `<stem>_depr.pgw`), and the workflow
    joins this table against those filenames.

    `n_core` on every row is the number of core tiles that feed that parent. Nextflow uses it as
    the `groupKey` size, which is what lets tiling of a finished parent start while other grids
    are still rendering -- and, because `groupTuple` still flushes short groups when the upstream
    channel ends, what lets a tile that failed to render leave a hole instead of a deadlock.
    """
    rows: list[tuple[str, str, int, int, int]] = []
    per_parent: Counter[mercantile.Tile] = Counter()
    for parts in grids.values():
        for t in parts["core"]:
            stem = Path(t.tile).stem
            for p in parent_tiles_for(t, base_zoom):
                rows.append((stem, f"{p.z}_{p.x}_{p.y}", p.z, p.x, p.y))
                per_parent[p] += 1

    with (outdir / "parent_tiles.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tile", "parent", "z", "x", "y", "crs", "n_core"])
        for tile_name, parent, z, x, y in sorted(rows, key=lambda r: (r[2], r[3], r[4], r[0])):
            w.writerow([tile_name, parent, z, x, y, crs, per_parent[mercantile.Tile(x, y, z)]])
    return len(per_parent), len(rows)


def write_osm_chunks(
    outdir: Path,
    grids: dict[str, dict[str, list[Tile]]],
    *,
    crs: str,
    buffer_m: float,
    chunk_size: int,
) -> int:
    """
    Write `osmium extract --config` files, several grids per file.

    One osmium pass costs a full read of the source pbf (809 MB for Bavaria), so extracting per
    grid would re-read it once per grid. osmium can cut many extracts in a single pass, which is
    what these chunks are for. The chunk size stays modest because `--strategy=smart` keeps a
    per-extract id set in memory.

    The bbox is buffered in the projected CRS and only then converted to lon/lat, so the buffer is
    a real distance rather than a number of degrees that would shrink with latitude.
    """
    cdir = outdir / "osm_chunks"
    cdir.mkdir(parents=True, exist_ok=True)
    tr = pyproj.Transformer.from_crs(crs, WGS84, always_xy=True)

    items = sorted(grids.items())
    n_chunks = 0
    for start in range(0, len(items), chunk_size):
        chunk = items[start : start + chunk_size]
        n_chunks += 1
        extracts = []
        for grid_id, parts in chunk:
            everything = parts["core"] + parts["halo"]
            left, bottom, right, top = tr.transform_bounds(
                min(t.min_x for t in everything) - buffer_m,
                min(t.min_y for t in everything) - buffer_m,
                max(t.max_x for t in everything) + buffer_m,
                max(t.max_y for t in everything) + buffer_m,
                densify_pts=21,
            )
            extracts.append(
                {
                    "description": grid_id,
                    "output": f"{grid_id}.pbf",
                    "output_format": "pbf",
                    "bbox": [round(left, 7), round(bottom, 7), round(right, 7), round(top, 7)],
                }
            )
        payload = {"directory": "extracts", "extracts": extracts}
        (cdir / f"chunk_{n_chunks:05d}.json").write_text(json.dumps(payload, indent=2) + "\n")
    return n_chunks


def write_summary(outdir: Path, lines: list[str]) -> None:
    (outdir / "plan_summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tiles-csv", type=Path, required=True)
    ap.add_argument("--outdir", type=Path, default=Path("."))
    ap.add_argument(
        "--grid-size",
        type=int,
        required=True,
        help="core tiles per grid edge; a pure efficiency knob (see module docstring)",
    )
    ap.add_argument(
        "--halo-m",
        type=float,
        default=127.0,
        help="neighbour distance a render needs; 127 is karttapullautin's hard-coded value",
    )
    ap.add_argument("--base-zoom", type=int, default=12)
    ap.add_argument("--osm-buffer-m", type=float, default=2000.0)
    ap.add_argument("--osm-chunk-size", type=int, default=32)
    ap.add_argument("--bbox", help="minx,miny,maxx,maxy region filter")
    ap.add_argument(
        "--bbox-crs",
        default="lonlat",
        help="'lonlat' (default) or an EPSG string, meaning --bbox is in the tiles' own CRS",
    )
    ap.add_argument("--tile-regex", help="keep only tiles whose name matches")
    ap.add_argument(
        "--min-laz-bytes",
        type=int,
        default=0,
        help="skip tiles smaller than this; near-empty laz files are the likeliest trigger of "
        "the karttapullautin crash this pipeline has to survive",
    )
    ap.add_argument(
        "--schema",
        type=Path,
        help="also validate every row against this JSON schema (assets/schema_tiles.json)",
    )
    args = ap.parse_args(argv)

    if args.grid_size < 1:
        raise PlanError("--grid-size must be >= 1")
    if args.halo_m < 0:
        raise PlanError("--halo-m must be >= 0")

    if args.schema:
        validate_against_schema(args.tiles_csv, args.schema)

    all_tiles = read_tiles(args.tiles_csv)
    derive_lonlat(all_tiles)

    bbox = parse_bbox(args.bbox) if args.bbox else None
    tiles, dropped, too_small = select_tiles(
        all_tiles,
        bbox=bbox,
        bbox_crs=args.bbox_crs,
        tile_regex=args.tile_regex,
        min_laz_bytes=args.min_laz_bytes,
    )
    # Only the *selection* has to be single-CRS. Checking it here rather than on the whole file
    # means a multi-zone national dataset can still be processed one zone at a time, which is what
    # require_single_crs()'s error message tells the user to do.
    crs = require_single_crs(tiles)
    halo_pool = [t for t in all_tiles if t.crs == crs]

    tile_w, tile_h, uniform = infer_tile_size(tiles)
    grids = build_grids(
        tiles, halo_pool, crs=crs, grid_size=args.grid_size, halo_m=args.halo_m
    )

    args.outdir.mkdir(parents=True, exist_ok=True)
    write_grid_csvs(args.outdir, grids)
    write_grid_index(args.outdir, grids, crs)
    n_parents, n_parent_rows = write_parent_tiles(
        args.outdir, grids, crs=crs, base_zoom=args.base_zoom
    )
    n_chunks = write_osm_chunks(
        args.outdir,
        grids,
        crs=crs,
        buffer_m=args.osm_buffer_m,
        chunk_size=args.osm_chunk_size,
    )

    core_bytes = sum(t.size_bytes for t in tiles)
    fetch_bytes = sum(
        t.size_bytes for parts in grids.values() for t in parts["core"] + parts["halo"]
    )
    per_grid_bytes = [
        sum(t.size_bytes for t in parts["core"] + parts["halo"]) for parts in grids.values()
    ]
    # Distinct files touched, i.e. what a perfect cache would fetch. Halo-only tiles outside the
    # selected region are counted here but never rendered.
    distinct = {t.tile: t for parts in grids.values() for t in parts["core"] + parts["halo"]}
    distinct_bytes = sum(t.size_bytes for t in distinct.values())
    halo_only = len(distinct) - len(tiles)

    lines = [
        "mapant plan",
        "===========",
        f"tiles CSV            {args.tiles_csv}",
        f"CRS                  {crs}",
        f"lattice cell         {tile_w:g} x {tile_h:g} {crs} units"
        + ("" if uniform else "  (INFERRED, sizes are not uniform)"),
        f"halo distance        {args.halo_m:g} units "
        f"({args.halo_m / tile_w:.2f} cells wide -- {'1 ring suffices' if args.halo_m <= tile_w else 'MORE THAN ONE RING'})",
        "",
        f"tiles in CSV         {len(all_tiles):,}",
        f"rendered (core)      {len(tiles):,}"
        + (f"  (filter dropped {len(dropped):,})" if dropped else ""),
        f"halo only            {halo_only:,}  (fetched for their points, never rendered; "
        f"drawn from the whole CSV so a filtered region renders identically to a full run)",
    ]
    if too_small:
        lines.append(
            f"skipped as too small {len(too_small):,}  (< {human_bytes(args.min_laz_bytes)}): "
            + ", ".join(t.tile for t in too_small[:10])
            + (" ..." if len(too_small) > 10 else "")
        )
    lines += [
        "",
        f"grids                {len(grids):,}  (grid_size={args.grid_size})",
        f"  core tiles/grid    min {min(len(p['core']) for p in grids.values())}, "
        f"max {max(len(p['core']) for p in grids.values())}, "
        f"mean {len(tiles) / len(grids):.1f}",
        f"  halo tiles/grid    min {min(len(p['halo']) for p in grids.values())}, "
        f"max {max(len(p['halo']) for p in grids.values())}, "
        f"mean {sum(len(p['halo']) for p in grids.values()) / len(grids):.1f}",
        "",
        f"mapped area          {human_bytes(core_bytes)} of laz",
        f"distinct files       {human_bytes(distinct_bytes)} over {len(distinct):,} files",
        f"to download          {human_bytes(fetch_bytes)}  "
        f"(amplification {fetch_bytes / distinct_bytes:.2f}x -- a halo tile is fetched once per "
        f"grid that borders it; raise --grid-size to reduce it)",
        f"peak laz per grid    {human_bytes(max(per_grid_bytes))} "
        f"(mean {human_bytes(sum(per_grid_bytes) / len(per_grid_bytes))})",
        "  + karttapullautin temporaries, roughly 3 GiB per configured process",
        "",
        f"web-mercator parents {n_parents:,} at z{args.base_zoom} "
        f"({n_parent_rows:,} tile->parent assignments)",
        f"osmium passes        {n_chunks} (chunk size {args.osm_chunk_size}, "
        f"buffer {args.osm_buffer_m:g} {crs} units)",
    ]
    write_summary(args.outdir, lines)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PlanError as exc:
        print(f"plan_grids.py: {exc}", file=sys.stderr)
        sys.exit(1)
