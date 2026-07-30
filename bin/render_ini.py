#!/usr/bin/env python3
"""
Produce the effective pullauta.ini from the user's file plus the settings the pipeline must own.

Three things make this worth a script rather than a few `sed` lines.

1. karttapullautin parses its ini with rust-ini, whose `get()` returns the **first** occurrence of
   a duplicated key. Appending `processes=8` to a file that already says `processes=4` therefore
   changes nothing, silently. Overrides have to replace the existing line in place.

2. `Config::load_or_create_default()` *writes a default ini* when `pullauta.ini` is missing rather
   than failing, and reads `processes`, `savetempfiles` and `savetempfolders` with `.unwrap()`, so
   an absent key panics. Both failure modes are much easier to diagnose here than three hours into
   a render, so the required keys are asserted.

3. The result is a provenance artifact. A render is only reproducible if you kept the exact
   parameters, so this file is published alongside the tiles.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Keys the pipeline owns, because the surrounding process depends on their values. Anything else
# in the user's ini -- the whole vegetation and cliff model -- is passed through untouched.
#
# batch/lazfolder/batchoutfolder: the process lays out `in/` and `out/` itself.
# processes: comes from the Nextflow `cpus` directive, so one place controls it.
# savetempfiles/savetempfolders: these add *extra outputs* (they do not control cleanup, which is
#   a common misreading -- pullauta never deletes its temp dirs). Extra outputs would be pruned
#   moments later, so they are wasted IO at 15 TB scale.
# experimental_use_in_memory_fs: copies every input laz into RAM. At ~200 MB per tile and a
#   hundred tiles per grid that is not survivable.
OWNED = (
    "batch",
    "processes",
    "lazfolder",
    "batchoutfolder",
    "savetempfiles",
    "savetempfolders",
    "experimental_use_in_memory_fs",
)

# Keys karttapullautin reads with .unwrap(); absent means panic, not default.
REQUIRED = ("processes", "savetempfiles", "savetempfolders", "batch")


def set_key(text: str, key: str, value: str) -> str:
    """
    Replace every assignment of `key`, or append one if there is none.

    Every occurrence, not just the first: rust-ini honours the first, but leaving stale duplicates
    behind makes the file lie to whoever reads it next.
    """
    pattern = re.compile(rf"^([ \t]*){re.escape(key)}([ \t]*)=([ \t]*).*$", re.MULTILINE)
    if pattern.search(text):
        # Keep the surrounding whitespace exactly as the user wrote it. rust-ini trims values, so
        # this is cosmetic -- but the file is published as the provenance record of the run, and a
        # provenance record that reformats its input is harder to diff against the original.
        return pattern.sub(rf"\g<1>{key}\g<2>=\g<3>{value}", text, count=0)
    if not text.endswith("\n"):
        text += "\n"
    return text + f"{key} = {value}\n"


def effective(text: str, key: str) -> str | None:
    """The value rust-ini would see: the first uncommented assignment."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


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

    text = args.in_ini.read_text()

    overrides = {
        "batch": "1",
        "processes": str(args.processes),
        "lazfolder": "./in",
        "batchoutfolder": "./out",
        "savetempfiles": "0",
        "savetempfolders": "0",
        "experimental_use_in_memory_fs": "0",
        "vectorconf": args.vectorconf,
    }
    for key, value in overrides.items():
        text = set_key(text, key, value)

    args.out_ini.write_text(text)

    # Read the file back and check what karttapullautin will actually see. Asserting on the string
    # we just built would only prove set_key() agrees with itself.
    written = args.out_ini.read_text()
    problems = []
    for key in REQUIRED:
        if effective(written, key) is None:
            problems.append(f"{key} is missing (karttapullautin reads it with .unwrap() and panics)")
    for key, want in overrides.items():
        if key == "vectorconf" and want == "":
            continue  # an empty value is legitimate and effective() cannot distinguish it
        got = effective(written, key)
        if got != want:
            problems.append(f"{key} should be {want!r} but the effective value is {got!r}")
    if problems:
        for p in problems:
            print(f"render_ini.py: {p}", file=sys.stderr)
        return 1

    for key in sorted({*OWNED, "vectorconf", "contour_interval", "formline", "thinfactor"}):
        print(f"  {key:32s} {effective(written, key)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
