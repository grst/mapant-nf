"""
Test bin/make_vector_tiles.py and the style that draws its tiles.

The tiler no longer touches the data -- karttapullautin's per-tile GeoJSON goes to tippecanoe as it
is -- so what is left to get wrong is what it tells tippecanoe: which file is which layer, what each
zoom may show, and which tiles are its own. Each of those produces a plausible-looking map rather
than an error when it is wrong: a layer missing, a zoom whose contents depend on which parent cut
it, a tile published by two tasks at once.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
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


def style_module():
    """bin/make_vector_style.py, loaded the way the tiler is loaded above."""
    spec = importlib.util.spec_from_file_location("mvs", REPO / "bin" / "make_vector_style.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: a dataclass in the module needs to find its own module
    # while its fields are being resolved.
    sys.modules["mvs"] = module
    spec.loader.exec_module(module)
    return module


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

    command = mvt.tippecanoe_command(mvt.layer_files(tmp_path), tmp_path / "out", PARENT, 16, 8)

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


def test_the_style_draws_only_layers_the_tiles_have():
    """A style layer naming a source layer the tiles do not carry draws nothing, silently."""
    mvs = style_module()
    style = mvs.build_style(
        tiles_url="tiles/{z}/{x}/{y}.pbf", base_zoom=13, max_zoom=16,
        bounds=(10.0, 47.0, 11.0, 48.0), latitude=47.5, ini={}, title="t",
    )
    used = {l["source-layer"] for l in style["layers"] if "source-layer" in l}
    assert used <= {layer.name for layer in mvt.LAYERS}
    # and every property a filter reads is one karttapullautin writes
    text = json.dumps(style["layers"])
    assert '["get", "c"]' not in text and '["get", "k"]' not in text


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

    assert mvt.main(["--parent", "13", "4329", "2862", "--max-zoom", "14",
                     str(tmp_path / "in"), str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "13" / "4329" / "2862.pbf").is_file()


def test_only_the_parents_own_subtree_survives(tmp_path):
    """
    --clip-bounding-box clips geometry but still writes a neighbouring tile whose buffer reaches
    into the parent. Left in place, two tasks would publish the same path with different contents.
    """
    parent = mercantile.Tile(x=4329, y=2862, z=13)
    out = tmp_path / "tiles_vector"
    for z, x, y in [
        (13, 4329, 2862),  # the parent
        (14, 8658, 5724),  # inside it
        (16, 34633, 22898),  # deeper inside it
        (13, 4330, 2862),  # a neighbour
        (14, 8660, 5724),  # inside the neighbour
        (12, 2164, 1431),  # above the parent zoom
    ]:
        path = out / str(z) / str(x)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{y}.pbf").write_bytes(b"x")
    (out / "metadata.json").write_text("{}")

    kept, pruned = mvt.prune_foreign_tiles(out, parent)

    assert (kept, pruned) == (3, 3)
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*.pbf")) == [
        "13/4329/2862.pbf",
        "14/8658/5724.pbf",
        "16/34633/22898.pbf",
    ]
    # Per-parent metadata would collide in the published pyramid.
    assert not (out / "metadata.json").exists()


def test_the_brush_is_wider_than_its_nominal_width():
    """
    `draw_curves` strokes with a square brush offset from -curvew-0.5 to +curvew+0.5, so a curvew
    of 2 covers five render pixels. Taking curvew for the width is what makes every line on a
    re-implementation come out too thin.
    """
    mvs = style_module()

    assert mvs.brush_metres(2.0) == pytest.approx(5 * 254 / 600)
    assert mvs.brush_metres(3.5) == pytest.approx(8 * 254 / 600)
    # And the green ramp is palette.rs's, so the tiles' class indices mean what the style says.
    assert mvs.green_shades(11, 200)[0] == (200, 254, 200)
    assert mvs.green_shades(11, 200)[-1] == (0, 180, 0)


def test_a_shape_is_drawn_as_the_isom_symbol_it_was_matched_to():
    """
    A dash pattern in the style spec is measured in multiples of the line width, so the widths and
    the dashes of a symbol have to be divided before they can be written down -- and the widths
    are millimetres of paper, which is ten metres of ground at 1:10 000.
    """
    mvs = style_module()

    # ISOM 504, vehicle track: a 0.525 mm line with 4.5 mm dashes and 0.375 mm gaps.
    track = mvs.ISOM_LINES["505"]
    assert track.symbol == "504"
    assert track.width_mm * mvs.METRES_PER_MM == pytest.approx(5.25)
    assert mvs.dash_array(track) == [pytest.approx(8.571, abs=1e-3), pytest.approx(0.714, abs=1e-3)]

    # ISOM 502, wide road: brown between two black edges, so the casing is the wider of the two.
    road = mvs.ISOM_LINES["503"]
    assert road.casing_mm > road.width_mm
    assert road.color == mvs.ISOM_BROWN_50

    # A bridge or tunnel section carries the same code with a T, and is the same symbol.
    assert mvs.codes_for("503") == ["503", "503T"]
