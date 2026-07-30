#!/usr/bin/env python3
"""
Write a self-contained index.html that previews the finished tile pyramid.

k2t ships a viewer, but `cli.py` passes `max_zoom=-max_zoom` into the template, so its zoom bounds
come out wrong. This writes an equivalent viewer with the right bounds, centred on the data, and
with the actual zoom range the run produced -- which is also the quickest way to tell whether a
run's output is usable at all.

The map is centred on the middle of the rendered area, derived from parent_tiles.csv rather than
hardcoded, so it works for any region.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import mercantile

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin: 0; height: 100%; }
  #map { height: 100%; background: #fff; }
  .legend {
    background: rgba(255,255,255,.9); padding: 6px 8px; border-radius: 4px;
    font: 12px/1.4 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.3);
  }
</style>
</head>
<body>
<div id="map"></div>
<script>
  const map = L.map('map').setView([__LAT__, __LON__], __DEFAULT_ZOOM__);

  // The tiles live in ./{z}/{x}/{y}.png next to this file. `noWrap` matters: without it Leaflet
  // requests copies of the world either side of the antimeridian and the console fills with 404s.
  L.tileLayer('{z}/{x}/{y}.png', {
    minZoom: __MIN_ZOOM__,
    maxZoom: __MAX_ZOOM__,
    tileSize: 256,
    noWrap: true,
    attribution: '__ATTRIBUTION__'
  }).addTo(map);

  const legend = L.control({ position: 'bottomleft' });
  legend.onAdd = () => {
    const div = L.DomUtil.create('div', 'legend');
    div.innerHTML = '__LEGEND__';
    return div;
  };
  legend.addTo(map);
</script>
</body>
</html>
"""

ATTRIBUTION = (
    'Map: <a href="https://github.com/karttapullautin/karttapullautin">karttapullautin</a> '
    "&middot; LiDAR: source data provider &middot; Vectors: &copy; OpenStreetMap contributors"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parent-tiles", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--min-zoom", type=int, required=True)
    ap.add_argument("--max-zoom", type=int, required=True)
    ap.add_argument("--title", default="mapant")
    args = ap.parse_args(argv)

    west = south = None
    east = north = None
    n_parents = 0
    with args.parent_tiles.open(newline="") as fh:
        for row in csv.DictReader(fh):
            b = mercantile.bounds(int(row["x"]), int(row["y"]), int(row["z"]))
            n_parents += 1
            west = b.west if west is None else min(west, b.west)
            south = b.south if south is None else min(south, b.south)
            east = b.east if east is None else max(east, b.east)
            north = b.north if north is None else max(north, b.north)

    if west is None:
        print(f"make_viewer.py: {args.parent_tiles} has no rows", file=sys.stderr)
        return 1

    lon = (west + east) / 2
    lat = (south + north) / 2
    # Open at the base zoom: the overviews exist for orientation, but the point of the map is the
    # detail, and starting fully zoomed out on a small region shows a dot.
    default_zoom = max(args.min_zoom, min(args.max_zoom, args.max_zoom - 5))

    legend = (
        f"<strong>{args.title}</strong><br>"
        f"zoom {args.min_zoom}&ndash;{args.max_zoom} &middot; "
        f"{n_parents:,} base tiles<br>"
        f"{west:.4f},{south:.4f} &rarr; {east:.4f},{north:.4f}"
    )

    html = TEMPLATE
    for key, value in {
        "__TITLE__": args.title,
        "__LAT__": f"{lat:.6f}",
        "__LON__": f"{lon:.6f}",
        "__DEFAULT_ZOOM__": str(default_zoom),
        "__MIN_ZOOM__": str(args.min_zoom),
        "__MAX_ZOOM__": str(args.max_zoom),
        "__ATTRIBUTION__": ATTRIBUTION,
        "__LEGEND__": legend,
    }.items():
        html = html.replace(key, value)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"make_viewer.py: wrote {args.out} centred on {lat:.5f},{lon:.5f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
