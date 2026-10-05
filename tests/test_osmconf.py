"""
Test bin/osmconf.py: the keys karttapullautin's rules test must be shapefile columns, or a rule on
them matches nothing -- which is how power lines went missing from the map without an error.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("osmconf", REPO / "bin" / "osmconf.py")
oc = importlib.util.module_from_spec(spec)
sys.modules["osmconf"] = oc
spec.loader.exec_module(oc)

TEMPLATE = """\
[points]
osm_id=yes
attributes=name,barrier

[lines]
attributes=name,highway,railway
# a comment
[multipolygons]
attributes=name,landuse

[other_relations]
attributes=name,type
"""


def test_the_keys_come_from_the_conditions_in_first_seen_order():
    rules = "power line|516|power=line\nlake|301|natural=water&water!=river\nbad line\nx|1|power!="
    assert oc.rule_keys(rules) == ["power", "natural", "water"]


def test_every_layer_gets_every_key_once(tmp_path):
    out = oc.with_keys(TEMPLATE, ["power", "highway", "water"])
    assert "attributes=name,highway,railway,power,water\n" in out
    assert "attributes=name,barrier,power,highway,water\n" in out
    assert "attributes=name,landuse,power,highway,water\n" in out
    assert "osm_id=yes" in out and "# a comment" in out


def test_the_shipped_rules_file_gets_its_power_column(tmp_path):
    rules = REPO / "assets" / "osm.txt"
    template = tmp_path / "osmconf.ini"
    template.write_text(TEMPLATE)
    out = tmp_path / "out.ini"
    assert oc.main(["--rules", str(rules), "--template", str(template), "--out", str(out)]) == 0
    lines = [line for line in out.read_text().splitlines() if line.startswith("attributes=")]
    assert all("power" in line.split("=", 1)[1].split(",") for line in lines)
