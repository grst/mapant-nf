"""
Test bin/make_vector_tiles.py's routing, tracing, zoom plan and pruning.

The parts worth testing here are the ones where being wrong produces a plausible-looking map rather
than an error: a class routed into the wrong layer, form line candidates drawn twice, a shape
reaching past the data it belongs to, a tile published by two tasks at once, white paper showing
between two shades of green, or a zoom whose contents depend on which parent cut it.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import mercantile
import numpy as np
import pytest
import shapely
from PIL import Image

REPO = Path(__file__).resolve().parent.parent

#: The tile the fixtures describe: one square kilometre in EPSG:25832.
TILE_BBOX = [593000.0, 5269000.0, 594000.0, 5270000.0]

spec = importlib.util.spec_from_file_location("mvt", REPO / "bin" / "make_vector_tiles.py")
mvt = importlib.util.module_from_spec(spec)
sys.modules["mvt"] = mvt
spec.loader.exec_module(mvt)


def style_module():
    """bin/make_vector_style.py, loaded the way the tiler is loaded above."""
    spec = importlib.util.spec_from_file_location("mvs", REPO / "bin" / "make_vector_style.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: a dataclass in the module needs to find its own module
    # while its fields are being resolved.
    sys.modules["mvs"] = module
    spec.loader.exec_module(module)
    return module


def feature(geometry: dict, **properties) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def line(*points) -> dict:
    return {"type": "LineString", "coordinates": [list(p) for p in points]}


@pytest.fixture
def bundle(tmp_path):
    """A tile bundle in the shape run_pullauta.py leaves behind."""

    def build(
        features: list[dict],
        rasters: dict[str, np.ndarray] | None = None,
        pixel: float = 1.0,
    ) -> Path:
        directory = tmp_path / "593_5269_vec"
        directory.mkdir(exist_ok=True)
        with gzip.open(directory / "593_5269.geojson.gz", "wt") as fh:
            # The bbox is the tile karttapullautin cropped its own layers to, and what the shapes
            # get cropped to here.
            json.dump({"type": "FeatureCollection", "bbox": TILE_BBOX, "features": features}, fh)
        for name, array in (rasters or {}).items():
            Image.fromarray(array, mode="L").save(directory / f"593_5269_{name}.png")
            # Origin at the north-west pixel's centre, as karttapullautin writes it. The pixel is a
            # metre for three of the four rasters and 254/600 m for the undergrowth one.
            (directory / f"593_5269_{name}.pgw").write_text(
                f"{pixel}\n0.0\n0.0\n-{pixel}\n{593000 + pixel / 2}\n{5270000 - pixel / 2}\n"
            )
        return tmp_path

    return build


def plan(base: int = 13, maximum: int = 16) -> mvt.ZoomPlan:
    """The zooms the fixtures are cut to: the square-kilometre grid's base zoom, four levels."""
    return mvt.ZoomPlan(base=base, max=maximum, latitude=47.56)


def route(
    in_dir: Path,
    formline_mode: float = 2.0,
    zoom_plan: mvt.ZoomPlan | None = None,
    osm: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """
    Run the routing stage alone and return the features written per layer.

    `osm` stands in for what osm_shapes.py writes: one matched shape per line, for the whole parent
    rather than per tile, which is why the tiler clips it to the union of the tiles' own bounds.
    """
    from pyproj import Transformer

    zoom_plan = zoom_plan or plan()
    transformer = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
    work = in_dir / "layers"
    work.mkdir(exist_ok=True)

    files = {}
    handles = []
    for layer in mvt.LAYERS:
        path = work / f"{layer.name}.geojsonl"
        handle = path.open("w")
        handles.append(handle)
        files[layer.name] = mvt.LayerFile(layer=layer, path=path, handle=handle)
    try:
        covered = []
        for directory in sorted(p for p in in_dir.glob("*_vec") if p.is_dir()):
            tile_box = mvt.route_features(directory, files, transformer, formline_mode, zoom_plan)
            if tile_box is not None:
                covered.append(tile_box)
            mvt.route_rasters(directory, files, transformer, zoom_plan)

        if osm is not None:
            path = work / "osm.geojsonl"
            path.write_text("".join(json.dumps(f) + "\n" for f in osm))
            region = shapely.union_all(covered) if covered else None
            mvt.route_osm(path, files, transformer, zoom_plan, region)
    finally:
        for handle in handles:
            handle.close()

    return {
        name: [json.loads(line) for line in f.path.read_text().splitlines() if line]
        for name, f in files.items()
    }


def test_each_class_goes_to_the_layer_that_draws_it(bundle):
    in_dir = bundle(
        [
            feature(line((593000, 5269000), (593100, 5269100)), layer="contour", elevation=800.0),
            feature(line((593000, 5269000), (593100, 5269100)), layer="contour_index"),
            feature(line((593000, 5269000), (593010, 5269010)), layer="cliff3"),
            feature(line((593000, 5269000), (593010, 5269010)), layer="formline"),
            feature({"type": "Point", "coordinates": [593500, 5269500]}, layer="dotknoll"),
        ]
    )
    layers = route(in_dir)

    assert [f["properties"]["k"] for f in layers["contours"]] == ["contour", "contour_index"]
    assert layers["contours"][0]["properties"]["e"] == 800.0
    assert [f["properties"]["k"] for f in layers["cliffs"]] == ["cliff3"]
    assert [f["properties"]["k"] for f in layers["formlines"]] == ["formline"]
    assert [f["properties"]["k"] for f in layers["knolls"]] == ["dotknoll"]
    assert layers["knolls"][0]["geometry"]["type"] == "Point"


def test_formline_candidates_are_dropped_unless_no_formlines_were_generated(bundle):
    """
    `render.rs` draws the intermed classes filtered by slope and writes the survivors as formlines,
    so with formline=2 they are already on the map: routing them into the contour layer as well is
    what put nearly twice as many lines on an earlier attempt at this.
    """
    in_dir = bundle(
        [feature(line((593000, 5269000), (593100, 5269100)), layer="contour_intermed")]
    )

    assert route(in_dir, formline_mode=2.0)["contours"] == []
    # With formlines switched off, nothing else represents them, so they are the contour.
    assert len(route(in_dir, formline_mode=0.0)["contours"]) == 1


def test_shapes_split_by_where_the_map_composites_them(bundle):
    shapes = [
        feature(
            {
                "type": "Polygon",
                "coordinates": [
                    [[593000, 5269000], [593100, 5269000], [593100, 5269100], [593000, 5269000]]
                ],
            },
            layer="osm",
            isom="401",
            osm_id="1",
        ),
        feature(line((593000, 5269000), (593100, 5269100)), layer="osm", isom="503", osm_id="2"),
    ]
    layers = route(bundle([]), osm=shapes)

    # 401 is an area fill, which goes under the contours; a road goes over the top.
    assert [f["properties"]["isom"] for f in layers["osm_low"]] == ["401"]
    assert [f["properties"]["isom"] for f in layers["osm_high"]] == ["503"]


def test_shapes_stop_where_the_lidar_does(bundle, tmp_path):
    """
    The OSM extract covers a whole grid plus its buffer, and it arrives once for the parent rather
    than once per tile. What bounds it is the ground that was actually rendered -- the union of the
    tiles' own bounds -- because a road drawn past the last rendered square kilometre would be map
    over terrain the map does not describe.
    """
    # A road running well past both ends of the tile, and one entirely outside it.
    crossing = feature(
        line((592000, 5269500), (595000, 5269500)), layer="osm", isom="503", osm_id="42"
    )
    elsewhere = feature(
        line((596000, 5269500), (597000, 5269500)), layer="osm", isom="503", osm_id="43"
    )
    in_dir = bundle([])

    features = route(in_dir, osm=[crossing, elsewhere])["osm_high"]

    assert len(features) == 1
    lons = [point[0] for point in features[0]["geometry"]["coordinates"]]
    # 593000-594000 in EPSG:25832 is about 10.239-10.252 degrees east.
    assert min(lons) > 10.23
    assert max(lons) < 10.26

    # A second rendered tile extends the ground, so more of the same road survives -- as one
    # feature, not one per tile, which is what the per-tile crop used to produce.
    second = tmp_path / "594_5269_vec"
    second.mkdir()
    with gzip.open(second / "594_5269.geojson.gz", "wt") as fh:
        json.dump(
            {
                "type": "FeatureCollection",
                "bbox": [594000.0, 5269000.0, 595000.0, 5270000.0],
                "features": [],
            },
            fh,
        )

    (extended,) = route(in_dir, osm=[crossing, elsewhere])["osm_high"]
    assert max(point[0] for point in extended["geometry"]["coordinates"]) > 10.26


def test_classified_rasters_are_traced_per_class(bundle):
    # Two vegetation classes side by side, and a row of background that must not become a polygon.
    array = np.array([[0, 0, 0], [1, 1, 2], [1, 1, 2]], dtype=np.uint8)
    in_dir = bundle([], rasters={"vege_bit": array})

    features = route(in_dir)["vegetation"]
    deepest = [f for f in features if f["tippecanoe"]["minzoom"] == 16]

    assert sorted(f["properties"]["c"] for f in deepest) == [1, 2]
    assert all(f["geometry"]["type"] == "Polygon" for f in deepest)
    # Traced in the source CRS and reprojected, so the ring must land near the tile in lon/lat.
    lon, lat = deepest[0]["geometry"]["coordinates"][0][0]
    assert 10.2 < lon < 10.3
    assert 47.5 < lat < 47.6


def test_a_world_file_gives_the_centre_of_its_first_pixel_not_its_corner(tmp_path):
    """
    Half a metre, and the whole raster sits half a pixel south-east of the vectors drawn over the
    same ground -- and half a pixel over the neighbouring tile, whose polygons then overlap these.
    """
    pgw = tmp_path / "tile.pgw"
    pgw.write_text("1.0\n0.0\n0.0\n-1.0\n593000.5\n5269999.5\n")

    assert mvt.read_world_file(pgw) == (1.0, -1.0, 593000.0, 5270000.0)


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


def test_a_border_between_two_classes_belongs_to_both(bundle):
    """
    The bug this guards against is white paper showing through between two shades of green.

    karttapullautin's vegetation is a raster of class numbers, so two shades meet along a pixel
    edge that is part of both polygons. Simplifying each polygon on its own -- which is what
    `Polygon.simplify` does -- moves that edge twice, once per side, and the two no longer meet.
    The coverage simplifier moves a shared edge once, for both sides.
    """
    import rasterio.transform
    from shapely.ops import unary_union

    # Two classes meeting along a staircase, which is the shape every boundary in a traced raster
    # has and the one simplification is most tempted by.
    array = np.array(
        [
            [1, 1, 1, 2, 2, 2],
            [1, 1, 2, 2, 2, 2],
            [1, 1, 1, 2, 2, 2],
            [1, 2, 2, 2, 2, 2],
            [1, 1, 1, 1, 2, 2],
            [1, 1, 1, 2, 2, 2],
        ],
        dtype=np.uint8,
    )
    transform = rasterio.transform.Affine(1.0, 0.0, 0.0, 0.0, -1.0, 6.0)

    traced = dict(mvt.traced_areas(array, transform, mvt.SIMPLIFY_PIXELS))
    light, dark = traced[1], traced[2]

    # Nothing between them and nothing over the top: the two classes partition the ground.
    assert light.intersection(dark).area == 0
    assert unary_union([light, dark]).area == pytest.approx(float(array.size))
    shared = light.exterior.intersection(dark.exterior)
    assert shared.length == pytest.approx(14.0)

    # Simplifying each polygon on its own instead loses part of that shared edge, which is the
    # gap. Asserted here so the reason for the coverage simplifier stays on the record.
    apart = light.simplify(mvt.SIMPLIFY_PIXELS).exterior.intersection(
        dark.simplify(mvt.SIMPLIFY_PIXELS).exterior
    )
    assert apart.length < shared.length

    # A tolerance far coarser than the pixel still only drops vertices, never moves them: every
    # coordinate is still a pixel corner. That is what keeps the polygons of two neighbouring
    # square kilometres meeting along the tile border they share.
    coarse = dict(mvt.traced_areas(array, transform, 3.0))
    assert coarse[1].intersection(coarse[2]).area == 0
    for polygon in coarse.values():
        for x, y in polygon.exterior.coords:
            assert (x, y) == (round(x), round(y))


def test_generalising_the_raster_keeps_the_classes_a_partition():
    """
    Every zoom below the deepest is traced from a coarser grid rather than from simplified
    polygons, so the classes still tile the ground exactly and nothing opens up between them.
    """
    array = np.array(
        [[1, 1, 0, 0], [1, 0, 0, 0], [2, 2, 2, 0], [2, 2, 0, 0]],
        dtype=np.uint8,
    )

    assert mvt.downsample_mode(array, 2).tolist() == [[1, 0], [2, 0]]
    # A block split evenly keeps its class rather than dissolving into paper: a feature the map
    # is generalising should thin out, not blink out.
    assert mvt.downsample_mode(np.array([[2, 0], [0, 2]], dtype=np.uint8), 2).tolist() == [[2]]
    # Sizes that do not divide are padded, not truncated.
    assert mvt.downsample_mode(array, 3).shape == (2, 2)


def test_the_zoom_a_pixel_is_merged_to_follows_the_screen_it_is_drawn_on():
    """One raster pixel per screen pixel, give or take a power of two, and none at all deeper."""
    zoom_plan = plan()

    assert zoom_plan.generalisation(16, 1.0) == 1  # the deepest zoom is the render itself
    assert zoom_plan.generalisation(15, 1.0) == 4
    assert zoom_plan.generalisation(14, 1.0) == 8
    assert zoom_plan.generalisation(13, 1.0) == 16
    # A 4 m raster at a zoom that shows 4.8 m per pixel is already as coarse as it needs to be.
    assert zoom_plan.generalisation(15, 4.0) == 1


def test_what_a_zoom_shows_is_a_property_of_the_feature(bundle):
    """
    Every feature carries the zoom it appears at, because the alternative -- letting tippecanoe
    drop whatever does not fit the tile -- makes the answer depend on which parent cut the tile.
    Two parents then disagree along the border they share: a lake drawn on one side and missing on
    the other, which is what this pins down.
    """
    in_dir = bundle(
        [
            feature(line((593000, 5269000), (593100, 5269100)), layer="contour_index"),
            feature(line((593000, 5269000), (593100, 5269100)), layer="contour"),
            feature(line((593000, 5269000), (593010, 5269010)), layer="formline"),
            feature({"type": "Point", "coordinates": [593500, 5269500]}, layer="dotknoll"),
        ]
    )
    shapes = [
        feature(line((593000, 5269000), (593100, 5269100)), layer="osm", isom="503", osm_id="1"),
        feature(
            {
                "type": "Polygon",
                "coordinates": [
                    [[593000, 5269000], [593100, 5269000], [593100, 5269100], [593000, 5269000]]
                ],
            },
            layer="osm",
            isom="526",
            osm_id="2",
        ),
    ]
    layers = route(in_dir, osm=shapes)

    def minzoom(name: str, key: str, value: str) -> int | None:
        for f in layers[name]:
            if f["properties"].get(key) == value:
                # No hint at all means "every zoom this parent was cut to".
                return f.get("tippecanoe", {}).get("minzoom", 13)
        raise AssertionError(f"no {key}={value} in {name}")

    assert minzoom("contours", "k", "contour_index") == 13  # the shape of the ground, everywhere
    assert minzoom("contours", "k", "contour") == 15
    assert minzoom("formlines", "k", "formline") == 16  # only where a tile is a few hundred metres
    assert minzoom("knolls", "k", "dotknoll") == 16
    assert minzoom("osm_high", "isom", "503") == 13  # the road network is how you find yourself
    assert minzoom("osm_high", "isom", "526") == 15  # a building is not, at a kilometre a tile


def test_the_cliff_hatching_is_thinned_by_where_a_tick_is_not_by_what_else_is_in_the_tile():
    """
    Well over a hundred thousand two-point ticks per square kilometre is a texture, and drawing all of
    it on an overview tile is neither useful nor affordable. Sampling it by a hash of the tick's
    own position makes the sample the same whichever parent cuts the tile, and nested, so a tick
    on an overview tile is on the deeper ones too.
    """
    ticks = [[[593000.0 + i * 0.7, 5269000.0], [593000.5 + i * 0.7, 5269000.4]] for i in range(4000)]
    levels = [mvt.texture_levels(tick) for tick in ticks]

    assert set(levels) == {0, 1, 2}
    # Roughly one in sixteen survives each zoom out, and each survivor of the deeper cut is a
    # survivor of the shallower one.
    assert 0.03 < sum(l >= 1 for l in levels) / len(levels) < 0.10
    assert sum(l >= 2 for l in levels) < sum(l >= 1 for l in levels)
    # And the decision travels with the tick: same coordinates, same answer, whatever order the
    # bundles were read in.
    assert [mvt.texture_levels(tick) for tick in reversed(ticks)] == list(reversed(levels))


def test_tippecanoe_is_not_allowed_to_decide_what_fits(tmp_path, monkeypatch):
    """
    Every one of tippecanoe's size-driven decisions is made per tile from what happens to be in
    it, which is exactly the dependency the zoom plan exists to remove.
    """
    files = [mvt.LayerFile(layer=mvt.LAYERS[0], path=tmp_path / "vegetation.geojsonl", handle=None)]
    recorded = []
    monkeypatch.setattr(mvt.subprocess, "run", lambda command, check: recorded.append(command))

    mvt.run_tippecanoe(files, tmp_path / "out", mercantile.Tile(x=4329, y=2862, z=13), 16, 8)

    command = recorded[0]
    assert "--no-tile-size-limit" in command
    assert "--no-feature-limit" in command
    assert "--drop-rate=1" in command
    assert not [flag for flag in command if "drop-densest" in flag or "as-needed" in flag]


def test_a_raster_is_traced_on_its_own_grid_not_on_a_metre_one(bundle):
    """
    The four classified rasters are not on one grid: vegetation, water and blocks are 1 m per
    pixel, and undergrowth is drawn at render resolution, 254/600 m. Assuming metres puts a tile's
    undergrowth at 42% of its right offset and 2.4x its right size, which looks like a map with
    undergrowth in places that have none.
    """
    pixel = 254 / 600
    array = np.zeros((24, 24), dtype=np.uint8)
    array[4:8, 4:8] = 1  # a square four pixels on a side, starting four pixels in
    in_dir = bundle([], rasters={"undergrowth_bit": array}, pixel=pixel)

    from pyproj import Transformer

    features = route(in_dir)["undergrowth"]
    deepest = [f for f in features if f["tippecanoe"]["minzoom"] == 16]
    assert len(deepest) == 1

    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    ring = [to_utm.transform(lon, lat) for lon, lat in deepest[0]["geometry"]["coordinates"][0]]
    xs = [x for x, _ in ring]
    ys = [y for _, y in ring]

    # Four pixels of 254/600 m, four pixels in from the tile's north-west corner.
    assert min(xs) == pytest.approx(593000 + 4 * pixel, abs=0.05)
    assert max(xs) - min(xs) == pytest.approx(4 * pixel, abs=0.05)
    assert max(ys) == pytest.approx(5270000 - 4 * pixel, abs=0.05)
    assert max(ys) - min(ys) == pytest.approx(4 * pixel, abs=0.05)
