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
import gzip
import json
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
        # The key bin/render_ini.py owns unconditionally: every render this pipeline does is a
        # vector render, so the stub is always asked for one.
        ini = self.path / "effective.ini"
        if "vectorvege = 1" not in ini.read_text():
            ini.write_text(ini.read_text() + "\nvectorvege = 1\n")
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
        """
        The tiles that came out, which is the bundles: the images are deleted once they are made.
        """
        return sorted(p.name.removesuffix("_vec") for p in self.out.glob("*_vec"))

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
    assert not (grid.out / "h1_vec").exists(), "a halo tile was rendered"


def test_a_tile_that_panics_is_recorded_and_skipped(grid):
    grid.setup(["a1", "a2", "a3"], ("h1",))
    proc = grid.run(STUB_CRASH_TILES="a2")

    assert proc.returncode == 0
    assert grid.rendered() == ["a1", "a3"]
    (failure,) = grid.failures()
    assert failure["tile"] == "a2"
    assert "panicked at" in failure["panic_message"]
    assert "could not read LAZ points" in failure["log_tail"]
    # The placeholder that blacklisted a2 is a zero-byte PNG in out/. Nothing may survive that has
    # not been through a full render: no bundle for a2, and no leftover image of any kind.
    assert not (grid.out / "a2_vec").exists()
    assert not list(grid.out.glob("*.png"))


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


def test_a_later_attempt_re_renders_only_what_is_missing(grid):
    """
    karttapullautin skips a file whose output image already exists, which is what makes the attempt
    ladder affordable: a panic on the fifth tile of a hundred must not cost the four before it. The
    same behaviour is what the halo placeholders and the blacklist exploit.

    Within one invocation, which is the only place it is relied on -- a Nextflow retry gets a fresh
    work directory, and the images are deleted once the bundles are made.
    """
    grid.setup(["a1", "a2", "a3"])

    proc = grid.run(processes=1, STUB_CRASH_TILES="a2")

    assert proc.returncode == 0
    assert grid.rendered() == ["a1", "a3"]
    assert grid.log().count("in/a1.laz ->") == 1, "a1 was rendered again after the panic on a2"


def test_vector_output_is_bundled_per_tile_and_gzipped(grid):
    """
    The bundle is what the vector fan-in groups by parent, so its shape is a contract: one
    directory per core tile holding each layer's GeoJSON compressed, under the name karttapullautin
    gave it, and nothing of it left loose in out/ where the publish step would pick it up.
    """
    grid.setup(["a1", "a2"], ("h1",))
    proc = grid.run()

    assert proc.returncode == 0, proc.stderr
    for stem in ("a1", "a2"):
        bundle = grid.out / f"{stem}_vec"
        assert bundle.is_dir()
        layers = ("cliffs", "contours", "dotknolls", "formlines", "osm_areas", "osm_lines",
                  "undergrowth", "vegetation", "yellow")
        assert sorted(p.name for p in bundle.iterdir()) == [
            f"{stem}_{layer}.geojson.gz" for layer in layers
        ]
        with gzip.open(bundle / f"{stem}_contours.geojson.gz", "rt") as fh:
            assert json.load(fh)["type"] == "FeatureCollection"

    assert not list(grid.out.glob("*.geojson")), "uncompressed GeoJSON left in out/"
    # The halo tile is only there for its points, so it gets no bundle either.
    assert not (grid.out / "h1_vec").exists()


def test_the_rendered_images_do_not_survive_the_bundle(grid):
    """
    Nothing reads them since the pyramid became vector-only, and at 1.5 MB a tile they would be a
    hundred gigabytes of work directories across Bavaria. They cannot go any earlier than this: an
    image closed with an IEND chunk is how this script knows a tile finished, and how
    karttapullautin knows not to render it again on a retry.
    """
    grid.setup(["a1"])

    proc = grid.run()

    assert proc.returncode == 0, proc.stderr
    assert grid.rendered() == ["a1"]
    assert not list(grid.out.glob("*.png"))
    assert not list(grid.out.glob("*.pgw"))
    # ...while the tile is still counted as rendered, which is what keeps a good grid from failing.
    assert "1 tile(s) rendered" in proc.stderr


def test_the_grid_crs_reaches_the_renderer_as_epsg(grid):
    """
    karttapullautin reprojects its GeoJSON to WGS84 from `epsg`, which is per grid and so cannot be
    in the shared ini: without it every coordinate would be read in the wrong system.
    """
    grid.setup(["a1"])
    proc = grid.run()

    assert proc.returncode == 0, proc.stderr
    assert "epsg = 25832" in (grid.path / "pullauta.ini").read_text()


def test_a_grid_with_two_crss_is_refused(grid):
    """One `epsg` per render: a grid straddling two zones would be reprojected wrongly, not fail."""
    grid.setup(["a1", "a2"])
    csv_path = grid.path / "grid.csv"
    lines = csv_path.read_text().splitlines()
    lines[-1] = lines[-1].replace("EPSG:25832", "EPSG:25833")
    csv_path.write_text("\n".join(lines) + "\n")

    proc = grid.run()

    assert proc.returncode != 0
    assert "exactly one EPSG CRS" in proc.stderr
