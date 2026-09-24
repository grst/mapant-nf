"""
Test bin/osm_shapes.py -- the matcher that decides which OSM shapes reach the map.

This code exists because the raster pyramid does not: karttapullautin used to match these shapes
on its way to drawing them, and now nothing draws them, so the pipeline matches them itself. That
makes it a second implementation of `src/shapefile/render.rs`, and the risk is not that it crashes
but that it quietly disagrees -- a road that used to be a road becoming a track, or a lake mapped
as an open way silently becoming a lake.

So what is pinned here is the *semantics*, including the parts that look like accidents and are
load-bearing anyway: an absent field comparing equal to the empty string, a rule matching a code
nothing can draw leaving the shape for the next rule, and the geometry kind having to suit the
code. Each of these was read off the Rust and each changes which features exist.

The other half of the check is not here, because it needs data: the matcher was run over the
Immenstadt shapes and its output compared, feature by feature, against the shapes karttapullautin
itself wrote into two tiles' GeoJSON. 262 and 324 features, no disagreement in either direction.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest
import shapefile

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("osm_shapes", REPO / "bin" / "osm_shapes.py")
osm = importlib.util.module_from_spec(spec)
sys.modules["osm_shapes"] = osm
spec.loader.exec_module(osm)


def write_shapes(directory: Path, name: str, fields, rows) -> Path:
    """
    A one-layer shapefile. `fields` is (name, type, size); a row is (geometry, {field: value}).

    Geometry is a list of parts, which pyshp writes as a polyline or a polygon depending on which
    writer method is used -- chosen here by whether the first part closes on itself, the same thing
    a shapefile records in its type field.
    """
    directory.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(target=str(directory / name))
    for field in fields:
        writer.field(*field)
    for parts, attributes in rows:
        if parts[0][0] == parts[0][-1] and len(parts[0]) > 3:
            writer.poly(parts)
        else:
            writer.line(parts)
        writer.record(**attributes)
    writer.close()
    return directory / f"{name}.shp"


def match(tmp_path, rules_text, fields, rows) -> list[dict]:
    """Run the matcher over one written layer and return the features it produced."""
    write_shapes(tmp_path / "shapes", "lines", fields, rows)
    out = tmp_path / "osm.geojsonl"
    with out.open("w") as fh:
        osm.convert([tmp_path / "shapes"], osm.parse_rules(rules_text), fh)
    return [json.loads(line) for line in out.read_text().splitlines()]


LINE = [[(0.0, 0.0), (10.0, 10.0)]]
RING = [[(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)]]


# --- the rules file -------------------------------------------------------------------------


def test_a_rule_is_a_code_and_its_conditions():
    (rule,) = osm.parse_rules("road|503|highway=motorway&bridge!=yes")
    assert rule.isom == "503"
    assert rule.conditions == (
        osm.Condition("highway", "motorway", True),
        osm.Condition("bridge", "yes", False),
    )


def test_a_code_written_with_a_trailing_space_is_still_that_code():
    # karttapullautin does not trim it and then compares against untrimmed literals, so its own
    # `trench|516 |...` rule can never draw. Trimming here is a deliberate divergence: the rule
    # says 516 and 516 is a code we can draw.
    (rule,) = osm.parse_rules("trench|516 |man_made=trench")
    assert rule.isom == "516"


@pytest.mark.parametrize(
    "line",
    [
        "description|306",  # two sections
        "description||waterway!=",  # no code
        "description|306|waterway",  # no operator
    ],
)
def test_a_malformed_rule_is_an_error_not_a_silently_skipped_line(line):
    with pytest.raises(ValueError):
        osm.parse_rules(line)


# --- matching -------------------------------------------------------------------------------


def test_the_first_matching_rule_wins(tmp_path):
    rules = "track|505|highway=track\nroad|504|highway!=\n"
    features = match(tmp_path, rules, [("highway", "C", 32)], [(LINE, {"highway": "track"})])
    assert [f["properties"]["isom"] for f in features] == ["505"]


def test_a_rule_whose_code_cannot_be_drawn_leaves_the_shape_to_the_next_one(tmp_path):
    # 999 is in no cascade branch, so karttapullautin assigns no colour and keeps looking. A `break`
    # here instead would silently drop every footway.
    rules = "unknown|999|highway=footway\npath|507|highway=footway\n"
    features = match(tmp_path, rules, [("highway", "C", 32)], [(LINE, {"highway": "footway"})])
    assert [f["properties"]["isom"] for f in features] == ["507"]


def test_a_field_the_data_does_not_have_reads_as_empty(tmp_path):
    # Which is why `bridge!=yes` selects a way with no bridge column at all -- most of them.
    rules = "road|503|highway=motorway&bridge!=yes"
    features = match(tmp_path, rules, [("highway", "C", 32)], [(LINE, {"highway": "motorway"})])
    assert len(features) == 1


def test_not_equal_to_nothing_means_the_tag_is_present(tmp_path):
    rules = "railway|515|railway!="
    fields = [("railway", "C", 32)]
    drawn = match(tmp_path, rules, fields, [(LINE, {"railway": "rail"})])
    absent = match(tmp_path, rules, fields, [(LINE, {"railway": ""})])
    assert len(drawn) == 1
    assert absent == []


def test_a_numeric_column_never_matches(tmp_path):
    # The Rust reads `FieldValue::Character` only. A rule keyed on a numeric column matches nothing
    # there, so it must match nothing here either.
    rules = "by number|503|z_order=5"
    features = match(tmp_path, rules, [("z_order", "N", 9)], [(LINE, {"z_order": 5})])
    assert features == []


# --- geometry -------------------------------------------------------------------------------


def test_an_area_code_is_not_drawn_from_a_line(tmp_path):
    # An unclosed way tagged natural=water is not a lake on the rendered map either.
    features = match(
        tmp_path, "water|301|natural=water", [("natural", "C", 32)], [(LINE, {"natural": "water"})]
    )
    assert features == []


def test_a_line_code_is_not_drawn_from_an_area(tmp_path):
    features = match(
        tmp_path, "road|504|highway!=", [("highway", "C", 32)], [(RING, {"highway": "residential"})]
    )
    assert features == []


def test_an_area_code_is_drawn_from_a_polygon(tmp_path):
    features = match(
        tmp_path, "water|301|natural=water", [("natural", "C", 32)], [(RING, {"natural": "water"})]
    )
    assert [f["geometry"]["type"] for f in features] == ["Polygon"]


def test_each_part_of_a_multipart_line_is_its_own_feature(tmp_path):
    parts = [[(0.0, 0.0), (10.0, 10.0)], [(20.0, 20.0), (30.0, 30.0)]]
    features = match(
        tmp_path, "road|504|highway!=", [("highway", "C", 32)], [(parts, {"highway": "residential"})]
    )
    assert [f["geometry"]["type"] for f in features] == ["LineString", "LineString"]


def test_osm_id_travels_with_the_feature(tmp_path):
    features = match(
        tmp_path,
        "road|504|highway!=",
        [("osm_id", "C", 16), ("highway", "C", 32)],
        [(LINE, {"osm_id": "12345", "highway": "residential"})],
    )
    assert features[0]["properties"]["osm_id"] == "12345"


# --- inputs ---------------------------------------------------------------------------------


def test_shapes_are_read_out_of_a_zip_archive(tmp_path):
    write_shapes(tmp_path / "loose", "lines", [("highway", "C", 32)], [(LINE, {"highway": "x"})])
    archive = tmp_path / "grid.shp.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for member in (tmp_path / "loose").iterdir():
            zf.write(member, member.name)

    out = tmp_path / "osm.geojsonl"
    with out.open("w") as fh:
        counts = osm.convert([archive], osm.parse_rules("road|504|highway!="), fh)
    assert counts["504"] == 1


def test_a_shape_in_two_grids_extracts_is_written_once(tmp_path):
    """
    A parent tile draws from every grid under it, and neighbouring grids' extracts overlap by
    `osm_buffer_m` with ways kept whole across the cut -- so the same road arrives twice, identical.
    Drawn twice it is invisible; exported to OCAD it is two objects on top of each other.
    """
    write_shapes(tmp_path / "a", "lines", [("highway", "C", 32)], [(LINE, {"highway": "x"})])
    archives = []
    for grid in ("590_5268_grid", "591_5268_grid"):
        archive = tmp_path / f"{grid}.shp.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for member in (tmp_path / "a").iterdir():
                zf.write(member, member.name)
        archives.append(archive)

    out = tmp_path / "osm.geojsonl"
    with out.open("w") as fh:
        counts = osm.convert(archives, osm.parse_rules("road|504|highway!="), fh)
    assert counts["504"] == 1
    assert len(out.read_text().splitlines()) == 1


def test_two_shapes_that_only_look_alike_both_survive(tmp_path):
    """The test is exact equality, so a second road somewhere else is not a duplicate."""
    other = [[(100.0, 100.0), (110.0, 110.0)]]
    features = match(
        tmp_path,
        "road|504|highway!=",
        [("highway", "C", 32)],
        [(LINE, {"highway": "x"}), (other, {"highway": "x"})],
    )
    assert len(features) == 2


def test_a_sentinel_instead_of_an_archive_is_not_an_error(tmp_path):
    # A grid whose extract held nothing drawable stages a .NONE file. The parent it belongs to still
    # has to be cut, from whatever else is under it.
    sentinel = tmp_path / "grid.NONE"
    sentinel.write_text("no OSM features in this grid\n")
    out = tmp_path / "osm.geojsonl"
    with out.open("w") as fh:
        counts = osm.convert([sentinel], osm.parse_rules("road|504|highway!="), fh)
    assert counts.total() == 0


def test_an_undecodable_name_does_not_end_the_grid(tmp_path):
    # ogr2ogr writes whatever the extract had. One street name that is not UTF-8 must not fail a
    # square kilometre of map; rule values are ASCII, so the mangled character cannot change a match.
    directory = tmp_path / "shapes"
    write_shapes(directory, "lines", [("name", "C", 64), ("highway", "C", 32)],
                 [(LINE, {"name": "x", "highway": "residential"})])
    dbf = directory / "lines.dbf"
    dbf.write_bytes(dbf.read_bytes().replace(b"x" + b" " * 63, b"\xdf" + b" " * 63))

    out = tmp_path / "osm.geojsonl"
    with out.open("w") as fh:
        counts = osm.convert([directory], osm.parse_rules("road|504|highway!="), fh)
    assert counts["504"] == 1
