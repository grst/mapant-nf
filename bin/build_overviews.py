#!/usr/bin/env python3
"""
Build the zoom levels below the base zoom by repeatedly halving.

Why not just ask k2t for them: k2t renders each output tile by reprojecting the source imagery into
that tile's own bounds, treating them as linear in lon/lat. The approximation is excellent at high
zoom and gets worse as tiles get bigger, so the low zooms are exactly where it is least accurate.
Averaging four finished child tiles has no such error, and it is orders of magnitude cheaper --
about 1600 base tiles for all of Bavaria, versus re-reading terabytes of source PNGs.

Input is the `base_<z>_<x>_<y>.png` files MAKE_TILES emits. Those carry their coordinates in the
filename because Nextflow stages inputs by basename, so the z/x/y directory structure they came
from is not available here.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

# k2t fills areas with no data with opaque white, so a missing child must be white too -- not
# transparent, and not black, either of which would show as a visible block at low zoom.
NODATA = (255, 255, 255)

NAME_RE = re.compile(r"^base_(?P<z>\d+)_(?P<x>\d+)_(?P<y>\d+)\.png$")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in-dir", type=Path, default=Path("."))
    ap.add_argument("--out-dir", type=Path, default=Path("tiles"))
    ap.add_argument("--min-zoom", type=int, required=True)
    ap.add_argument("--tile-size", type=int, default=256)
    args = ap.parse_args(argv)

    level: dict[tuple[int, int], Path] = {}
    base_zoom: int | None = None
    for path in sorted(args.in_dir.glob("base_*.png")):
        m = NAME_RE.match(path.name)
        if not m:
            continue
        z, x, y = (int(m[k]) for k in ("z", "x", "y"))
        if base_zoom is None:
            base_zoom = z
        elif z != base_zoom:
            print(
                f"build_overviews.py: got base tiles at both z{base_zoom} and z{z}; "
                "all base tiles must come from one zoom level",
                file=sys.stderr,
            )
            return 1
        level[(x, y)] = path

    if base_zoom is None:
        print("build_overviews.py: no base_<z>_<x>_<y>.png inputs found", file=sys.stderr)
        return 1
    if args.min_zoom > base_zoom:
        print(
            f"build_overviews.py: --min-zoom {args.min_zoom} is above the base zoom {base_zoom}; "
            "nothing to do",
            file=sys.stderr,
        )
        return 0

    size = args.tile_size
    # Held in memory as PIL images from here on: one zoom level of Bavaria is ~1600 tiles of 256 px,
    # so a few hundred MB at the widest, and it halves every round.
    images: dict[tuple[int, int], Image.Image] = {
        xy: Image.open(p).convert("RGB") for xy, p in level.items()
    }

    for z in range(base_zoom - 1, args.min_zoom - 1, -1):
        groups: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for x, y in images:
            groups[(x // 2, y // 2)].append((x, y))

        nxt: dict[tuple[int, int], Image.Image] = {}
        for (px, py), children in sorted(groups.items()):
            # Compose at double size, then downsample once: resizing each child separately and
            # pasting them would apply the filter to each quadrant in isolation and leave seams
            # along the internal edges.
            canvas = Image.new("RGB", (size * 2, size * 2), NODATA)
            for cx, cy in children:
                canvas.paste(images[(cx, cy)], ((cx - px * 2) * size, (cy - py * 2) * size))
            nxt[(px, py)] = canvas.resize((size, size), Image.LANCZOS)

        out_z = args.out_dir / str(z)
        for (px, py), img in nxt.items():
            d = out_z / str(px)
            d.mkdir(parents=True, exist_ok=True)
            img.save(d / f"{py}.png", optimize=True)
        print(f"build_overviews.py: z{z}: {len(nxt)} tiles from {len(images)}", file=sys.stderr)
        images = nxt

    return 0


if __name__ == "__main__":
    sys.exit(main())
