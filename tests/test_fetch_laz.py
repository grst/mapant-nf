"""
Test bin/fetch_laz.py's verdict on a file that arrives but is wrong.

The checksum is the pipeline's only defence against a laz file that is present and corrupt:
karttapullautin renders whatever points it can read, so a truncated or misdelivered file becomes a
plausible but wrong map tile rather than an error. What the verdict has to be is as load-bearing as
the check itself -- 'transient' fails the whole grid task, so one bad file would cost every tile
around it, while 'permanent' leaves that one tile as a recorded hole.

Nothing here touches the network: the CSV's url column may hold a plain path, which fetch_laz.py
turns into a file:// URL, so curl fetches from the temporary directory.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "fetch_laz.py"

_spec = importlib.util.spec_from_file_location("fetch_laz", SCRIPT)
fetch_laz = importlib.util.module_from_spec(_spec)
sys.modules["fetch_laz"] = fetch_laz
_spec.loader.exec_module(fetch_laz)

GOOD = b"the bytes the checksum in the CSV was computed from\n"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()


def write_source(tmp_path: Path, name: str, payload: bytes) -> Path:
    src = tmp_path / "server" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(payload)
    return src


def row_for(src: Path, *, size: int, sha256: str, role: str = "core") -> dict[str, str]:
    return {
        "tile": src.name,
        # A bare path rather than a URL: the schema allows it and fetch_laz.py resolves it to file://.
        "url": str(src),
        "size_bytes": str(size),
        "sha256": sha256,
        "role": role,
    }


def run_fetch(tmp_path: Path, rows: list[dict[str, str]], *, retries: int = 1) -> list[list[str]]:
    """Run fetch_laz.py's main() over `rows` and return the rows of its failures TSV."""
    csv_path = tmp_path / "grid.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    failures = tmp_path / "download_failures.tsv"
    status = fetch_laz.main([
        "--csv", str(csv_path),
        "--outdir", str(tmp_path / "in"),
        "--failures", str(failures),
        "--retries", str(retries),
    ])
    with failures.open(newline="") as fh:
        reported = list(csv.reader(fh, delimiter="\t"))
    return [status, *reported[1:]]


def test_a_file_that_matches_is_accepted(tmp_path: Path) -> None:
    src = write_source(tmp_path, "600_5300.laz", GOOD)
    status, *failures = run_fetch(tmp_path, [row_for(src, size=len(GOOD), sha256=GOOD_SHA)])

    assert status == 0
    assert failures == []
    assert (tmp_path / "in" / "600_5300.laz").read_bytes() == GOOD


@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    [
        (GOOD[:10], "size mismatch"),
        (b"x" * len(GOOD), "sha256 mismatch"),
    ],
    ids=["truncated", "wrong bytes of the right length"],
)
def test_a_file_that_never_verifies_is_permanent_not_transient(
    tmp_path: Path, payload: bytes, expected_detail: str
) -> None:
    """
    The verdict this test exists for.

    A server handing over a complete file that is not the file the CSV describes -- a stale checksum,
    a bad mirror -- will hand over the same bytes on the next attempt and on the next run. Reporting
    that as transient makes fetch_laz.py exit 1, which fails the PULLAUTA_GRID task, exhausts its
    retries and loses the *whole* grid instead of the one tile. tests/test_failure_injection.sh
    checks the same thing end to end.
    """
    src = write_source(tmp_path, "600_5300.laz", payload)
    status, *failures = run_fetch(tmp_path, [row_for(src, size=len(GOOD), sha256=GOOD_SHA)])

    # Exit 0: a permanent failure is a hole in the map, not a reason to retry the grid.
    assert status == 0
    assert len(failures) == 1
    tile, role, outcome, detail = failures[0]
    assert (tile, role, outcome) == ("600_5300.laz", "core", "permanent")
    assert expected_detail in detail
    assert "verification" in detail
    # And the bad copy must not be left where karttapullautin would read it.
    assert not (tmp_path / "in" / "600_5300.laz").exists()


def test_a_file_that_is_simply_absent_is_transient(tmp_path: Path) -> None:
    """
    The other half of the distinction: nothing arrived, so a retry is worth having.

    curl reports a missing file:// path as exit 37, which is not one of the settled HTTP answers, so
    the grid task exits 1 and Nextflow runs it again.
    """
    src = tmp_path / "server" / "600_5300.laz"
    src.parent.mkdir(exist_ok=True)
    status, *failures = run_fetch(tmp_path, [row_for(src, size=len(GOOD), sha256=GOOD_SHA)])

    assert status == 1
    assert [f[2] for f in failures] == ["transient"]
