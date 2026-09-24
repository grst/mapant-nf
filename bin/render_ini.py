#!/usr/bin/env python3
"""
Produce the effective pullauta.ini from the user's file plus the settings the pipeline must own.

The result is also the run's provenance record -- a render is only reproducible if you kept the
exact parameters -- so it is published alongside the tiles.
"""

from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

# Keys the pipeline owns because the surrounding process depends on their values. Everything else in
# the user's ini -- the whole vegetation and cliff model -- is passed through untouched.
#
# batch/lazfolder/batchoutfolder: the process lays out `in/` and `out/` itself.
# processes: comes from the Nextflow `cpus` directive, so one place controls it.
# savetempfiles/savetempfolders: these add extra *outputs*; they do not control cleanup, which
#   karttapullautin never does. Extra outputs would be pruned moments later.
# experimental_use_in_memory_fs: copies every input laz into RAM, which at ~200 MB per tile and a
#   hundred tiles per grid is not survivable.
# vectorvege: karttapullautin's vector export -- contours, form lines, knolls, cliffs, vegetation,
#   and the OSM shapes it matches -- written per tile, already in its published form.
# geojson_wgs84: those files in longitude/latitude, the only CRS tippecanoe reads, so
#   MAKE_VECTOR_TILES hands them over untouched. It needs `epsg`, which is per grid and so is set
#   by run_pullauta.py from the grid CSV, not here.
# batchmerge: off. The pipeline cuts tiles per parent; merging a grid into one file is a reduction
#   nothing reads.
# output_dxf: off, because it is on in most inis (including this pipeline's own asset) and writes a
#   second, text copy of every vector that nothing downstream reads.
# vectorconf: the OSM rules file, staged as osm.txt, or empty when the run has no OSM extract --
#   an empty value is what turns karttapullautin's shapefile pass off.
#
# None of it is conditional: a vector pyramid is the only thing this pipeline builds.
#
# Writing batch, processes, savetempfiles and savetempfolders unconditionally also guarantees they
# exist: karttapullautin reads those four with .unwrap(), so an absent one is a panic rather than a
# default.
OWNED = {
    "batch": "1",
    "lazfolder": "./in",
    "batchoutfolder": "./out",
    "savetempfiles": "0",
    "savetempfolders": "0",
    "experimental_use_in_memory_fs": "0",
    "vectorvege": "1",
    "geojson_wgs84": "1",
    "batchmerge": "0",
    "output_dxf": "0",
}


def read_ini(path: Path) -> configparser.SectionProxy:
    """
    Read a karttapullautin ini, which has no section headers at all.

    interpolation=None: values like `zone1=1.0|2.65|99|1` are not format strings.
    strict=False: upstream inis do carry duplicated keys.
    optionxform=str: keys are used verbatim, never case-folded.
    """
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string("[pullauta]\n" + path.read_text())
    return cp["pullauta"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in-ini", type=Path, required=True)
    ap.add_argument("--out-ini", type=Path, required=True)
    ap.add_argument("--processes", type=int, required=True)
    ap.add_argument(
        "--vectorconf",
        default="",
        help="the OSM rules file name as staged next to the ini; empty for a run without OSM",
    )
    args = ap.parse_args(argv)

    if not args.in_ini.is_file():
        print(f"render_ini.py: no such ini: {args.in_ini}", file=sys.stderr)
        return 1
    if args.processes < 1:
        print("render_ini.py: --processes must be >= 1", file=sys.stderr)
        return 1

    conf = read_ini(args.in_ini)
    conf.update(OWNED)
    conf["processes"] = str(args.processes)
    conf["vectorconf"] = args.vectorconf

    # No section header on output: karttapullautin reads rust-ini's general_section(), so a
    # `[pullauta]` line would hide every key below it. Comments do not survive the round trip; the
    # values, which are what makes a render reproducible, all do.
    args.out_ini.write_text("".join(f"{key} = {value}\n" for key, value in conf.items()))

    for key in sorted({*OWNED, "processes", "vectorconf", "contour_interval", "formline"}):
        print(f"  {key:32s} {conf.get(key)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
