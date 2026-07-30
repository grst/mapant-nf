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
        help="shape mapping file; empty disables vector rendering, which is what a laz-only "
        "region (no OSM pbf) needs",
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
