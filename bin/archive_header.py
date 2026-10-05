#!/usr/bin/env python3
"""
Give the merged archive the header and metadata a viewer reads: where the map is, where to look
first, and what it is called.

`tile-join` makes them up from its inputs, and for an archive joined from one archive per parent
they come out wrong in ways that matter: the centre is a corner of the last parent at the deepest
zoom, the description is the name of the last input file, and `generator_options` is every
parent's tippecanoe command line, one after another -- about a kilobyte per parent, read by every
viewer that opens the archive.

So the bounds are the rendered region's, from the plan: the envelope of every core tile's square
in WGS84. The centre is their middle, at the zoom the pyramid starts generalising for (the base
zoom), and the metadata keeps what describes the tiles (`vector_layers`, `attribution`) and drops
the rest. Written as the JSON `pmtiles show --header-json` / `--metadata` print and
`pmtiles edit` reads.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pyproj

#: What tile-join writes into the metadata that describes how it was made rather than what it holds.
DROPPED = ("generator_options", "antimeridian_adjusted_bounds", "strategies", "tilestats")


def region_bounds(parent_tiles: Path) -> tuple[float, float, float, float]:
    """The WGS84 envelope of the planned core tiles, densified: a UTM square's edges curve."""
    boxes: dict[str, tuple[str, float, float, float, float]] = {}
    with parent_tiles.open(newline="") as fh:
        for row in csv.DictReader(fh):
            boxes[row["tile"]] = (row["crs"], *(float(row[k]) for k in ("min_x", "min_y", "max_x", "max_y")))
    if not boxes:
        raise SystemExit(f"archive_header.py: {parent_tiles} has no tiles")
    west = south = 180.0
    east = north = -180.0
    transformers: dict[str, pyproj.Transformer] = {}
    for crs, x0, y0, x1, y1 in boxes.values():
        tr = transformers.setdefault(crs, pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True))
        w, s, e, n = tr.transform_bounds(x0, y0, x1, y1, densify_pts=21)
        west, south, east, north = min(west, w), min(south, s), max(east, e), max(north, n)
    return west, south, east, north


def header(joined: dict, bounds: tuple[float, float, float, float], center_zoom: int) -> dict:
    west, south, east, north = (round(v, 7) for v in bounds)
    out = dict(joined)
    out["bounds"] = [west, south, east, north]
    zoom = min(max(center_zoom, joined["minzoom"]), joined["maxzoom"])
    out["center"] = [round((west + east) / 2, 7), round((south + north) / 2, 7), zoom]
    return out


def metadata(joined: dict, title: str, description: str) -> dict:
    out = {k: v for k, v in joined.items() if k not in DROPPED}
    out["name"] = title
    out["description"] = description
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--header-in", type=Path, required=True, help="pmtiles show --header-json")
    ap.add_argument("--metadata-in", type=Path, required=True, help="pmtiles show --metadata")
    ap.add_argument("--parent-tiles", type=Path, required=True, help="parent_tiles.csv from the plan")
    ap.add_argument("--center-zoom", type=int, required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--header-out", type=Path, required=True)
    ap.add_argument("--metadata-out", type=Path, required=True)
    args = ap.parse_args(argv)

    bounds = region_bounds(args.parent_tiles)
    head = header(json.loads(args.header_in.read_text()), bounds, args.center_zoom)
    meta = metadata(json.loads(args.metadata_in.read_text()), args.title, args.description)
    args.header_out.write_text(json.dumps(head, indent=1) + "\n")
    args.metadata_out.write_text(json.dumps(meta, indent=1) + "\n")
    print(f"bounds {head['bounds']}, centre {head['center']}, zoom {head['minzoom']}-{head['maxzoom']}, "
          f"layers {', '.join(layer['id'] for layer in meta.get('vector_layers', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
