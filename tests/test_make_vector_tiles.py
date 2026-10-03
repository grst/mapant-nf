"""
Test bin/make_vector_tiles.py.

The tiler sorts karttapullautin's features into isom-maplibre's tables, gives each its ISOM 2017-2
`isom_code` and decides the zooms it is drawn at. Every one of those produces a plausible-looking
map rather than an error when it is wrong: a symbol the style never draws because it is in the
wrong table or spelt `"403"` instead of `"403.000"`, or a zoom whose contents depend on which
parent cut it.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import mercantile
import pytest

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("mvt", REPO / "bin" / "make_vector_tiles.py")
mvt = importlib.util.module_from_spec(spec)
sys.modules["mvt"] = mvt
spec.loader.exec_module(mvt)

CROSSWALK = mvt.read_crosswalk(REPO / "assets" / "isom2000-isom2017-2.crt")
LAYER = {layer.name: layer for layer in mvt.LAYERS}


def feature(isom: str, geometry_type: str = "LineString", **properties) -> dict:
    coordinates = {"Point": [10.25, 47.56], "LineString": [[10.25, 47.56], [10.26, 47.57]],
                   "Polygon": [[[10.25, 47.56], [10.26, 47.56], [10.26, 47.57], [10.25, 47.56]]]}
    return {"type": "Feature", "properties": {"isom": isom, **properties},
            "geometry": {"type": geometry_type, "coordinates": coordinates[geometry_type]}}


def classify(layer: str, isom: str, geometry_type: str = "LineString") -> tuple[str, str]:
    return mvt.Classifier(CROSSWALK).classify(LAYER[layer], feature(isom, geometry_type))


def write_bundle(root: Path, stem: str, layers: dict[str, list[dict] | bytes]) -> Path:
    bundle = root / f"{stem}_vec"
    bundle.mkdir(parents=True)
    for layer, content in layers.items():
        if isinstance(content, list):
            content = gzip.compress(json.dumps({"type": "FeatureCollection", "features": content}).encode())
        (bundle / f"{stem}_{layer}.geojson.gz").write_bytes(content)
    return bundle


def write_parent_tiles(path: Path, tiles: dict[str, tuple[float, float, float, float]]) -> Path:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tile", "parent", "z", "x", "y", "crs", "n_core", "min_x", "min_y", "max_x", "max_y"])
        for stem, box in tiles.items():
            w.writerow([stem, "13_4329_2862", 13, 4329, 2862, "EPSG:25832", len(tiles), *box])
    return path


def test_the_terrain_keeps_its_number_in_the_style_spelling_and_its_table():
    assert classify("vegetation", "406", "Polygon") == ("vegetation_areas", "406.000")
    assert classify("yellow", "403", "Polygon") == ("vegetation_areas", "403.000")
    assert classify("undergrowth", "409", "Polygon") == ("vegetation_areas", "409.000")
    assert classify("contours", "102") == ("contours", "102.000")
    assert classify("formlines", "103") == ("contours", "103.000")
    assert classify("dotknolls", "111", "Point") == ("knolls_points", "111.000")
    assert classify("cliffs", "201") == ("cliffs", "201.000")


def test_the_osm_shapes_are_translated_from_iso_2000_by_the_crosswalk():
    """The pairs the webapp's bridge used to translate at load time (HANDOFF-isom-maplibre.md)."""
    assert classify("osm_areas", "301", "Polygon") == ("water", "301.000")
    assert classify("osm_lines", "301.1") == ("water", "301.000")  # the bank, as a line
    assert classify("osm_lines", "306") == ("water", "305.000")
    assert classify("osm_areas", "310", "Polygon") == ("water", "308.000")
    assert classify("osm_areas", "401", "Polygon") == ("vegetation_areas", "401.000")
    assert classify("osm_lines", "401.1") == ("vegetation_areas", "415.000")
    assert classify("osm_lines", "414") == ("vegetation_areas", "415.000")
    assert classify("osm_lines", "503") == ("paths", "502.000")
    assert classify("osm_lines", "503T") == ("paths", "502.000")  # a bridge is the road
    assert classify("osm_lines", "504") == ("paths", "503.000")
    assert classify("osm_lines", "505") == ("paths", "504.000")
    assert classify("osm_lines", "507") == ("paths", "506.000")
    assert classify("osm_lines", "515") == ("manmade", "509.000")
    assert classify("osm_lines", "516") == ("manmade", "510.000")
    assert classify("osm_lines", "524") == ("manmade", "518.000")
    assert classify("osm_areas", "526", "Polygon") == ("manmade", "521.000")
    assert classify("osm_areas", "527", "Polygon") == ("manmade", "520.000")
    assert classify("osm_areas", "529", "Polygon") == ("manmade", "501.000")


def test_any_code_a_rules_file_may_use_is_translated_not_only_mapant_s():
    """The crosswalk is keyed on ISOM 2000, so a region with rules of its own still maps."""
    assert classify("osm_lines", "517") == ("manmade", "511.000")  # major power line
    assert classify("osm_areas", "302", "Polygon") == ("water", "301.000")  # a pond is a lake now


def test_a_code_nobody_translated_keeps_its_number_and_is_reported():
    classifier = mvt.Classifier(CROSSWALK)
    assert classifier.classify(LAYER["osm_lines"], feature("777")) == ("manmade", "777.000")
    assert classifier.unknown == {"777": 1}


def test_what_a_zoom_shows_is_a_property_of_the_feature():
    """
    The zoom a feature first appears at depends on its own `isom` and table alone, never on what
    else is in the tile -- which is what keeps two parents that cut the same zoom in agreement.
    """
    plan = mvt.ZoomPlan(base=13, max=15)

    def minzoom(layer: str, isom: str, geometry_type: str = "LineString") -> int:
        table, _ = classify(layer, isom, geometry_type)
        return plan.minzoom(LAYER[layer].shown_from_of(isom), table)

    assert plan.overview == 12
    assert minzoom("vegetation", "410", "Polygon") == 12
    assert minzoom("osm_lines", "503") == 12  # the road network is how you find yourself
    assert minzoom("osm_lines", "503T") == 12  # and a bridge is part of the road
    assert minzoom("osm_areas", "526", "Polygon") == 14  # a building is not, at a kilometre a tile
    assert minzoom("osm_lines", "999") == 14  # a code the table has never heard of
    assert minzoom("formlines", "103") == 15
    assert minzoom("dotknolls", "109", "Point") == 15
    assert minzoom("contours", "101") == 14


def test_small_roads_show_a_zoom_before_the_tracks():
    plan = mvt.ZoomPlan(base=11, max=15)
    for code in ("504", "504T"):
        assert plan.minzoom(LAYER["osm_lines"].shown_from_of(code), "paths") == 11
    for code in ("505", "505T"):
        assert plan.minzoom(LAYER["osm_lines"].shown_from_of(code), "paths") == 12
    assert plan.minzoom(LAYER["osm_lines"].shown_from_of("503"), "paths") == 10


def test_shown_from_counts_back_from_the_deepest_zoom():
    plan = mvt.ZoomPlan(base=11, max=15)
    assert plan.minzoom(0, "manmade") == 15
    assert plan.minzoom(-1, "manmade") == 14
    assert plan.minzoom(-5, "manmade") == 10
    assert plan.minzoom(-9, "manmade") == 10  # never above the overview
    assert plan.minzoom(mvt.ALL_ZOOMS, "contours") == 11  # nor contours on it


def square(x0, y0, size, hole=None):
    """A square in the tile's UTM coordinates, as a WGS84 Polygon feature of `vegetation`."""
    tr = mvt.pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

    def ring(x, y, s):
        pts = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
        return [list(tr.transform(px, py)) for px, py in pts]

    rings = [ring(x0, y0, size)] + ([ring(*hole)] if hole else [])
    return {"type": "Feature", "properties": {"isom": "406", "layer": "406"},
            "geometry": {"type": "Polygon", "coordinates": rings}}


WEST = (593000, 5269000, 594000, 5270000)
EAST = (594000, 5269000, 595000, 5270000)


def first_zooms(boxes, pieces, min_px=16):
    """{name: first zoom} of (name, stem, square args) pieces, all of one symbol."""
    small = mvt.SmallAreas(min_px, 11, {stem: ("EPSG:25832", *box) for stem, box in boxes.items()})
    for name, stem, args in pieces:
        small.add(name, stem, "vegetation:406", square(*args)["geometry"])
    return {name: small.first_zoom(name) for name, _, _ in pieces}


def test_a_patch_appears_from_the_zoom_it_covers_enough_pixels_at():
    # a z11 pixel is ~26 m here: 16 px is ~1.1 ha at z11, ~4.3 ha at z10, ~0.27 ha at z12
    sizes = {"6.25 ha": 250, "2.25 ha": 150, "0.64 ha": 80, "0.16 ha": 40, "0.04 ha": 20}
    pieces = [(name, "593_5269", (593100 + 300 * (k % 3), 5269100 + 300 * (k // 3), size))
              for k, (name, size) in enumerate(sizes.items())]
    assert first_zooms({"593_5269": WEST}, pieces) == {
        "6.25 ha": None,  # on the overview already
        "2.25 ha": 11, "0.64 ha": 12, "0.16 ha": 13, "0.04 ha": 14}
    # holes count against the area
    assert first_zooms({"593_5269": WEST}, [("ring", "593_5269", (593400, 5269400, 150, (593410, 5269410, 130)))]) \
        == {"ring": 12}
    # 0 switches the rule off
    assert first_zooms({"593_5269": WEST}, [("p", "593_5269", (593400, 5269400, 10))], min_px=0) == {"p": None}


def test_a_patch_cut_by_a_tile_edge_is_measured_whole():
    # 1 ha either side of the shared edge: one 2 ha patch, shown from z11 on both sides (alone,
    # either half would wait for z12)
    halves = [("west", "593_5269", (593900, 5269400, 100)),
              ("east", "594_5269", (594000, 5269400, 100))]
    assert first_zooms({"593_5269": WEST, "594_5269": EAST}, halves) == {"west": 11, "east": 11}
    # a forest across the edge: neither piece is judged by its own small share
    forest = [("west", "593_5269", (593900, 5269300, 100)), ("east", "594_5269", (594000, 5269000, 1000))]
    assert first_zooms({"593_5269": WEST, "594_5269": EAST}, forest) == {"west": None, "east": None}


def test_a_patch_cut_where_this_parent_cannot_see_is_not_judged():
    # the eastern tile is not in this parent: the piece may go on there
    assert first_zooms({"593_5269": WEST}, [("west", "593_5269", (593950, 5269400, 50))]) == {"west": None}
    # a piece merely near the edge, not cut by it, is judged
    assert first_zooms({"593_5269": WEST}, [("near", "593_5269", (593940, 5269400, 50))]) == {"near": 13}


def test_write_tables_shows_every_patch_from_one_zoom_above_the_deepest(tmp_path):
    box = (593000, 5269000, 594000, 5270000)
    write_bundle(tmp_path / "in", "593_5269", {
        "vegetation": [square(593400, 5269400, 80), square(593400, 5269600, 300), square(593600, 5269600, 5)]})
    parent_tiles = write_parent_tiles(tmp_path / "p.csv", {"593_5269": box})
    small = mvt.SmallAreas(16, 11, mvt.tile_boxes(parent_tiles, {"593_5269"}))
    tables = mvt.write_tables(mvt.bundle_files(tmp_path / "in"), [], mvt.ZoomPlan(11, 15),
                              mvt.Classifier(CROSSWALK), tmp_path / "t", small)
    (_, path, _), = tables
    assert [json.loads(line)["tippecanoe"]["minzoom"] for line in path.read_text().splitlines()] == [12, 10, 14]

def test_the_overview_level_has_no_contour_lines():
    plan = mvt.ZoomPlan(base=13, max=15)
    # index contours are on every zoom of the pyramid -- except the overview
    assert plan.minzoom(mvt.ALL_ZOOMS, "contours") == 13
    assert plan.minzoom(mvt.ALL_ZOOMS, "water") == 12


def test_style_codes():
    assert mvt.style_code("403") == "403.000"
    assert mvt.style_code("101.1") == "101.001"
    assert mvt.style_code("521.1") == "521.001"
    assert mvt.style_code("301.4") == "301.000"
    assert mvt.style_code("weird") == "weird"


def test_every_feature_gets_its_table_code_and_minzoom_and_the_coverage_its_tiles(tmp_path):
    write_bundle(tmp_path / "in", "593_5269", {
        "vegetation": [feature("406", "Polygon", layer="406")],
        "contours": [feature("101", layer="contour", elevation=700.0)],
        "osm_lines": [feature("503", layer="503", category="road-path")],
        "cliffs": b"",  # the stub's placeholder
    })
    parent_tiles = write_parent_tiles(tmp_path / "p.csv", {
        "593_5269": (593000, 5269000, 594000, 5270000),
        "594_5269": (594000, 5269000, 595000, 5270000),  # not rendered here: not covered
    })
    files = mvt.bundle_files(tmp_path / "in")
    assert [(stem, layer.name) for stem, layer, _ in files] == [
        ("593_5269", "vegetation"), ("593_5269", "contours"), ("593_5269", "osm_lines")]

    tables = mvt.write_tables(files, mvt.footprints(parent_tiles, {"593_5269"}),
                              mvt.ZoomPlan(13, 15), mvt.Classifier(CROSSWALK), tmp_path / "t")
    assert [(t, n) for t, _, n in tables] == [
        ("coverage", 1), ("vegetation_areas", 1), ("contours", 1), ("paths", 1)]
    read = {t: [json.loads(line) for line in path.read_text().splitlines()] for t, path, _ in tables}
    (road,) = read["paths"]
    assert road["properties"] == {"isom": "503", "layer": "503", "category": "road-path",
                                  "isom_code": "502.000"}
    assert road["tippecanoe"] == {"minzoom": 12}
    assert read["contours"][0]["tippecanoe"] == {"minzoom": 14}
    (paper,) = read["coverage"]
    assert paper["properties"] == {"tile": "593_5269"} and paper["tippecanoe"] == {"minzoom": 12}


def test_neighbouring_footprints_share_their_edge_point_for_point(tmp_path):
    """Two squares that meet must leave no hairline of background between them."""
    parent_tiles = write_parent_tiles(tmp_path / "p.csv", {
        "593_5269": (593000, 5269000, 594000, 5270000),
        "594_5269": (594000, 5269000, 595000, 5270000),
    })
    west, east = mvt.footprints(parent_tiles, {"593_5269", "594_5269"})
    west_ring = {tuple(p) for p in west["geometry"]["coordinates"][0]}
    east_ring = {tuple(p) for p in east["geometry"]["coordinates"][0]}
    shared = west_ring & east_ring
    assert len(shared) == 21  # one edge, densified to 20 segments


def test_tippecanoe_decides_nothing_by_size_and_cuts_512_px_tiles(tmp_path):
    """
    Every one of tippecanoe's size-driven decisions is made per tile from what happens to be in
    it, which is exactly the dependency the per-feature zooms exist to remove.
    """
    parent = mercantile.Tile(x=4329, y=2862, z=13)
    tables = [("contours", tmp_path / "contours.geojsonl", 1), ("paths", tmp_path / "paths.geojsonl", 1)]
    command = mvt.tippecanoe_command(tables, tmp_path / "out.pmtiles", parent, 15, 4)

    assert f"--named-layer=contours:{tmp_path / 'contours.geojsonl'}" in command
    assert "--minimum-zoom=12" in command and "--maximum-zoom=15" in command
    assert "--full-detail=13" in command and "--low-detail=13" in command
    assert "--no-tile-size-limit" in command
    assert "--no-feature-limit" in command
    assert "--drop-rate=1" in command
    assert not [flag for flag in command if "drop-densest" in flag or "as-needed" in flag]
    assert "--detect-shared-borders" in command
    assert "--simplify-only-low-zooms" in command  # the deepest zoom is drawn overzoomed


@pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="needs tippecanoe")
def test_tippecanoe_accepts_the_command(tmp_path):
    """The flags and the per-feature zooms, against the real thing: a typo here is a failed task."""
    write_bundle(tmp_path / "in", "593_5269", {
        "contours": [feature("102", layer="contour_index", elevation=700.0)],
        "osm_areas": [feature("301", "Polygon", layer="301", category="lake")],
    })
    parent_tiles = write_parent_tiles(tmp_path / "p.csv", {"593_5269": (593000, 5269000, 594000, 5270000)})
    archive = tmp_path / "13-4329-2862.pmtiles"
    assert mvt.main(["--parent", "13", "4329", "2862", "--max-zoom", "14",
                     "--crosswalk", str(REPO / "assets" / "isom2000-isom2017-2.crt"),
                     "--parent-tiles", str(parent_tiles), "--work-dir", str(tmp_path / "t"),
                     str(tmp_path / "in"), str(archive)]) == 0
    assert archive.read_bytes()[:7] == b"PMTiles"
