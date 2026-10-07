#!/usr/bin/env python3
"""
Acquire every laz file a grid needs into ./in, and prove each one arrived intact.

A truncated laz does not make karttapullautin fail -- it renders whatever points it managed to read,
so the damage surfaces as a plausible but wrong map tile. So every file is verified on every attempt,
however it arrived: by its checksum, where the CSV has one. The checksum column holds a bare
SHA-256, or a digest prefixed with its algorithm (`sha256:<hex>`, `sha1:<hex>`): few sources publish
SHA-256, and some publish SHA-1. The CSV's size is not checked: sources misreport it, and a correct
file must not be rejected for it. Without a checksum there is still curl, which fails a transfer
shorter than the server's Content-Length.

A tile may be a .zip holding one .laz/.las, the way several sources serve them. It is downloaded
next to ./in rather than into it -- karttapullautin unzips any archive in its input folder itself --
verified as above, unpacked to `in/<stem>.<laz|las>` and deleted. Unpacking checks each member's
CRC-32, which covers the laz inside even when the CSV has no checksum for the archive.

Exit status is a contract, because Nextflow's retry logic depends on telling "this will never work"
apart from "the network had a bad minute":

  0   every core tile is verified, or the missing ones failed permanently (404/410/403, or a checksum
      that never matches). Those are written to the failures TSV and left as holes in the map.
  1   at least one tile failed transiently after all retries, or the disk is too small; retrying the
      whole grid is the right response.

A permanently missing *halo* tile is only a warning: the renders next to it lose some of their 127 m
of context, which is a slightly worse border rather than a wrong map.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# curl --fail turns all of these into exit 22; they are settled answers, not bad luck.
PERMANENT_HTTP = {"400", "401", "403", "404", "410", "451"}

USER_AGENT = "mapant/1.0 (+https://github.com/grst/mapant) nextflow pipeline"


def log(message: str) -> None:
    print(f"fetch_laz.py: {message}", file=sys.stderr)


#: Checksum algorithms the CSV may name in a `<algo>:<hex>` prefix, and their digest length in hex.
ALGORITHMS = {"sha256": 64, "sha1": 40}


def parse_checksum(value: str | None) -> tuple[str, str] | None:
    """`<algo>:<hex>` or a bare SHA-256 as (algo, hex digest); None when the cell is empty."""
    value = (value or "").strip().lower()
    if not value:
        return None
    algo, _, digest = value.rpartition(":")
    algo = algo or "sha256"
    if ALGORITHMS.get(algo) != len(digest) or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"not a checksum: {value!r} (expected sha256:<64 hex>, sha1:<40 hex> or "
                         "a bare SHA-256)")
    return algo, digest


def parse_size(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


def verify(path: Path, checksum: tuple[str, str] | None) -> str | None:
    """Return None if the file matches, else a description of the mismatch. Unknown, unchecked."""
    if not path.is_file():
        return "missing"
    if checksum is None:
        return None
    algo, expected = checksum
    digest = hashlib.new(algo)
    with path.open("rb") as fh:
        while chunk := fh.read(4 << 20):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        return f"{algo} mismatch (expected {expected}, got {digest.hexdigest()})"
    return None


def is_zip(tile: str) -> bool:
    return tile.lower().endswith(".zip")


def unpack(archive: Path, outdir: Path) -> str | None:
    """
    Extract the one .laz/.las in `archive` as `<outdir>/<archive stem>.<laz|las>`. Return None on
    success, else a description of what is wrong with the archive.

    Renamed to the archive's stem because the rest of the pipeline knows a tile only by the stem of
    its `tile` column; whatever the member is called inside is the source's business.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.infolist()
                       if not m.is_dir() and Path(m.filename).suffix.lower() in (".laz", ".las")]
            if len(members) != 1:
                return (f"archive holds {len(members)} .laz/.las files, expected exactly one: "
                        f"{', '.join(m.filename for m in members) or 'none'}")
            dest = outdir / (archive.stem + Path(members[0].filename).suffix.lower())
            part = dest.with_name(dest.name + ".part")
            # Reading a member to the end checks its CRC-32 and raises BadZipFile on a mismatch.
            with zf.open(members[0]) as src, part.open("wb") as dst:
                shutil.copyfileobj(src, dst, 4 << 20)
            part.replace(dest)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError) as exc:
        for leftover in outdir.glob(f"{archive.stem}.*.part"):
            leftover.unlink()
        return f"unreadable archive ({exc})"
    return None


def curl(url: str, dest: Path, limit_rate: str | None) -> tuple[int, str, str]:
    """Fetch one URL. Returns (exit status, HTTP status, last line of curl's stderr)."""
    cmd = [
        "curl", "--silent", "--show-error", "--location", "--fail",
        "--connect-timeout", "30",
        # Give up on a stream that delivers less than 1 kB/s for two minutes rather than hanging.
        "--speed-limit", "1024", "--speed-time", "120",
        "--user-agent", USER_AGENT,
        "--write-out", "%{http_code}",
        "--output", str(dest),
    ]
    if limit_rate:
        cmd += ["--limit-rate", limit_rate]
    proc = subprocess.run(cmd + [url], capture_output=True, text=True)
    stderr = proc.stderr.strip().splitlines()
    return proc.returncode, proc.stdout.strip(), stderr[-1] if stderr else ""


def fetch_one(row: dict[str, str], args: argparse.Namespace) -> tuple[str, str]:
    """
    Get one tile into place and verify it. Returns (outcome, detail).

    Outcome is 'ok', 'permanent' (no retry can help) or 'transient' (the grid should be retried).
    """
    tile = row["tile"]
    sha = parse_checksum(row.get("sha256"))
    archive = is_zip(tile)
    # An archive lands outside ./in: karttapullautin would try to unzip it as a shapefile set.
    dest = (args.zipdir if archive else args.outdir) / tile

    # Already there and intact? That happens on a Nextflow retry of the same task. An unpacked laz
    # cannot be checked against the archive's checksum, so an archive is fetched again.
    if not archive and dest.exists() and verify(dest, sha) is None:
        return "ok", "cached"
    dest.unlink(missing_ok=True)

    # The schema permits a bare path as well as a URL; curl needs a scheme. A file:// URL is also how
    # a run against an already-downloaded mirror is expressed: rewrite the CSV's url column, and the
    # bytes still go through the same verification as anything off the network.
    url = row["url"]
    if "://" not in url:
        url = Path(url).resolve().as_uri()

    part = dest.with_name(dest.name + ".part")
    reason = "no attempt made"
    # Whether the *last* attempt was a complete transfer of the wrong bytes, as opposed to a transfer
    # that did not finish. It decides the outcome below.
    bad_bytes = False
    for attempt in range(1, args.retries + 1):
        status, http_code, stderr = curl(url, part, args.limit_rate)
        if status == 0:
            part.replace(dest)
            problem = verify(dest, sha)
            if problem is None and archive:
                problem = unpack(dest, args.outdir)
                dest.unlink()
                if problem is None:
                    return "ok", "downloaded and unpacked"
            if problem is None:
                return "ok", "downloaded"
            # One more try -- a truncated transfer happens -- but if the server keeps handing us the
            # same wrong bytes, the CSV's checksum is stale and no retry will fix it.
            dest.unlink(missing_ok=True)
            reason = problem
            bad_bytes = True
        else:
            part.unlink(missing_ok=True)
            bad_bytes = False
            if http_code in PERMANENT_HTTP:
                return "permanent", f"HTTP {http_code}"
            reason = f"curl exit {status}, HTTP {http_code}: {stderr}"

        if attempt < args.retries:
            # Exponential backoff with jitter: a hundred workers retrying in lockstep is how a
            # transient blip becomes a sustained outage for everyone else too.
            time.sleep(attempt * attempt * 5 + random.random() * 5)

    # A server that delivers a whole file which is still the wrong file -- a stale checksum in the
    # CSV, a bad mirror -- will deliver it again on the next attempt and on the next run. Reporting
    # that as transient costs the grid: the task exits 1, Nextflow retries it in full, and after
    # maxRetries the *whole* grid is ignored rather than the one tile. So it is a hole in the map,
    # exactly like a 404.
    if bad_bytes:
        return "permanent", f"verification failed on every attempt: {reason}"
    return "transient", f"{reason} after {args.retries} attempts"


def check_free_space(rows: list[dict[str, str]]) -> None:
    """
    Fail before spending an hour on a download that cannot fit.

    The estimate is the grid's own laz bytes plus room for karttapullautin's temporaries, which are
    comparable in size. A file of unknown size counts as the mean of the known ones; with none known
    there is nothing to estimate from.
    """
    sizes = [s for s in (parse_size(r.get("size_bytes")) for r in rows) if s is not None]
    if not sizes:
        log("no file sizes in the CSV; skipping the free-space check")
        return
    need = int(sum(sizes) / len(sizes) * len(rows) * 1.6)
    free = shutil.disk_usage(".").free
    if free < need:
        log(f"not enough free space here: need ~{need // 1024**3} GiB, have {free // 1024**3} GiB")
        log("  Lower params.grid_size or PULLAUTA_GRID's maxForks, or point workDir at a bigger "
            "volume.")
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path, required=True, help="a grid CSV from PLAN_GRIDS")
    ap.add_argument("--outdir", type=Path, default=Path("in"))
    ap.add_argument("--zipdir", type=Path, default=Path("zips"),
                    help="where .zip tiles are downloaded before unpacking; never inside --outdir")
    ap.add_argument("--failures", type=Path, default=Path("download_failures.tsv"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--limit-rate", help="curl --limit-rate value per stream, e.g. '20M'")
    args = ap.parse_args(argv)

    with args.csv.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    args.outdir.mkdir(parents=True, exist_ok=True)
    if any(is_zip(r["tile"]) for r in rows):
        args.zipdir.mkdir(parents=True, exist_ok=True)
    check_free_space(rows)
    unhashed = sum(1 for r in rows if parse_checksum(r.get("sha256")) is None)
    unsized = sum(1 for r in rows if parse_size(r.get("size_bytes")) is None)
    if unhashed:
        log(f"{unhashed} of {len(rows)} file(s) have no checksum in the CSV")
    if unsized:
        log(f"{unsized} of {len(rows)} file(s) have no size in the CSV")

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda row: fetch_one(row, args), rows))

    # Reported in CSV order rather than completion order, so the report is deterministic.
    counts: dict[str, int] = {"ok": 0, "permanent": 0, "transient": 0}
    missing_by_role: dict[str, int] = {"core": 0, "halo": 0}
    failed = []
    for row, (outcome, detail) in zip(rows, results):
        counts[outcome] += 1
        if outcome == "ok":
            continue
        failed.append([row["tile"], row["role"], outcome, detail])
        if outcome == "permanent":
            missing_by_role[row["role"]] += 1

    with args.failures.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["tile", "role", "outcome", "detail"])
        w.writerows(failed)

    log(f"{counts['ok']} verified, {counts['permanent']} permanently unavailable, "
        f"{counts['transient']} transient failures")
    if counts["transient"]:
        log(f"giving up so the grid can be retried; see {args.failures}")
        for entry in failed:
            log("  " + "\t".join(entry))
        return 1
    if missing_by_role["halo"]:
        log(f"WARNING {missing_by_role['halo']} halo tile(s) unavailable; borders next to them lose "
            "some of their 127 m context")
    if missing_by_role["core"]:
        log(f"WARNING {missing_by_role['core']} core tile(s) permanently unavailable; they will be "
            "holes in the map")
    return 0


if __name__ == "__main__":
    sys.exit(main())
