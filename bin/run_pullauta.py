#!/usr/bin/env python3
"""
Render a grid's core tiles with karttapullautin, surviving the tiles that make it panic.

This is a loop rather than one invocation because karttapullautin v2 .unwrap()s throughout: a
malformed laz aborts the *process*, and since `launch_threads` joins worker 1 first, a panic anywhere
also abandons up to `processes - 1` in-flight tiles mid-write.

Two facts from its source make a clean recovery possible:

  * Plan::new_from_input_files() queues a laz file only if `<batchoutfolder>/<stem>.png` does not
    already exist, so pullauta resumes by itself.
  * plan.input_files() -- the list consulted for the 127 m halo -- contains *every* laz file,
    including the ones that check excluded from the queue.

So an empty placeholder PNG hides a tile from the renderer while its points stay available to its
neighbours. That is used to stop halo tiles being rendered at all, and to blacklist a tile that
panics -- blacklisting rather than deleting the laz is what keeps the neighbours correct.

A panic cannot be pinned on a tile from a multi-worker log, so an attempt that makes no progress is
retried with processes=1, where the last "<in> -> <out>" line is unambiguously the culprit.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import re
import subprocess
import sys
from pathlib import Path

FAILURE_COLUMNS = (
    "tile", "role", "grid_id", "crs", "min_x", "min_y", "max_x", "max_y", "size_bytes", "url",
    "sha256", "pullauta_version", "pullauta_git_sha", "isa_variant", "exit_code", "reason",
    "panic_message", "log_tail",
)

# A PNG that ends in an IEND chunk was closed properly. This distinguishes "this tile is done" from
# "the process was killed while writing this tile" -- which matters because pullauta's own resume
# logic only tests for the file's existence.
IEND = bytes.fromhex("49454e44ae426082")

RENDERED_TILE_RE = re.compile(r"(\S+\.la[sz]) -> ")
PANIC_RE = re.compile(r"panicked at|^Error|^thread ")

GIT_SHA_FILE = Path("/opt/karttapullautin/GIT_SHA")


class Renderer:
    """One grid's render, with the state the attempt ladder needs."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path("out")
        self.out.mkdir(exist_ok=True)

        with args.csv.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        # Stems, i.e. tile names without the suffix, because that is what karttapullautin names its
        # outputs after.
        self.rows = {Path(r["tile"]).stem: r for r in rows}
        self.core = [Path(r["tile"]).stem for r in rows if r["role"] == "core"]
        self.halo = [Path(r["tile"]).stem for r in rows if r["role"] == "halo"]
        if not self.core:
            sys.exit(f"run_pullauta.py: {args.csv} lists no core tiles")

        self.blacklisted: set[str] = set()
        # Provenance for the failure report. Deliberately not obtained by probing the binary: with
        # batch=1 and no arguments, `pullauta` starts rendering. The version and the ISA variant both
        # appear in the renderer's own first lines of output, so they are read from the real log.
        self.git_sha = GIT_SHA_FILE.read_text().strip() if GIT_SHA_FILE.is_file() else "unknown"
        self.version = "unknown"
        self.isa = "unknown"
        self.failures: list[list[str]] = []

    # -- tile state ---------------------------------------------------------
    def is_complete(self, stem: str) -> bool:
        """pullauta writes <t>.png, <t>.pgw, <t>_depr.png, <t>_depr.pgw; a partial quartet is redone."""
        for name in (f"{stem}.pgw", f"{stem}_depr.pgw"):
            if not (self.out / name).is_file() or (self.out / name).stat().st_size == 0:
                return False
        for name in (f"{stem}.png", f"{stem}_depr.png"):
            png = self.out / name
            if not png.is_file() or png.stat().st_size < len(IEND):
                return False
            with png.open("rb") as fh:
                fh.seek(-len(IEND), 2)
                if fh.read() != IEND:
                    return False
        return True

    def pending(self) -> list[str]:
        return [s for s in self.core if s not in self.blacklisted and not self.is_complete(s)]

    def quartet(self, stem: str) -> list[Path]:
        return [self.out / f"{stem}{v}.{e}" for v in ("", "_depr") for e in ("png", "pgw")]

    def quarantine_incomplete(self) -> None:
        """
        Remove half-written quartets so pullauta's resume logic picks those tiles up again.

        Doing nothing here would be the worst outcome: <t>.png exists, so the tile would be skipped
        forever and shipped as a corrupt image.
        """
        n = 0
        for stem in self.core:
            if stem in self.blacklisted:
                continue
            # Nothing written at all is not damage, just not-done-yet.
            if any(p.exists() for p in self.quartet(stem)) and not self.is_complete(stem):
                for path in self.quartet(stem):
                    path.unlink(missing_ok=True)
                n += 1
        if n:
            log(f"quarantined {n} half-written tile(s)")

    def blacklist(self, stem: str) -> None:
        """Skip a tile from now on, keeping its points available to its neighbours."""
        self.blacklisted.add(stem)
        for path in self.quartet(stem):
            path.unlink(missing_ok=True)
        (self.out / f"{stem}.png").touch()

    def record_failure(self, stem: str, exit_code: str, reason: str, panic: str, tail: str) -> None:
        """
        Everything an upstream bug report needs: the exact file, where it is, how to fetch it, which
        build produced the crash, and the panic itself.
        """
        row = self.rows[stem]
        self.failures.append(
            [
                stem, "core", self.args.grid_id, row["crs"],
                row["min_x"], row["min_y"], row["max_x"], row["max_y"],
                row["size_bytes"], row["url"], row["sha256"],
                self.version, self.git_sha, self.isa, exit_code, reason,
                flatten(panic), flatten(tail),
            ]
        )

    # -- the render ---------------------------------------------------------
    def write_ini(self, processes: int) -> None:
        """
        pullauta reads pullauta.ini from the current directory and has no flag to point it elsewhere,
        so write a real copy: the staged input is a symlink to a file we must not edit, and
        `processes` has to change between attempts.
        """
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        cp.read_string("[pullauta]\n" + self.args.ini.read_text())
        cp["pullauta"]["processes"] = str(processes)
        # No section header: karttapullautin reads rust-ini's general_section().
        Path("pullauta.ini").write_text(
            "".join(f"{k} = {v}\n" for k, v in cp["pullauta"].items())
        )

    def render(self, processes: int) -> tuple[int, str]:
        """Run the renderer once. Returns (exit code, its output)."""
        self.write_ini(processes)
        proc = subprocess.run(["pullauta"], capture_output=True, text=True)
        output = proc.stdout + proc.stderr
        with self.args.log.open("a") as fh:
            fh.write(output)
        # Both lines are printed at the top of every run -- the ISA line by the dispatch wrapper, the
        # version by pullauta itself.
        if self.isa == "unknown":
            self.isa = first_match(r"^pullauta: ISA variant (\S+)", output) or "unknown"
        if self.version == "unknown":
            self.version = first_match(r"^Karttapullautin v(.*)$", output) or "unknown"
        return proc.returncode, output

    def ladder(self) -> int:
        """
        Render, isolating tiles that abort the process, until nothing is left to do.

        Returns the last exit code, for the failure report.
        """
        made_progress = True
        done_before = 0
        exit_code = 0

        for attempt in range(1, self.args.max_attempts + 1):
            pending = self.pending()
            if not pending:
                break

            # Serialise only when the previous attempt achieved nothing: that is the situation where
            # a crash has to be attributed to a specific tile.
            processes = self.args.processes if made_progress else 1
            log(f"attempt {attempt}/{self.args.max_attempts}: "
                f"{len(pending)} tile(s) pending, processes={processes}")

            exit_code, output = self.render(processes)
            self.quarantine_incomplete()
            done_now = sum(1 for s in self.core if s not in self.blacklisted and self.is_complete(s))
            made_progress = done_now > done_before
            done_before = done_now
            log(f"attempt {attempt} exit={exit_code}, {done_now}/{len(self.core)} core tiles done")

            if exit_code == 0 and done_now == len(self.core) - len(self.blacklisted):
                break
            if made_progress:
                continue

            # Did the renderer even get as far as a tile? If not, this is a configuration or
            # environment failure -- a missing shapefile zip, an unreadable ini, a full disk -- and
            # blaming a tile for it would hide a real problem and corrupt the bug report.
            started = RENDERED_TILE_RE.findall(output)
            if not started:
                log("pullauta failed without starting a tile; not a per-tile bug")
                log(tail(output, 40))
                self.write_failures()
                sys.exit(exit_code or 1)
            if processes == 1:
                stem = Path(started[-1]).stem
                panic = "\n".join(
                    [ln for ln in output.splitlines() if PANIC_RE.search(ln)][-3:]
                )
                log(f"blacklisting {stem} (exit {exit_code}): {panic or 'no panic message'}")
                self.record_failure(
                    stem, str(exit_code),
                    "karttapullautin aborted while rendering this tile",
                    panic, tail(output, 20),
                )
                self.blacklist(stem)
                made_progress = True  # the blacklist *is* progress; go back to full parallelism

        return exit_code

    # -- afterwards ---------------------------------------------------------
    def prune(self) -> None:
        """
        Leave out/ holding exactly the rendered tiles of the requested variant.

        The placeholders were a means, not a result, and `<t>.png` / `<t>_depr.png` are the same map
        with and without depression markings -- carrying both would double a full run's intermediate
        storage (~90 GB -> ~175 GB for Bavaria) for output nobody consumes.
        """
        for stem in [*self.halo, *self.blacklisted]:
            png = self.out / f"{stem}.png"
            if png.exists() and png.stat().st_size == 0:
                png.unlink()

        for path in self.out.iterdir():
            # Deleting anything that is not a wanted render also gets rid of whatever else pullauta
            # dropped here (<t>_basemap.dxf.bin and friends), which must not reach the publish step.
            if path.is_file() and not self.wanted(path):
                path.unlink()

    def wanted(self, path: Path) -> bool:
        if path.suffix not in (".png", ".pgw"):
            return False
        if self.args.variant == "both":
            return True
        is_depr = path.stem.endswith("_depr")
        return is_depr if self.args.variant == "depr" else not is_depr

    def write_failures(self) -> None:
        with self.args.failures.open("w", newline="") as fh:
            w = csv.writer(fh, delimiter="\t", lineterminator="\n")
            w.writerow(FAILURE_COLUMNS)
            w.writerows(self.failures)


def log(message: str) -> None:
    print(f"run_pullauta.py: {message}", file=sys.stderr)


def flatten(text: str) -> str:
    """TSV fields cannot hold tabs or newlines."""
    return text.replace("\t", " ").replace("\n", " ")


def tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])


def first_match(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path, required=True, help="a grid CSV from PLAN_GRIDS")
    ap.add_argument("--grid-id", default="(unnamed grid)")
    ap.add_argument("--ini", type=Path, default=Path("effective.ini"))
    ap.add_argument("--processes", type=int, default=1)
    ap.add_argument("--max-attempts", type=int, default=6)
    ap.add_argument("--variant", choices=("depr", "plain", "both"), default="depr")
    ap.add_argument("--log", type=Path, default=Path("pullauta.log"))
    ap.add_argument("--failures", type=Path, default=Path("failures.tsv"))
    args = ap.parse_args(argv)

    args.log.write_text("")
    r = Renderer(args)

    # fetch_laz.py has already decided these are permanently unavailable (it would have failed the
    # task otherwise). Blacklisting them up front stops the ladder spending an attempt per tile
    # rediscovering that they cannot be rendered.
    for stem in r.core:
        if not any(Path("in").glob(f"{stem}.la[sz]")):
            log(f"{stem} was never downloaded; recording it as a hole")
            r.record_failure(stem, "", "laz file unavailable (see download_failures.tsv)", "", "")
            r.blacklist(stem)

    # Halo tiles are here only for their points; the placeholder is what stops pullauta rendering
    # them. At grid_size 10 the ring is 36% of the files in the folder.
    placeholders = 0
    for stem in r.halo:
        png = r.out / f"{stem}.png"
        if not png.exists():
            png.touch()
            placeholders += 1
    log(f"{len(r.core)} core tiles, {len(r.halo)} halo tiles "
        f"({placeholders} placeholders so they are not rendered)")

    exit_code = r.ladder()

    for stem in r.pending():
        log(f"giving up on {stem} after {args.max_attempts} attempts")
        r.record_failure(
            stem, str(exit_code), f"still unrendered after {args.max_attempts} attempts",
            "", tail(args.log.read_text(), 20),
        )

    r.prune()
    r.write_failures()

    rendered = len(list(r.out.glob("*.pgw")))
    log(f"{args.grid_id} finished: {rendered} tile(s) rendered, "
        f"{len(r.failures)} recorded as failures")

    # Nothing at all came out: that is not a bad tile, that is a broken grid, and it should be
    # retried rather than silently leaving a hole the size of the whole batch.
    if rendered == 0:
        log("no tiles rendered at all; failing so this grid is retried")
        log(tail(args.log.read_text(), 40))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
