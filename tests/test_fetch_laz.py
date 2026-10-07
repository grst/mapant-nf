"""
Test bin/fetch_laz.py's verdict on a file that arrives but is wrong.

Verification -- the checksum where the CSV has one, CRC-32 for a .zip's members -- is the
pipeline's only defence against a laz file that is present and corrupt:
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
import zipfile
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
        "--zipdir", str(tmp_path / "zips"),
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
        (GOOD[:10], "sha256 mismatch"),
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


# ---------------------------------------------------------------------------
# no checksum in the CSV
# ---------------------------------------------------------------------------
def test_a_file_without_a_checksum_is_accepted(tmp_path: Path) -> None:
    src = write_source(tmp_path, "600_5300.laz", GOOD)
    status, *failures = run_fetch(tmp_path, [row_for(src, size=len(GOOD), sha256="")])

    assert status == 0
    assert failures == []
    assert (tmp_path / "in" / "600_5300.laz").read_bytes() == GOOD


@pytest.mark.parametrize("sha256", [GOOD_SHA, ""], ids=["with-checksum", "without-checksum"])
def test_a_misreported_size_is_not_held_against_the_file(tmp_path: Path, sha256: str) -> None:
    """Sources misreport sizes; the checksum, where there is one, is what decides."""
    src = write_source(tmp_path, "600_5300.laz", GOOD)
    status, *failures = run_fetch(tmp_path, [row_for(src, size=len(GOOD) + 1, sha256=sha256)])

    assert (status, failures) == (0, [])
    assert (tmp_path / "in" / "600_5300.laz").read_bytes() == GOOD


# ---------------------------------------------------------------------------
# .zip tiles
# ---------------------------------------------------------------------------
def make_zip(members: dict[str, bytes]) -> bytes:
    """An archive of `members`, stored uncompressed so a test can corrupt a member's bytes."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def zip_row(src: Path, payload: bytes, *, with_sha: bool = True) -> dict[str, str]:
    return row_for(src, size=len(payload),
                   sha256=hashlib.sha256(payload).hexdigest() if with_sha else "")


@pytest.mark.parametrize("with_sha", [True, False], ids=["checksum", "no-checksum"])
def test_a_zip_is_unpacked_under_the_tiles_stem(tmp_path: Path, with_sha: bool) -> None:
    """
    The laz lands as <tile stem>.laz whatever the source called it -- run_pullauta.py and the
    tiler know a tile only by that stem -- and nothing but it is left in in/: karttapullautin unzips
    every archive in its input folder, and would take this one for a shapefile set.
    """
    payload = make_zip({"inner_name.laz": GOOD, "inner_name_meta.csv": b"a,b\n"})
    src = write_source(tmp_path, "lsc_33430_5640_2_sn_laz.zip", payload)
    status, *failures = run_fetch(tmp_path, [zip_row(src, payload, with_sha=with_sha)])

    assert status == 0
    assert failures == []
    assert sorted(p.name for p in (tmp_path / "in").iterdir()) == ["lsc_33430_5640_2_sn_laz.laz"]
    assert (tmp_path / "in" / "lsc_33430_5640_2_sn_laz.laz").read_bytes() == GOOD
    assert list((tmp_path / "zips").iterdir()) == []


def test_a_zip_keeps_the_members_suffix(tmp_path: Path) -> None:
    payload = make_zip({"sub/dir/TILE.LAS": GOOD})
    src = write_source(tmp_path, "600_5300.zip", payload)
    status, *failures = run_fetch(tmp_path, [zip_row(src, payload)])

    assert (status, failures) == (0, [])
    assert (tmp_path / "in" / "600_5300.las").read_bytes() == GOOD


@pytest.mark.parametrize(
    ("members", "expected_detail"),
    [
        ({"readme.txt": b"no point cloud here"}, "holds 0 .laz/.las files"),
        ({"a.laz": GOOD, "b.laz": GOOD}, "holds 2 .laz/.las files"),
    ],
    ids=["no-laz", "two-laz"],
)
def test_a_zip_without_exactly_one_laz_is_permanent(
    tmp_path: Path, members: dict[str, bytes], expected_detail: str
) -> None:
    payload = make_zip(members)
    src = write_source(tmp_path, "600_5300.zip", payload)
    status, *failures = run_fetch(tmp_path, [zip_row(src, payload)])

    assert status == 0
    assert len(failures) == 1
    assert failures[0][2] == "permanent"
    assert expected_detail in failures[0][3]
    assert list((tmp_path / "in").iterdir()) == []
    assert list((tmp_path / "zips").iterdir()) == []


def test_a_corrupt_member_is_caught_by_its_crc_without_a_checksum(tmp_path: Path) -> None:
    """
    The archive's size is right and the CSV has no checksum, so only the member's CRC-32 can tell
    -- and a half-written laz must not be left behind for karttapullautin.
    """
    payload = bytearray(make_zip({"t.laz": GOOD}))
    offset = bytes(payload).index(GOOD)
    payload[offset] ^= 0xFF
    payload = bytes(payload)
    src = write_source(tmp_path, "600_5300.zip", payload)
    status, *failures = run_fetch(tmp_path, [zip_row(src, payload, with_sha=False)])

    assert status == 0
    assert len(failures) == 1
    assert failures[0][2] == "permanent"
    assert "unreadable archive" in failures[0][3]
    assert list((tmp_path / "in").iterdir()) == []


# ---------------------------------------------------------------------------
# checksum algorithms and unknown sizes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "checksum",
    [
        "sha1:" + hashlib.sha1(GOOD).hexdigest(),
        "sha256:" + GOOD_SHA,
        "SHA1:" + hashlib.sha1(GOOD).hexdigest().upper(),
    ],
    ids=["sha1", "sha256-prefixed", "upper-case"],
)
def test_a_prefixed_checksum_names_its_algorithm(tmp_path: Path, checksum: str) -> None:
    src = write_source(tmp_path, "600_5300.laz", GOOD)
    status, *failures = run_fetch(tmp_path, [row_for(src, size=len(GOOD), sha256=checksum)])

    assert (status, failures) == (0, [])


def test_a_wrong_sha1_is_permanent(tmp_path: Path) -> None:
    payload = b"x" * len(GOOD)
    src = write_source(tmp_path, "600_5300.laz", payload)
    checksum = "sha1:" + hashlib.sha1(GOOD).hexdigest()
    status, *failures = run_fetch(tmp_path, [row_for(src, size=len(GOOD), sha256=checksum)])

    assert status == 0
    assert [(f[2], "sha1 mismatch" in f[3]) for f in failures] == [("permanent", True)]


@pytest.mark.parametrize(
    "value",
    ["abc", "md5:" + "0" * 32, "sha1:" + "0" * 64, "sha256:" + "0" * 40, "z" * 64],
)
def test_a_malformed_checksum_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="not a checksum"):
        fetch_laz.parse_checksum(value)


def test_a_file_of_unknown_size_is_checked_by_its_checksum(tmp_path: Path) -> None:
    src = write_source(tmp_path, "600_5300.laz", GOOD)
    row = row_for(src, size=len(GOOD), sha256=GOOD_SHA) | {"size_bytes": ""}
    status, *failures = run_fetch(tmp_path, [row])
    assert (status, failures) == (0, [])

    # A fresh directory, or the verified copy from the first run would be reused.
    src.write_bytes(GOOD[:10])
    (tmp_path / "again").mkdir()
    status, *failures = run_fetch(tmp_path / "again", [row])
    assert [(f[2], "sha256 mismatch" in f[3]) for f in failures] == [("permanent", True)]


def test_a_file_with_neither_size_nor_checksum_is_accepted(tmp_path: Path) -> None:
    """Nothing to check it against but curl's own Content-Length check."""
    src = write_source(tmp_path, "600_5300.laz", GOOD)
    row = row_for(src, size=len(GOOD), sha256="") | {"size_bytes": ""}
    status, *failures = run_fetch(tmp_path, [row])

    assert (status, failures) == (0, [])
    assert (tmp_path / "in" / "600_5300.laz").read_bytes() == GOOD
