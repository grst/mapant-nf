#!/usr/bin/env python3
"""
Write the osmconf.ini that makes ogr2ogr expose every OSM key the shape rules file tests.

karttapullautin matches the OSM shapes to ISOM symbols by the columns of the shapefiles ogr2ogr
writes from the .pbf (`power line|516|power=line`). ogr2ogr's default osmconf.ini makes a column
of only a handful of keys per layer -- `lines` has no `power`, `multipolygons` no `water` or
`waterway` -- and folds everything else into one `other_tags` text. A rule on any other key then
compares the empty string, so `power=line` matches nothing and `power!=` everything: power lines
were simply absent from the map, with nothing to say so.

So the keys come from the rules file itself: every key a condition names becomes a column of every
layer, on top of GDAL's own list. A region with rules of its own needs no change here.

Two limits of the shapefile format are reported rather than silently suffered: a column name is at
most 10 characters (ogr2ogr truncates `admin_level` to `admin_leve`, which no rule names), and a
key with characters a DBF field cannot have (`addr:street`) is laundered to something else.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: The osmconf.ini sections that describe an OGR layer.
LAYERS = ("points", "lines", "multipolygons", "multilinestrings", "other_relations")

#: A shapefile (DBF) column name.
DBF_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,9}$")


def rule_keys(rules: str) -> list[str]:
    """The keys the rules' conditions test, in first-seen order: `desc|isom|k=v&k2!=v2`."""
    keys: list[str] = []
    for line in rules.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        for condition in parts[2].split("&"):
            key = re.split(r"!=|=", condition, maxsplit=1)[0].strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def with_keys(template: str, keys: list[str]) -> str:
    """The template with `keys` added to the `attributes=` line of every layer section."""
    out = []
    section = None
    for line in template.splitlines():
        header = re.match(r"^\[(\w+)\]\s*$", line)
        if header:
            section = header.group(1)
        elif section in LAYERS and line.startswith("attributes="):
            have = [a for a in line.split("=", 1)[1].split(",") if a]
            line = "attributes=" + ",".join(have + [k for k in keys if k not in have])
        out.append(line)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", type=Path, required=True, help="karttapullautin's vectorconf (osm.txt)")
    ap.add_argument("--template", type=Path, required=True, help="GDAL's osmconf.ini")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    keys = rule_keys(args.rules.read_text(errors="replace"))
    usable = [k for k in keys if DBF_NAME.match(k)]
    for key in keys:
        if key not in usable:
            print(f"osmconf.py: rules test `{key}`, which a shapefile column cannot be called; "
                  f"no feature will match on it", file=sys.stderr)
    args.out.write_text(with_keys(args.template.read_text(), usable))
    print(f"osmconf.ini: {len(usable)} key(s) from the rules: {', '.join(usable)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
