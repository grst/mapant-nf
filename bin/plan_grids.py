#!/usr/bin/env python3
"""
Turn a CSV of LiDAR tiles into a processing plan: which tiles are rendered together, which are
only there to supply neighbouring points, and which web-mercator tile each render feeds.

All of the pipeline's geometry lives here so that it can be unit-tested without Nextflow; see
tests/test_plan_grids.py.

karttapullautin's batch mode (src/process.rs::batch_process, v2.13.0) renders each tile from the
points of every laz file overlapping the tile's bounding box expanded by 127 m, then crops back to
the tile. Tiles are at least 1 km across, so one ring of neighbours always covers that reach, and a
tile rendered with its ring present is byte-identical to the same tile rendered in any larger batch.

Outputs (all relative to --outdir)
----------------------------------
grids/<grid_id>.csv   one row per laz file the grid needs, role=core|halo
grids.csv             one row per grid: crs, counts, bbox
parent_tiles.csv      long form (tile, parent, z, x, y, crs, n_core) -- the tiling fan-out
osm_chunks/<id>.json  ready-made `osmium extract --config` files, several grids per pass
plan_summary.txt      the numbers you want before starting a multi-terabyte run
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import mercantile
import pyproj

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
    # The WGS84 envelope of the projected box, filled in by derive_lonlat().
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


# ---------------------------------------------------------------------------
# Reading and checking the input
# ---------------------------------------------------------------------------
def read_tiles(path: Path) -> list[Tile]:
    """Read the tiles CSV, ignoring any column outside the contract."""
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
            except (TypeError, ValueError) as exc:
                raise PlanError(f"{path}:{lineno}: {exc}") from exc

            # A degenerate box would silently produce a grid that renders nothing, and JSON Schema
            # cannot express "max beyond min".
            if t.width <= 0 or t.height <= 0:
                raise PlanError(
                    f"{path}:{lineno}: tile {t.tile!r} has a degenerate bbox "
                    f"({t.min_x},{t.min_y})-({t.max_x},{t.max_y})"
                )
            tiles.append(t)

    if not tiles:
        raise PlanError(f"{path} has a header but no data rows")
    return tiles


def require_single_crs(tiles: list[Tile]) -> str:
    """
    Refuse input that mixes coordinate reference systems.

    karttapullautin compares raw header coordinates when it decides which files overlap a tile, so
    mixing UTM zones inside one grid produces garbage rather than an error. Supporting a multi-zone
    dataset needs a warp step (each foreign-CRS render reprojected onto the target lattice *before*
    tiling, because k2t writes opaque white for nodata and two runs cannot be alpha-composited);
    that is not implemented, so this fails loudly instead.
    """
    counts = Counter(t.crs for t in tiles)
    if len(counts) == 1:
        return next(iter(counts))
    detail = "\n".join(f"  {crs}: {n} tiles" for crs, n in counts.most_common())
    raise PlanError(
        "the selected tiles span more than one CRS:\n"
        f"{detail}\n"
        "Per-CRS reprojection is not implemented. Restrict the input to one CRS with --bbox, or "
        "reproject the sources first."
    )


def derive_lonlat(tiles: list[Tile]) -> None:
    """
    Fill in each tile's WGS84 envelope.

    `transform_bounds(densify_pts=...)` rather than transforming the four corners: a UTM box's
    edges are curves in lon/lat, so the corner-only envelope is *smaller* than the true one. Tiles
    would then be assigned to fewer web-mercator parents than they cover, which shows up as seams
    in the finished map. Erring large is free -- k2t re-filters with exact geometry.

    Grouped by CRS because this runs before the single-CRS check: a multi-zone national dataset is
    only rejected once the region filter has had its chance to narrow the selection.
    """
    by_crs: dict[str, list[Tile]] = defaultdict(list)
    for t in tiles:
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
) -> tuple[list[Tile], list[Tile]]:
    """
    Apply the region filter. Returns (selected, dropped).

    A tile is selected when its bbox *intersects* the region, so a region that cuts through tiles
    still renders them whole rather than leaving a ragged edge.
    """
    if bbox is None:
        return tiles, []

    minx, miny, maxx, maxy = bbox
    if bbox_crs and bbox_crs.lower() not in {"lonlat", "wgs84", WGS84.lower()}:
        # bbox given in the tiles' own CRS: compare projected coordinates directly.
        def inside(t: Tile) -> bool:
            return t.min_x < maxx and t.max_x > minx and t.min_y < maxy and t.max_y > miny
    else:
        def inside(t: Tile) -> bool:
            return t.min_lon < maxx and t.max_lon > minx and t.min_lat < maxy and t.max_lat > miny

    kept = [t for t in tiles if inside(t)]
    if not kept:
        raise PlanError(f"the region filter selected no tiles (bbox={bbox}, bbox_crs={bbox_crs})")
    return kept, [t for t in tiles if not inside(t)]


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------
def infer_tile_size(tiles: list[Tile]) -> tuple[float, float, bool]:
    """
    Infer the lattice cell size from the tiles themselves, as (width, height, uniform).

    The modal size, not the mean: a handful of clipped tiles at a dataset's edge must not shift the
    lattice everyone else is placed on.
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

    A block index relative to the first selected tile would renumber every grid when the region
    filter changes, invalidating the whole `-resume` cache. Absolute indices make a grid's identity
    a pure function of the tiles in it and `grid_size`.
    """
    epsg = crs.split(":")[-1]
    return f"grid_{epsg}_{ix // grid_size}_{iy // grid_size}"


def build_grids(
    core_tiles: list[Tile],
    halo_pool: list[Tile],
    *,
    crs: str,
    grid_size: int,
) -> dict[str, dict[str, list[Tile]]]:
    """
    Partition `core_tiles` into lattice blocks and attach each block's ring of neighbours.

    `halo_pool` is deliberately the *whole* dataset rather than the region-filtered selection. Halo
    membership decides what points a render can see, so drawing it from the selection would make a
    `--bbox` run produce different pixels than a full run for the same tile. Keeping the pool global
    is what makes a small test region a faithful sample -- and what lets
    assets/laz_tiles_immenstadt.csv be 24 rows of Bavaria's export and still plan the grids the full
    71,979-row CSV plans for that bbox.
    """
    tile_w, tile_h, _ = infer_tile_size(core_tiles)

    def index(t: Tile) -> tuple[int, int]:
        return math.floor(t.min_x / tile_w), math.floor(t.min_y / tile_h)

    blocks: dict[str, list[Tile]] = defaultdict(list)
    for t in core_tiles:
        ix, iy = index(t)
        blocks[grid_id_for(crs, ix, iy, grid_size)].append(t)

    pool_by_cell: dict[tuple[int, int], list[Tile]] = defaultdict(list)
    for t in halo_pool:
        pool_by_cell[index(t)].append(t)

    grids: dict[str, dict[str, list[Tile]]] = {}
    for grid_id, core in sorted(blocks.items()):
        core_names = {t.tile for t in core}
        ring = {
            (ix + dx, iy + dy)
            for ix, iy in (index(t) for t in core)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
        }
        halo = [
            t
            for cell in ring
            for t in pool_by_cell.get(cell, ())
            if t.tile not in core_names
        ]
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
            ["grid_id", "crs", "n_core", "n_halo", "bytes_total", "min_x", "min_y", "max_x", "max_y"]
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
                ]
            )


def write_parent_tiles(
    outdir: Path, grids: dict[str, dict[str, list[Tile]]], *, crs: str, base_zoom: int
) -> tuple[int, int]:
    """
    Write the tile -> web-mercator-parent mapping, and return (n_parents, n_rows).

    The `tile` column holds the tile *stem*, because that is what karttapullautin names its outputs
    after (`<stem>.png`, `<stem>_depr.pgw`) and what the workflow joins on.

    `n_core` is the number of core tiles feeding that parent. Nextflow uses it as the `groupKey`
    size, so tiling of a finished parent can start while other grids are still rendering.
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

    One osmium pass costs a full read of the source pbf (809 MB for Bavaria), so extracting per grid
    would re-read it once per grid. The chunk size stays modest because `--strategy=smart` keeps a
    per-extract id set in memory.

    The bbox is buffered in the projected CRS and only then converted to lon/lat, so the buffer is a
    real distance rather than a number of degrees that shrinks with latitude.
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
    ap.add_argument("--grid-size", type=int, required=True, help="core tiles per grid edge")
    ap.add_argument("--base-zoom", type=int, default=12)
    ap.add_argument("--osm-buffer-m", type=float, default=2000.0)
    ap.add_argument("--osm-chunk-size", type=int, default=32)
    ap.add_argument("--bbox", help="minx,miny,maxx,maxy region filter")
    ap.add_argument(
        "--bbox-crs",
        default="lonlat",
        help="'lonlat' (default) or an EPSG string, meaning --bbox is in the tiles' own CRS",
    )
    args = ap.parse_args(argv)

    if args.grid_size < 1:
        raise PlanError("--grid-size must be >= 1")

    all_tiles = read_tiles(args.tiles_csv)
    derive_lonlat(all_tiles)

    bbox = parse_bbox(args.bbox) if args.bbox else None
    tiles, dropped = select_tiles(all_tiles, bbox=bbox, bbox_crs=args.bbox_crs)
    # Only the *selection* has to be single-CRS, so a multi-zone national dataset can be processed
    # one zone at a time -- which is what require_single_crs()'s error message tells the user to do.
    crs = require_single_crs(tiles)
    halo_pool = [t for t in all_tiles if t.crs == crs]

    tile_w, tile_h, uniform = infer_tile_size(tiles)
    grids = build_grids(tiles, halo_pool, crs=crs, grid_size=args.grid_size)

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
    # Distinct files touched, i.e. what a perfect cache would fetch.
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
        "",
        f"tiles in CSV         {len(all_tiles):,}",
        f"rendered (core)      {len(tiles):,}"
        + (f"  (filter dropped {len(dropped):,})" if dropped else ""),
        f"halo only            {halo_only:,}  (fetched for their points, never rendered; drawn "
        f"from the whole CSV so a filtered region renders identically to a full run)",
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
    (args.outdir / "plan_summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PlanError as exc:
        print(f"plan_grids.py: {exc}", file=sys.stderr)
        sys.exit(1)
