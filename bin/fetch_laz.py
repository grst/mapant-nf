#!/usr/bin/env python3
"""
Acquire every laz file a grid needs into ./in, and prove each one arrived intact.

Checksums are not optional here. A truncated laz does not make karttapullautin fail -- it renders
whatever points it managed to read, so the damage surfaces as a plausible but wrong map tile. So
every file is verified on every attempt, including files taken from --local-dir.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# curl --fail turns all of these into exit 22; they are settled answers, not bad luck.
PERMANENT_HTTP = {"400", "401", "403", "404", "410", "451"}

USER_AGENT = "mapant/1.0 (+https://github.com/grst/mapant) nextflow pipeline"


def log(message: str) -> None:
    print(f"fetch_laz.py: {message}", file=sys.stderr)


def verify(path: Path, size: int, sha256: str) -> str | None:
    """Return None if the file matches, else a description of the mismatch."""
    if not path.is_file():
        return "missing"
    # Size first: it is free, and it catches the common truncation case without hashing hundreds of
    # megabytes to find out.
    actual_size = path.stat().st_size
    if actual_size != size:
        return f"size mismatch (expected {size}, got {actual_size})"
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(4 << 20):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        return f"sha256 mismatch (expected {sha256}, got {digest.hexdigest()})"
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
    tile, size, sha = row["tile"], int(row["size_bytes"]), row["sha256"].lower()
    dest = args.outdir / tile

    # Already there and intact? That happens on a Nextflow retry of the same task.
    if dest.exists() and verify(dest, size, sha) is None:
        return "ok", "cached"
    dest.unlink(missing_ok=True)

    # A local copy short-circuits the network but not the verification: the fixture path in the test
    # profiles has to exercise the same checks the real one does. Symlink rather than copy -- a grid
    # is tens of gigabytes, and the caller's `rm -rf in/` removes the links, not the source.
    local = args.local_dir / tile if args.local_dir else None
    if local is not None and local.exists():
        dest.symlink_to(local.resolve())
        problem = verify(dest, size, sha)
        if problem is None:
            return "ok", "local"
        dest.unlink()
        return "permanent", f"local copy failed verification: {problem}"

    # The schema permits a bare path as well as a URL; curl needs a scheme.
    url = row["url"]
    if "://" not in url:
        url = Path(url).resolve().as_uri()

    part = dest.with_name(dest.name + ".part")
    reason = "no attempt made"
    for attempt in range(1, args.retries + 1):
        status, http_code, stderr = curl(url, part, args.limit_rate)
        if status == 0:
            part.replace(dest)
            problem = verify(dest, size, sha)
            if problem is None:
                return "ok", "downloaded"
            # One more try -- a truncated transfer happens -- but if the server keeps handing us the
            # same wrong bytes, the CSV's checksum is stale and no retry will fix it.
            dest.unlink(missing_ok=True)
            reason = problem
        else:
            part.unlink(missing_ok=True)
            if http_code in PERMANENT_HTTP:
                return "permanent", f"HTTP {http_code}"
            reason = f"curl exit {status}, HTTP {http_code}: {stderr}"

        if attempt < args.retries:
            # Exponential backoff with jitter: a hundred workers retrying in lockstep is how a
            # transient blip becomes a sustained outage for everyone else too.
            time.sleep(attempt * attempt * 5 + random.random() * 5)

    return "transient", f"{reason} after {args.retries} attempts"


def check_free_space(rows: list[dict[str, str]]) -> None:
    """
    Fail before spending an hour on a download that cannot fit.

    The estimate is the grid's own laz bytes plus room for karttapullautin's temporaries, which are
    comparable in size.
    """
    need = int(sum(int(r["size_bytes"]) for r in rows) * 1.6)
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
    ap.add_argument("--failures", type=Path, default=Path("download_failures.tsv"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--local-dir", type=Path, help="use files found here instead of downloading")
    ap.add_argument("--limit-rate", help="curl --limit-rate value per stream, e.g. '20M'")
    args = ap.parse_args(argv)

    with args.csv.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    args.outdir.mkdir(parents=True, exist_ok=True)
    check_free_space(rows)

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
