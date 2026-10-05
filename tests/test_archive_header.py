"""
Test bin/archive_header.py: the merged archive's header is what a viewer opens the map with -- its
bounds, where it looks first -- and tile-join's own is wrong for an archive joined from parents.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("archive_header", REPO / "bin" / "archive_header.py")
ah = importlib.util.module_from_spec(spec)
sys.modules["archive_header"] = ah
spec.loader.exec_module(ah)

JOINED_HEADER = {"tile_compression": "gzip", "tile_type": "mvt", "minzoom": 11, "maxzoom": 15,
                 "bounds": [10.19, 47.51, 10.30, 47.57], "center": [10.29, 47.52, 15]}
JOINED_METADATA = {"name": "x", "description": "../b.pmtiles", "attribution": "credits",
                   "generator_options": "tippecanoe ...; tippecanoe ...", "vector_layers": [{"id": "contours"}],
                   "antimeridian_adjusted_bounds": "10,47,11,48"}


def write_parent_tiles(path: Path) -> Path:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tile", "parent", "z", "x", "y", "crs", "n_core", "min_x", "min_y", "max_x", "max_y"])
        # one tile in two parents: counted once
        w.writerow(["590_5268", "12_2163_1431", 12, 2163, 1431, "EPSG:25832", 2, 590000, 5268000, 591000, 5269000])
        w.writerow(["590_5268", "12_2164_1431", 12, 2164, 1431, "EPSG:25832", 2, 590000, 5268000, 591000, 5269000])
        w.writerow(["593_5269", "12_2164_1431", 12, 2164, 1431, "EPSG:25832", 2, 593000, 5269000, 594000, 5270000])
    return path


def test_the_bounds_are_the_planned_tiles_and_the_centre_their_middle_at_the_base_zoom(tmp_path):
    west, south, east, north = ah.region_bounds(write_parent_tiles(tmp_path / "p.csv"))
    # 590-594 km E, 5268-5270 km N in UTM 32N is Immenstadt
    assert 10.18 < west < 10.20 and 10.23 < east < 10.25 and 47.55 < south < north < 47.58

    head = ah.header(JOINED_HEADER, (west, south, east, north), 12)
    assert head["bounds"] == [round(v, 7) for v in (west, south, east, north)]
    assert abs(head["center"][0] - (west + east) / 2) < 1e-6
    assert abs(head["center"][1] - (south + north) / 2) < 1e-6
    assert head["center"][2] == 12
    assert head["tile_type"] == "mvt" and head["minzoom"] == 11
    # a centre zoom outside the archive is pulled into it
    assert ah.header(JOINED_HEADER, (west, south, east, north), 9)["center"][2] == 11


def test_the_metadata_describes_the_tiles_not_how_they_were_joined():
    meta = ah.metadata(JOINED_METADATA, "mapant", "an orienteering map")
    assert meta == {"name": "mapant", "description": "an orienteering map", "attribution": "credits",
                    "vector_layers": [{"id": "contours"}]}


def test_main_writes_what_pmtiles_edit_reads(tmp_path):
    (tmp_path / "h.json").write_text(json.dumps(JOINED_HEADER))
    (tmp_path / "m.json").write_text(json.dumps(JOINED_METADATA))
    assert ah.main(["--header-in", str(tmp_path / "h.json"), "--metadata-in", str(tmp_path / "m.json"),
                    "--parent-tiles", str(write_parent_tiles(tmp_path / "p.csv")), "--center-zoom", "12",
                    "--title", "t", "--header-out", str(tmp_path / "ho.json"),
                    "--metadata-out", str(tmp_path / "mo.json")]) == 0
    assert json.loads((tmp_path / "ho.json").read_text())["center"][2] == 12
    assert "generator_options" not in json.loads((tmp_path / "mo.json").read_text())
