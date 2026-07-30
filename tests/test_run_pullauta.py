"""
Test bin/run_pullauta.py's recovery from a renderer that panics.

"Some tiles may fail due to a karttapullautin bug; that must not stop the pipeline, and the tile
coordinates and error messages must be collected" is a requirement that is very easy to believe you
have implemented and never actually exercise -- the offending tile is by definition one you do not
have. So the renderer is stubbed (tests/stub_pullauta) to reproduce karttapullautin's failure
behaviour exactly, and the recovery logic is tested against it.
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "bin" / "run_pullauta.py"
STUB = REPO / "tests" / "stub_pullauta"
IEND = bytes.fromhex("49454e44ae426082")


class Grid:
    """A case directory: a grid CSV, empty stand-ins for the laz files, and the stub on PATH."""

    def __init__(self, path: Path):
        self.path = path
        bin_dir = path / "bin"
        bin_dir.mkdir()
        shutil.copy(STUB, bin_dir / "pullauta")
        (bin_dir / "pullauta").chmod(0o755)
        self.env = os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        self.out = path / "out"

    def setup(self, core: list[str], halo: tuple[str, ...] = ()) -> None:
        (self.path / "in").mkdir()
        shutil.copy(REPO / "assets" / "pullauta.ini", self.path / "effective.ini")
        with (self.path / "grid.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                "tile url sha256 size_bytes role crs min_x min_y max_x max_y".split()
            )
            for role, tiles in (("core", core), ("halo", halo)):
                for t in tiles:
                    w.writerow(
                        [f"{t}.laz", f"https://example.invalid/{t}.laz", "0" * 64, 1000, role,
                         "EPSG:25832", 609000, 5285000, 610000, 5286000]
                    )
                    (self.path / "in" / f"{t}.laz").touch()

    def run(self, processes: int = 2, **stub_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT),
             "--grid-id", "test_grid",
             "--csv", "grid.csv",
             "--ini", "effective.ini",
             "--processes", str(processes),
             "--max-attempts", "6",
             "--variant", "depr",
             "--log", "pullauta.log",
             "--failures", "failures.tsv"],
            cwd=self.path, env=self.env | stub_env, capture_output=True, text=True,
        )

    def rendered(self) -> list[str]:
        return sorted(p.name.removesuffix("_depr.pgw") for p in self.out.glob("*_depr.pgw"))

    def failures(self) -> list[dict[str, str]]:
        with (self.path / "failures.tsv").open(newline="") as fh:
            return list(csv.DictReader(fh, delimiter="\t"))

    def log(self) -> str:
        return (self.path / "pullauta.log").read_text()


@pytest.fixture
def grid(tmp_path):
    return Grid(tmp_path)


def test_every_core_tile_is_rendered_and_halo_tiles_are_not(grid):
    """
    The halo placeholder trick is the thing to verify. Halo tiles are in the input folder so their
    points are available, but they must not be rendered: at grid_size 10 the ring is 36% of the
    folder, and rendering it would be pure waste.
    """
    grid.setup(["a1", "a2"], ("h1", "h2"))
    proc = grid.run()

    assert proc.returncode == 0
    assert grid.rendered() == ["a1", "a2"]
    assert grid.failures() == []
    assert not list(grid.out.glob("h[12].png")), "halo placeholders were left behind"


def test_a_tile_that_panics_is_recorded_and_skipped(grid):
    grid.setup(["a1", "a2", "a3"], ("h1",))
    proc = grid.run(STUB_CRASH_TILES="a2")

    assert proc.returncode == 0
    assert grid.rendered() == ["a1", "a3"]
    (failure,) = grid.failures()
    assert failure["tile"] == "a2"
    assert "panicked at" in failure["panic_message"]
    assert "could not read LAZ points" in failure["log_tail"]
    assert not (grid.out / "a2.png").exists(), "the blacklist placeholder was left behind"
    # A zero-byte PNG in out/ would be published as a corrupt map tile.
    assert not [p for p in grid.out.glob("*.png") if p.stat().st_size == 0]


def test_two_panicking_tiles_are_isolated_one_at_a_time(grid):
    grid.setup(["a1", "a2", "a3", "a4"])
    proc = grid.run(STUB_CRASH_TILES="a2 a4")

    assert proc.returncode == 0
    assert grid.rendered() == ["a1", "a3"]
    assert [f["tile"] for f in grid.failures()] == ["a2", "a4"]


def test_a_tile_abandoned_mid_write_is_re_rendered(grid):
    """
    karttapullautin's resume logic only checks that <t>.png exists, so a tile whose write was
    interrupted by a sibling's panic would be skipped forever and shipped corrupt. The recovery code
    has to notice the missing IEND chunk and delete the quartet so it gets redone.
    """
    grid.setup(["a1", "a2", "a3"])
    proc = grid.run(STUB_CRASH_TILES="a2", STUB_TRUNCATE="a3")

    assert proc.returncode == 0
    assert "a3" in grid.rendered()
    # The _depr variant, because the plain one is pruned at the end (params.png_variant).
    assert (grid.out / "a3_depr.png").read_bytes().endswith(IEND)
    assert "quarantined" in proc.stderr


def test_a_failure_before_any_tile_starts_is_not_blamed_on_a_tile(grid):
    """
    A bad ini or an unreadable shapefile archive is an environment problem. Recording it against
    whichever tile happened to be next would hide a real fault and corrupt the bug report, so the
    script must fail the task and let Nextflow retry it.
    """
    grid.setup(["a1", "a2"])
    proc = grid.run(STUB_FAIL_EARLY="1")

    assert proc.returncode == 101
    assert grid.failures() == []


def test_a_grid_where_everything_panics_fails_rather_than_emitting_nothing(grid):
    grid.setup(["a1", "a2"])
    proc = grid.run(STUB_CRASH_TILES="a1 a2")

    assert proc.returncode == 1
    assert [f["tile"] for f in grid.failures()] == ["a1", "a2"]


def test_a_core_tile_whose_laz_never_arrived_is_a_recorded_hole(grid):
    grid.setup(["a1", "a2"])
    (grid.path / "in" / "a2.laz").unlink()
    proc = grid.run()

    assert proc.returncode == 0
    assert grid.rendered() == ["a1"]
    (failure,) = grid.failures()
    assert failure["tile"] == "a2"
    assert "laz file unavailable" in failure["reason"]


def test_resuming_a_partly_finished_grid_re_renders_only_what_is_missing(grid):
    """
    This is karttapullautin's own skip-if-output-exists behaviour, which the pipeline leans on for
    both retries and the blacklist. If it broke, a retried grid would redo hours of work.
    """
    grid.setup(["a1", "a2", "a3"])
    grid.run()
    for stale in grid.out.glob("a2*"):
        stale.unlink()

    proc = grid.run()

    assert proc.returncode == 0
    assert grid.rendered() == ["a1", "a2", "a3"]
    assert grid.log().count("in/a2.laz ->") == 1
