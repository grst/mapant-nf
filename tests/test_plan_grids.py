"""
Unit tests for bin/plan_grids.py.

These target the properties that are expensive to notice by looking at a finished map: a halo
that is one tile too narrow shows as a faint seam, a lon/lat envelope that is slightly too small
shows as a missing sliver, and an unstable grid id shows as a full cache miss on the next
`-resume`. All of them are cheap to assert here.

Run with:  pytest tests/
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest
from shapely.geometry import box

REPO = Path(__file__).resolve().parents[1]


def _load_plan_grids():
    """Import bin/plan_grids.py, which is a script rather than an installed package."""
    spec = importlib.util.spec_from_file_location("plan_grids", REPO / "bin" / "plan_grids.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["plan_grids"] = module
    spec.loader.exec_module(module)
    return module


pg = _load_plan_grids()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_tile(ix: int, iy: int, *, size: float = 1000.0, crs: str = "EPSG:25832", size_bytes: int = 1000) -> "pg.Tile":
    """A synthetic tile on a `size`-metre lattice at cell (ix, iy)."""
    return pg.Tile(
        tile=f"{ix}_{iy}.laz",
        url=f"https://example.invalid/{ix}_{iy}.laz",
        sha256="0" * 64,
        size_bytes=size_bytes,
        crs=crs,
        min_x=ix * size,
        min_y=iy * size,
        max_x=(ix + 1) * size,
        max_y=(iy + 1) * size,
    )


def lattice(x_range, y_range, *, size: float = 1000.0, crs: str = "EPSG:25832") -> list["pg.Tile"]:
    return [make_tile(ix, iy, size=size, crs=crs) for ix in x_range for iy in y_range]


def cell(t: "pg.Tile") -> tuple[int, int]:
    """The (ix, iy) a synthetic tile was made at, from its name."""
    ix, iy = Path(t.tile).stem.split("_")
    return int(ix), int(iy)


def block(tiles, x_range, y_range) -> list["pg.Tile"]:
    """The tiles of `tiles` whose lattice cell falls inside the given ranges."""
    return [t for t in tiles if cell(t)[0] in x_range and cell(t)[1] in y_range]


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> Path:
    import csv

    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
    return path


def tile_row(ix: int, iy: int, **overrides) -> dict:
    row = {
        "tile": f"{ix}_{iy}.laz",
        "url": f"https://example.invalid/{ix}_{iy}.laz",
        "size_bytes": 1234,
        "sha256": "a" * 64,
        "crs": "EPSG:25832",
        "min_x": ix * 1000,
        "min_y": iy * 1000,
        "max_x": (ix + 1) * 1000,
        "max_y": (iy + 1) * 1000,
    }
    row.update(overrides)
    return row


TILE_COLUMNS = ["tile", "url", "size_bytes", "sha256", "crs", "min_x", "min_y", "max_x", "max_y"]


# ---------------------------------------------------------------------------
# halo geometry -- the property the whole grid design rests on
# ---------------------------------------------------------------------------
def test_halo_is_exactly_one_ring_for_1km_tiles():
    """
    karttapullautin reads neighbours within 127 m of a tile. For 1 km tiles that is one ring and
    no more -- fetching a second ring would be pure waste, fetching none would produce seams.
    """
    # Cells 3..5 with grid_size=3 land in a single lattice block (3//3 == 5//3 == 1).
    pool = lattice(range(0, 9), range(0, 9))
    grids = pg.build_grids(
        block(pool, range(3, 6), range(3, 6)),
        pool,
        crs="EPSG:25832",
        grid_size=3,
        halo_m=127.0,
    )
    assert len(grids) == 1
    (parts,) = grids.values()
    assert len(parts["core"]) == 9
    # A 5x5 block minus the 3x3 core.
    assert len(parts["halo"]) == 16
    halo_names = {t.tile for t in parts["halo"]}
    assert "2_2.laz" in halo_names, "diagonal neighbour must be included (pullauta expands a rectangle)"
    assert "1_4.laz" not in halo_names, "second ring is 873 m away and must not be fetched"


def test_a_core_region_not_aligned_to_the_lattice_splits_across_grids():
    """
    Grid blocks are cut on absolute lattice coordinates, so a core region that does not start on a
    multiple of grid_size is split. That is the deliberate cost of resume-stable grid ids (see
    test_grid_ids_are_stable_when_the_region_filter_changes) and it is harmless -- correctness
    never depended on grid boundaries, only on the halo -- but it does mean the grid count can
    exceed ceil(area / grid_size**2), which is worth knowing when sizing a run.
    """
    pool = lattice(range(0, 9), range(0, 9))
    grids = pg.build_grids(
        block(pool, range(2, 5), range(2, 5)), pool, crs="EPSG:25832", grid_size=3, halo_m=127.0
    )
    assert len(grids) == 4
    assert sum(len(p["core"]) for p in grids.values()) == 9, "every core tile still rendered once"


def test_halo_widens_automatically_when_tiles_are_smaller_than_the_halo():
    """
    With 100 m tiles, 127 m of reach spans two rings. Nothing in the code counts rings, so this
    should just work -- which is the point of using a distance query rather than a ring count.
    """
    pool = lattice(range(0, 9), range(0, 9), size=100.0)
    core = [t for t in pool if t.tile == "4_4.laz"]
    grids = pg.build_grids(core, pool, crs="EPSG:25832", grid_size=1, halo_m=127.0)
    (parts,) = grids.values()
    halo_names = {t.tile for t in parts["halo"]}
    assert "3_3.laz" in halo_names          # first ring
    assert "2_4.laz" in halo_names          # second ring, 100 m away, still within 127
    assert "1_4.laz" not in halo_names      # third ring, 200 m away


def test_every_halo_tile_is_genuinely_within_reach_and_none_are_missed():
    """Cross-check build_grids against a brute-force distance test over the whole pool."""
    pool = lattice(range(0, 8), range(0, 8))
    # Cells 4..5 with grid_size=2 are one block (4//2 == 5//2 == 2).
    core = [t for t in pool if t.tile in {"4_4.laz", "5_4.laz", "4_5.laz", "5_5.laz"}]
    grids = pg.build_grids(core, pool, crs="EPSG:25832", grid_size=2, halo_m=127.0)
    (parts,) = grids.values()

    core_names = {t.tile for t in core}
    expected = set()
    for cand in pool:
        if cand.tile in core_names:
            continue
        for c in core:
            # pullauta's own test: the tile's box expanded by 127 m, as a rectangle.
            if (
                cand.max_x > c.min_x - 127
                and cand.min_x < c.max_x + 127
                and cand.max_y > c.min_y - 127
                and cand.min_y < c.max_y + 127
            ):
                expected.add(cand.tile)
                break
    assert {t.tile for t in parts["halo"]} == expected


def test_halo_survives_holes_and_dataset_edges():
    """A missing neighbour is simply absent; nothing raises and nothing is invented."""
    pool = [t for t in lattice(range(0, 5), range(0, 5)) if t.tile != "1_1.laz"]
    core = [t for t in pool if t.tile == "0_0.laz"]  # corner of the dataset
    grids = pg.build_grids(core, pool, crs="EPSG:25832", grid_size=1, halo_m=127.0)
    (parts,) = grids.values()
    halo_names = {t.tile for t in parts["halo"]}
    assert halo_names == {"1_0.laz", "0_1.laz"}, "the hole and the off-dataset cells are gone"


def test_halo_pool_is_global_so_a_filtered_region_renders_identically():
    """
    The regression this guards: if the halo were drawn from the region-filtered selection, a small
    test region would render its edge tiles from fewer points than a full run, and the pipeline's
    grid-independence claim would be false.
    """
    pool = lattice(range(0, 7), range(0, 7))
    core = [t for t in pool if t.tile == "3_3.laz"]

    from_full_pool = pg.build_grids(core, pool, crs="EPSG:25832", grid_size=1, halo_m=127.0)
    from_selection = pg.build_grids(core, core, crs="EPSG:25832", grid_size=1, halo_m=127.0)

    assert len(next(iter(from_full_pool.values()))["halo"]) == 8
    assert len(next(iter(from_selection.values()))["halo"]) == 0


def test_core_output_is_independent_of_grid_size():
    """
    The claim that makes grid_size a pure efficiency knob: a core tile's halo set is the same
    whatever grid it is batched into, so its render is too.
    """
    pool = lattice(range(0, 12), range(0, 12))
    core = block(pool, range(2, 8), range(2, 8))

    def reach_of(tile_name: str, grid_size: int) -> set[str]:
        grids = pg.build_grids(core, pool, crs="EPSG:25832", grid_size=grid_size, halo_m=127.0)
        for parts in grids.values():
            names = {t.tile for t in parts["core"]}
            if tile_name in names:
                available = names | {t.tile for t in parts["halo"]}
                # What pullauta can actually see for this tile: its own 127 m box.
                target = next(t for t in parts["core"] if t.tile == tile_name)
                reach = box(target.min_x - 127, target.min_y - 127, target.max_x + 127, target.max_y + 127)
                return {
                    t.tile
                    for t in pool
                    if t.tile in available and t.geometry().intersection(reach).area > 0
                }
        raise AssertionError(f"{tile_name} not in any grid")

    assert reach_of("4_4.laz", 2) == reach_of("4_4.laz", 3) == reach_of("4_4.laz", 6)


# ---------------------------------------------------------------------------
# grid identity -- the -resume property
# ---------------------------------------------------------------------------
def test_grid_ids_are_stable_when_the_region_filter_changes():
    """
    Grid ids come from absolute lattice coordinates. If they were relative to the selection's
    extent, narrowing --bbox would renumber every grid and throw away the entire resume cache --
    which, on a multi-terabyte run, is the difference between resuming and starting over.
    """
    pool = lattice(range(0, 10), range(0, 10))
    wide = block(pool, range(2, 8), range(0, 10))
    narrow = block(wide, range(4, 8), range(0, 10))

    grids_wide = pg.build_grids(wide, pool, crs="EPSG:25832", grid_size=2, halo_m=127.0)
    grids_narrow = pg.build_grids(narrow, pool, crs="EPSG:25832", grid_size=2, halo_m=127.0)

    shared = set(grids_wide) & set(grids_narrow)
    assert shared, "narrowing the region must reuse grid ids, not renumber them"
    for grid_id in shared:
        assert {t.tile for t in grids_wide[grid_id]["core"]} == {
            t.tile for t in grids_narrow[grid_id]["core"]
        }, f"{grid_id} changed membership; every task in it would re-run"


def test_grid_id_encodes_the_epsg_code():
    assert pg.grid_id_for("EPSG:25832", 590, 5268, 2) == "grid_25832_295_2634"


# ---------------------------------------------------------------------------
# projection -- the gap bug
# ---------------------------------------------------------------------------
def corner_envelope(t: "pg.Tile") -> tuple[float, float, float, float]:
    """The lon/lat envelope you get from transforming only the four corners."""
    import pyproj

    tr = pyproj.Transformer.from_crs(t.crs, pg.WGS84, always_xy=True)
    corners = [
        tr.transform(x, y)
        for x, y in ((t.min_x, t.min_y), (t.min_x, t.max_y), (t.max_x, t.min_y), (t.max_x, t.max_y))
    ]
    return (
        min(c[0] for c in corners),
        min(c[1] for c in corners),
        max(c[0] for c in corners),
        max(c[1] for c in corners),
    )


def test_derived_envelope_never_undercuts_a_four_corner_transform():
    """
    A UTM box's edges are curves in lon/lat, so transforming only the corners can *under*-estimate
    the envelope. A tile would then be assigned to too few web-mercator parents and the map would
    have thin missing slivers. This is the mistake k2t's own list_tiles makes, and the reason the
    parent map is computed here instead.

    For a 1 km tile the two agree to within floating point -- the curvature is negligible at that
    size, which is worth knowing rather than assuming. The property that must hold at every size
    is that the derived envelope is never the *smaller* of the two.
    """
    tiles = [make_tile(590, 5268)]
    pg.derive_lonlat(tiles)
    t = tiles[0]
    west, south, east, north = corner_envelope(t)

    assert t.min_lon <= west and t.min_lat <= south
    assert t.max_lon >= east and t.max_lat >= north


def test_densification_strictly_widens_a_box_wide_enough_to_curve():
    """
    Where the corner-only shortcut actually bites: a box spanning the central meridian. Northing
    is constant along its top edge, but latitude peaks at the meridian -- in the *middle* of the
    edge, where no corner samples it. Only densification finds it.
    """
    t = pg.Tile(
        tile="wide.laz",
        url="https://example.invalid/wide.laz",
        sha256="0" * 64,
        size_bytes=1,
        crs="EPSG:25832",
        min_x=300_000,
        min_y=5_900_000,
        max_x=700_000,
        max_y=6_000_000,
    )
    pg.derive_lonlat([t])
    _, _, _, corner_north = corner_envelope(t)

    assert t.max_lat > corner_north, "densification must find the mid-edge latitude maximum"
    # Not a rounding artefact: at this width the shortcut loses hundreds of metres.
    assert (t.max_lat - corner_north) > 1e-4


def test_supplied_lonlat_columns_are_not_overwritten():
    tiles = [make_tile(590, 5268)]
    tiles[0].min_lon, tiles[0].min_lat, tiles[0].max_lon, tiles[0].max_lat = 1.0, 2.0, 3.0, 4.0
    pg.derive_lonlat(tiles)
    assert (tiles[0].min_lon, tiles[0].max_lat) == (1.0, 4.0)


def test_derive_lonlat_handles_more_than_one_crs():
    """Runs before the single-CRS check, so it must not assume one transformer."""
    tiles = [make_tile(590, 5268, crs="EPSG:25832"), make_tile(300, 5268, crs="EPSG:25833")]
    pg.derive_lonlat(tiles)
    assert all(not math.isnan(t.min_lon) for t in tiles)


# ---------------------------------------------------------------------------
# web-mercator parents
# ---------------------------------------------------------------------------
def test_parents_cover_every_core_tile():
    """Union of a tile's assigned parents must contain the tile, or the map has holes."""
    import mercantile

    tiles = lattice(range(590, 594), range(5268, 5270))
    pg.derive_lonlat(tiles)
    for t in tiles:
        parents = pg.parent_tiles_for(t, 13)
        assert parents
        covered = None
        for p in parents:
            b = mercantile.bounds(p)
            g = box(b.west, b.south, b.east, b.north)
            covered = g if covered is None else covered.union(g)
        envelope = box(t.min_lon, t.min_lat, t.max_lon, t.max_lat)
        assert covered.contains(envelope) or covered.intersection(envelope).area == pytest.approx(
            envelope.area, rel=1e-9
        ), f"{t.tile} is not fully covered by its parents"


def test_parent_tiles_near_the_mercator_pole_do_not_raise():
    t = make_tile(0, 0)
    t.min_lon, t.min_lat, t.max_lon, t.max_lat = 10.0, 89.0, 10.1, 89.5
    assert pg.parent_tiles_for(t, 12) == [], "clamped away entirely, but no exception"


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------
def test_mixed_crs_is_rejected_with_a_useful_message():
    tiles = [make_tile(1, 1, crs="EPSG:25832"), make_tile(2, 2, crs="EPSG:25833")]
    with pytest.raises(pg.PlanError) as exc:
        pg.require_single_crs(tiles)
    assert "EPSG:25832" in str(exc.value) and "EPSG:25833" in str(exc.value)
    assert "not implemented" in str(exc.value)


def test_degenerate_bbox_is_rejected(tmp_path):
    csv_path = write_csv(
        tmp_path / "t.csv", [tile_row(1, 1, max_x=1000)], TILE_COLUMNS
    )  # max_x == min_x
    with pytest.raises(pg.PlanError, match="degenerate"):
        pg.read_tiles(csv_path)


def test_missing_required_column_names_what_is_missing(tmp_path):
    rows = [tile_row(1, 1)]
    del rows[0]["sha256"]
    csv_path = write_csv(tmp_path / "t.csv", rows, [c for c in TILE_COLUMNS if c != "sha256"])
    with pytest.raises(pg.PlanError, match="sha256"):
        pg.read_tiles(csv_path)


def test_min_laz_bytes_drops_from_core_but_keeps_as_halo():
    """
    A near-empty laz is the likeliest trigger of the karttapullautin crash, so it can be excluded
    from rendering -- but it is still a valid source of neighbouring points, and dropping it from
    the halo too would degrade its neighbours' renders as well.
    """
    pool = lattice(range(0, 5), range(0, 5))
    for t in pool:
        if t.tile == "2_2.laz":
            t.size_bytes = 10
    kept, dropped, too_small = pg.select_tiles(
        pool, bbox=None, bbox_crs=None, tile_regex=None, min_laz_bytes=1000
    )
    assert [t.tile for t in too_small] == ["2_2.laz"]
    assert "2_2.laz" not in {t.tile for t in kept}
    assert not dropped

    grids = pg.build_grids(
        [t for t in kept if t.tile == "2_3.laz"], pool, crs="EPSG:25832", grid_size=1, halo_m=127.0
    )
    (parts,) = grids.values()
    assert "2_2.laz" in {t.tile for t in parts["halo"]}


def test_region_filter_selecting_nothing_is_an_error():
    pool = lattice(range(0, 3), range(0, 3))
    pg.derive_lonlat(pool)
    with pytest.raises(pg.PlanError, match="selected no tiles"):
        pg.select_tiles(
            pool, bbox=(100.0, 60.0, 101.0, 61.0), bbox_crs="lonlat", tile_regex=None, min_laz_bytes=0
        )


def test_bbox_filter_keeps_tiles_that_merely_overlap():
    """A region that cuts through a tile must still render it whole, not leave a ragged edge."""
    pool = lattice(range(0, 4), range(0, 4))
    kept, _, _ = pg.select_tiles(
        pool,
        bbox=(1500.0, 1500.0, 2500.0, 2500.0),
        bbox_crs="EPSG:25832",
        tile_regex=None,
        min_laz_bytes=0,
    )
    assert {t.tile for t in kept} == {"1_1.laz", "1_2.laz", "2_1.laz", "2_2.laz"}


def test_tile_size_inference_uses_the_mode_not_the_mean():
    """One clipped tile at a dataset edge must not shift the lattice everyone else sits on."""
    tiles = lattice(range(0, 4), range(0, 4))
    tiles[0].max_x = tiles[0].min_x + 137.0  # a clipped edge tile
    w, h, uniform = pg.infer_tile_size(tiles)
    assert (w, h) == (1000.0, 1000.0)
    assert uniform is False


# ---------------------------------------------------------------------------
# the schema is the pipeline's public contract, so test it directly
# ---------------------------------------------------------------------------
SCHEMA_PATH = REPO / "assets" / "schema_tiles.json"


def _validate_row(row: dict):
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text())["items"]
    jsonschema.Draft202012Validator(schema).validate(row)


def test_schema_accepts_a_real_bavaria_row():
    _validate_row(
        {
            "tile": "498_5543.laz",
            "url": "https://geodaten.bayern.de/odd_data/laser/498_5543.laz",
            "size_bytes": 209233677,
            "sha256": "575fa8e5bb493445b0c001270bf15e47d0dd91c8eadf16600c0306f435a967c9",
            "crs": "EPSG:25832",
            "min_x": 498000,
            "min_y": 5543000,
            "max_x": 499000,
            "max_y": 5544000,
            "min_lon": 8.9720652,
            "min_lat": 50.0392942,
            "max_lon": 8.9860352,
            "max_lat": 50.0482907,
            "units": "Regierungsbezirk Unterfranken",
        }
    )


def test_schema_accepts_a_minimal_row_without_lonlat():
    _validate_row(
        {
            "tile": "a.laz",
            "url": "/data/a.laz",
            "size_bytes": 1,
            "sha256": "f" * 64,
            "crs": "EPSG:32633",
            "min_x": 0,
            "min_y": 0,
            "max_x": 1,
            "max_y": 1,
        }
    )


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"sha256": "abc"}, id="short-sha256"),
        pytest.param({"sha256": "z" * 64}, id="non-hex-sha256"),
        pytest.param({"size_bytes": 0}, id="zero-size"),
        pytest.param({"size_bytes": -5}, id="negative-size"),
        pytest.param({"crs": "25832"}, id="crs-without-epsg-prefix"),
        pytest.param({"tile": "sub/dir/a.laz"}, id="tile-with-directory"),
        pytest.param({"tile": "a.tif"}, id="tile-wrong-extension"),
        pytest.param({"url": "ftp://example.invalid/a.laz"}, id="unsupported-url-scheme"),
        pytest.param({"min_lat": 120}, id="latitude-out-of-range"),
    ],
)
def test_schema_rejects_bad_rows(bad):
    import jsonschema

    row = {
        "tile": "a.laz",
        "url": "https://example.invalid/a.laz",
        "size_bytes": 1,
        "sha256": "f" * 64,
        "crs": "EPSG:32633",
        "min_x": 0,
        "min_y": 0,
        "max_x": 1,
        "max_y": 1,
        "min_lon": 1,
        "min_lat": 1,
        "max_lon": 2,
        "max_lat": 2,
    }
    row.update(bad)
    with pytest.raises(jsonschema.ValidationError):
        _validate_row(row)


def test_schema_requires_all_four_lonlat_columns_together():
    """Half a lon/lat envelope is worse than none: it would silently be treated as complete."""
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        _validate_row(
            {
                "tile": "a.laz",
                "url": "https://example.invalid/a.laz",
                "size_bytes": 1,
                "sha256": "f" * 64,
                "crs": "EPSG:32633",
                "min_x": 0,
                "min_y": 0,
                "max_x": 1,
                "max_y": 1,
                "min_lon": 1,
            }
        )
