"""
Test bin/make_vector_tiles.py.

The tiler does not touch the data -- karttapullautin's per-tile GeoJSON goes to tippecanoe as it
is -- so what is left to get wrong is what it tells tippecanoe: which file is which layer and what
each zoom may show. Both produce a plausible-looking map rather than an error when they are wrong:
a layer missing, or a zoom whose contents depend on which parent cut it.
"""

from __future__ import annotations

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

PARENT = mercantile.Tile(x=4329, y=2862, z=13)


def shown(expression: list | None, zoom: int, properties: dict) -> bool:
    """Evaluate the subset of tippecanoe's feature-filter language the tiler writes."""
    if expression is None:
        return True
    op, *args = expression
    if op == "any":
        return any(shown(a, zoom, properties) for a in args)
    if op == "all":
        return all(shown(a, zoom, properties) for a in args)
    if op == ">=":
        assert args[0] == "$zoom"
        return zoom >= args[1]
    if op in ("in", "!in"):
        found = properties.get(args[0]) in args[1:]
        return found if op == "in" else not found
    raise AssertionError(f"unexpected operator {op}")


def minzoom(layer: str, **properties) -> int:
    """The first zoom of a z13-16 pyramid that draws such a feature."""
    plan = mvt.ZoomPlan(base=13, max=16)
    (spec,) = [l for l in mvt.LAYERS if l.name == layer]
    expression = plan.feature_filter(spec)
    return next(z for z in range(13, 17) if shown(expression, z, properties))


def write_bundle(root: Path, stem: str, layers: dict[str, bytes]) -> Path:
    bundle = root / f"{stem}_vec"
    bundle.mkdir(parents=True)
    for layer, content in layers.items():
        (bundle / f"{stem}_{layer}.geojson.gz").write_bytes(content)
    return bundle


def test_every_file_goes_to_the_layer_its_name_says_in_drawing_order(tmp_path):
    """
    The layer order is the compositing order, so the vegetation must come before the contours
    whatever order the bundles list their files in; and an empty file -- the stub's placeholder --
    is not handed to tippecanoe, which cannot parse it.
    """
    write_bundle(tmp_path, "b", {"osm_lines": b"x", "contours": b"x", "vegetation": b"x"})
    write_bundle(tmp_path, "a", {"contours": b"x", "cliffs": b""})

    files = mvt.layer_files(tmp_path)

    assert [(layer, path.name) for layer, path in files] == [
        ("vegetation", "b_vegetation.geojson.gz"),
        ("contours", "a_contours.geojson.gz"),
        ("contours", "b_contours.geojson.gz"),
        ("osm_lines", "b_osm_lines.geojson.gz"),
    ]


def test_what_a_zoom_shows_is_a_property_of_the_feature():
    """
    The zoom a feature first appears at depends on its own `isom` alone, never on what else is
    in the tile -- which is what keeps two parents that cut the same zoom in agreement.
    """
    assert minzoom("contours", isom="102") == 13  # index contours carry the shape of the ground
    assert minzoom("contours", isom="101") == 15
    assert minzoom("vegetation", isom="410") == 13
    assert minzoom("formlines", isom="103") == 16
    assert minzoom("dotknolls", isom="109") == 16
    assert minzoom("osm_lines", isom="503") == 13  # the road network is how you find yourself
    assert minzoom("osm_lines", isom="503T") == 13  # and a bridge is part of the road
    assert minzoom("osm_areas", isom="526") == 15  # a building is not, at a kilometre a tile
    assert minzoom("osm_lines", isom="999") == 15  # a code the table has never heard of


def test_a_layer_drawn_at_every_zoom_needs_no_filter():
    plan = mvt.ZoomPlan(base=13, max=16)
    (vegetation,) = [l for l in mvt.LAYERS if l.name == "vegetation"]
    assert plan.feature_filter(vegetation) is None
    # and a pyramid of one zoom filters nothing at all
    single = mvt.ZoomPlan(base=16, max=16)
    assert all(single.feature_filter(layer) is None for layer in mvt.LAYERS)


def test_tippecanoe_gets_the_files_as_they_are_and_decides_nothing_by_size(tmp_path):
    """
    Every one of tippecanoe's size-driven decisions is made per tile from what happens to be in
    it, which is exactly the dependency the zoom plan exists to remove. And the files are named on
    the command line, untouched: there is no intermediate copy to diverge from what
    karttapullautin wrote.
    """
    contours = write_bundle(tmp_path, "a", {"contours": b"x"}) / "a_contours.geojson.gz"

    command = mvt.tippecanoe_command(
        mvt.layer_files(tmp_path), tmp_path / "out.pmtiles", PARENT, 16, 8
    )

    assert f"--named-layer=contours:{contours}" in command
    assert "--no-tile-size-limit" in command
    assert "--no-feature-limit" in command
    assert "--drop-rate=1" in command
    assert not [flag for flag in command if "drop-densest" in flag or "as-needed" in flag]
    assert "--detect-shared-borders" in command
    (filters,) = [f for f in command if f.startswith("--feature-filter=")]
    assert set(json.loads(filters.split("=", 1)[1])) == {
        layer.name for layer in mvt.LAYERS
        if mvt.ZoomPlan(base=13, max=16).feature_filter(layer) is not None
    }


@pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="needs tippecanoe")
def test_tippecanoe_accepts_the_command(tmp_path):
    """The feature filter and the flags, against the real thing: a typo here is a failed task."""
    import gzip

    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"layer": "contour_index", "isom": "102",
                                                "elevation": 700.0},
             "geometry": {"type": "LineString",
                          "coordinates": [[10.245, 47.55], [10.275, 47.57]]}},
        ],
    }
    write_bundle(tmp_path / "in", "a", {"contours": gzip.compress(json.dumps(collection).encode())})

    archive = tmp_path / "13-4329-2862.pmtiles"
    assert mvt.main(["--parent", "13", "4329", "2862", "--max-zoom", "14",
                     str(tmp_path / "in"), str(archive)]) == 0
    assert archive.read_bytes()[:7] == b"PMTiles"
