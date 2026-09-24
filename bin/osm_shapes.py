#!/usr/bin/env python3
"""
Match a grid's OSM shapefiles against the ISOM rules and write the features the map draws.

These used to come out of karttapullautin. It read the same shapefiles, matched them against the
same rules file, and -- because the shapes are only vectors while it is drawing them -- wrote each
matched shape into the tile's GeoJSON on the way to the canvas. With the raster pyramid gone
karttapullautin has no reason to draw them at all, so it renders the LiDAR alone and the matching
happens here, once per parent tile, from the archives OSM_TO_SHAPES already produces.

What that buys: the shapes are cut once for an area instead of once per square kilometre that
touches them, so no feature arrives twice and nothing has to be deduplicated on `osm_id`; and the
OSM layers can be re-cut without re-running a LiDAR render.

What it costs: this is a second implementation of a matcher whose first implementation is in
`src/shapefile/render.rs`. It is deliberately a transcription of that one rather than a tidier
equivalent, down to the parts that look like mistakes, because the thing being preserved is *which
shapes end up on the map*:

  * A rule's conditions are AND-ed. `key=value` compares equal, `key!=value` compares unequal, and
    a field the shapefile does not have -- or has, but not as a text column -- reads as the empty
    string. So `power!=` means "has a power tag" and `bridge!=yes` is also true of a way with no
    bridge tag at all.
  * Rules are tried in file order and the first *drawable* match wins. A rule can match and still
    not consume the shape: karttapullautin's cascade only assigns a colour to the codes it knows,
    and a rule matching some other code leaves the shape to be picked up by a later rule.
  * An area code is only drawn from a polygon and a line code only from a polyline. A lake mapped
    as an unclosed way is not a lake here, exactly as it is not one on the rendered map.
  * A multi-part polyline becomes one feature per part; a polygon becomes one feature per outer
    ring, carrying the holes that follow it.

`tests/test_osm_shapes.py` pins each of those, and the disagreement against karttapullautin's own
output over the Immenstadt shapes is what validated the transcription.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import shapefile  # pyshp

#: The codes `render.rs` fills as an area. Everything else it recognises, it strokes as a line.
#: Both sets are that file's `if isom == "..."` cascade, and they are also exactly what
#: `make_vector_style.py` knows how to draw -- a code in neither is matched by no rule that can
#: reach the map, so it is dropped here with a warning rather than travelling as an unstyled
#: feature.
AREA_CODES = frozenset({"301", "310", "401", "526", "527", "529", "529T"})
LINE_CODES = frozenset(
    {
        "301.1",
        "306",
        "414",
        "503",
        "503T",
        "504",
        "504T",
        "505",
        "505T",
        "507",
        "507T",
        "515",
        "515T",
        "516",
        "524",
        "529.1",
    }
)
DRAWN_CODES = AREA_CODES | LINE_CODES

#: pyshp's shape types, by the two geometry families the rules distinguish.
POLYLINE_TYPES = frozenset({3, 13, 23})
POLYGON_TYPES = frozenset({5, 15, 25})


@dataclass(frozen=True)
class Condition:
    key: str
    value: str
    equal: bool


@dataclass(frozen=True)
class Rule:
    """One line of the rules file: an ISOM code and the conditions that select it."""

    isom: str
    conditions: tuple[Condition, ...]


def parse_rules(text: str) -> list[Rule]:
    """
    Parse a vectorconf file -- `description|isom|key=value&key2!=value2` per line.

    Blank lines are skipped, which the Rust parser does not do only because it is never handed
    one; everything else is its `Mapping::from_str`, including trimming the ISOM code, which that
    one forgets. (It compares the untrimmed code against untrimmed literals, so a code written
    with a trailing space matches nothing and the rule silently never draws. The shipped osm.txt
    has one: `trench|516 |...`.)
    """
    rules: list[Rule] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != 3:
            raise ValueError(
                f"{number}: expected 3 sections separated by '|', got {len(parts)}: {line}"
            )
        isom = parts[1].strip()
        if not isom:
            raise ValueError(f"{number}: the ISOM code must not be empty: {line}")

        conditions: list[Condition] = []
        for param in parts[2].split("&"):
            if "!=" in param:
                key, value = param.split("!=", 1)
                equal = False
            elif "=" in param:
                key, value = param.split("=", 1)
                equal = True
            else:
                raise ValueError(f"{number}: condition has no operator: {param}")
            conditions.append(Condition(key.strip(), value.strip(), equal))
        rules.append(Rule(isom, tuple(conditions)))
    return rules


class Fields:
    """The text fields of one shapefile, read the way karttapullautin reads them."""

    def __init__(self, reader: shapefile.Reader) -> None:
        # fields[0] is the deletion flag, which is not a column.
        self.text = {name for name, kind, *_ in reader.fields[1:] if kind == "C"}
        self.any = {name for name, *_ in reader.fields[1:]}

    def get(self, record, key: str) -> str:
        """
        What a condition compares against: a text column's trimmed value, or the empty string.

        A numeric column reads as empty even when it has a value, because the Rust side matches
        only `FieldValue::Character`. This is load-bearing rather than incidental: `osm_id` is
        numeric in some ogr2ogr outputs, and a rule keyed on it would then match nothing.
        """
        if key not in self.text:
            return ""
        value = record[key]
        return "" if value is None else str(value).strip()


def match_code(fields: Fields, record, rules: list[Rule]) -> str | None:
    """The ISOM code this record is drawn as, or None if no drawable rule matches it."""
    for rule in rules:
        if all(
            (fields.get(record, c.key) == c.value) == c.equal for c in rule.conditions
        ):
            # Not `return`: a rule whose code the renderer has no branch for leaves the shape
            # unclaimed, and a later rule may still take it.
            if rule.isom in DRAWN_CODES:
                return rule.isom
    return None


def geometries(shape, code: str) -> list[dict]:
    """
    The GeoJSON geometries one shape contributes, or none if its kind does not suit its code.

    Kept identical to `write_shape_feature`: parts of a polyline are separate features, and a
    polygon is one feature per outer ring with the holes that follow it -- which is what pyshp's
    `__geo_interface__` reconstructs from the ring orientations.
    """
    is_area = code in AREA_CODES
    if is_area and shape.shapeType in POLYGON_TYPES:
        pass
    elif not is_area and shape.shapeType in POLYLINE_TYPES:
        pass
    else:
        return []

    geometry = shape.__geo_interface__
    kind = geometry["type"]
    if kind in {"LineString", "Polygon"}:
        return [geometry]
    if kind == "MultiLineString":
        return [
            {"type": "LineString", "coordinates": part}
            for part in geometry["coordinates"]
        ]
    if kind == "MultiPolygon":
        return [
            {"type": "Polygon", "coordinates": rings}
            for rings in geometry["coordinates"]
        ]
    return []


def osm_id_of(fields: Fields, record) -> str | None:
    """`osm_id` as a string, from a text or a numeric column, or None if the data has neither."""
    if "osm_id" not in fields.any:
        return None
    value = record["osm_id"]
    if value is None or value == "":
        return None
    return str(value).strip()


#: How a DBF's text is decoded when the archive does not say. Never strictly: a shapefile written
#: from OSM carries whatever the extract had, and one undecodable street name must not end a grid.
#: Rule values are ASCII, so a mangled character can only appear in a field no condition reads.
DEFAULT_ENCODING = "utf-8"


def _reader(parts: dict[str, io.BytesIO], cpg: bytes | None) -> shapefile.Reader:
    encoding = cpg.decode("ascii", "ignore").strip() if cpg else ""
    return shapefile.Reader(
        encoding=encoding or DEFAULT_ENCODING, encodingErrors="replace", **parts
    )


def layers(archive: Path) -> list[tuple[str, shapefile.Reader]]:
    """
    Every shapefile in a zip archive or a directory, as readers over in-memory bytes.

    Read whole rather than streamed: a grid's archive is a few tens of megabytes, and seeking
    inside a deflated member decompresses it again from the start.
    """
    found: list[tuple[str, shapefile.Reader]] = []
    if archive.is_dir():
        for shp in sorted(archive.glob("*.shp")):
            parts = {
                ext: io.BytesIO(shp.with_suffix(f".{ext}").read_bytes())
                for ext in ("shp", "dbf", "shx")
                if shp.with_suffix(f".{ext}").is_file()
            }
            cpg = shp.with_suffix(".cpg")
            if "shp" in parts and "dbf" in parts:
                found.append(
                    (shp.stem, _reader(parts, cpg.read_bytes() if cpg.is_file() else None))
                )
        return found

    if not zipfile.is_zipfile(archive):
        return []

    with zipfile.ZipFile(archive) as zf:
        members = {Path(n).name: n for n in zf.namelist()}
        stems = sorted({Path(n).stem for n in zf.namelist() if n.endswith(".shp")})
        for stem in stems:
            parts = {
                ext: io.BytesIO(zf.read(members[f"{stem}.{ext}"]))
                for ext in ("shp", "dbf", "shx")
                if f"{stem}.{ext}" in members
            }
            cpg = members.get(f"{stem}.cpg")
            if "shp" in parts and "dbf" in parts:
                found.append((stem, _reader(parts, zf.read(cpg) if cpg else None)))
    return found


def convert(archives: list[Path], rules: list[Rule], out) -> Counter:
    """
    Write every drawable shape as one GeoJSON feature per line. Returns the code histogram.

    Identical features are written once. A parent tile can draw from two grids, and each grid's
    extract was cut with a 2 km buffer and `--strategy smart`, so a way near their shared boundary
    is in both archives -- whole and identical in both, which is what makes an exact match the right
    test. Left in, it would be drawn twice, and exported to OCAD as two objects on top of each
    other.
    """
    counts: Counter = Counter()
    seen: set[bytes] = set()
    duplicates = 0
    for archive in archives:
        for name, reader in layers(archive):
            fields = Fields(reader)
            for shape_record in reader.iterShapeRecords():
                shape = shape_record.shape
                if shape.shapeType not in POLYLINE_TYPES | POLYGON_TYPES:
                    continue
                code = match_code(fields, shape_record.record, rules)
                if code is None:
                    continue
                properties = {"layer": "osm", "isom": code}
                osm_id = osm_id_of(fields, shape_record.record)
                if osm_id is not None:
                    properties["osm_id"] = osm_id
                for geometry in geometries(shape, code):
                    feature = json.dumps(
                        {
                            "type": "Feature",
                            "geometry": geometry,
                            "properties": properties,
                        },
                        separators=(",", ":"),
                    )
                    digest = hashlib.blake2b(feature.encode(), digest_size=16).digest()
                    if digest in seen:
                        duplicates += 1
                        continue
                    seen.add(digest)
                    out.write(feature)
                    out.write("\n")
                    counts[code] += 1
            print(f"  {archive.name}/{name:20s} {counts.total():7d} feature(s) so far")
    if duplicates:
        print(f"  {duplicates} duplicate(s) from overlapping extracts, written once")
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archives", nargs="*", type=Path, help="shape archives or directories")
    ap.add_argument("--rules", type=Path, required=True, help="the vectorconf mapping file")
    ap.add_argument("--out", type=Path, required=True, help="GeoJSONL to write")
    args = ap.parse_args(argv)

    try:
        rules = parse_rules(args.rules.read_text())
    except ValueError as err:
        print(f"osm_shapes.py: {args.rules}:{err}", file=sys.stderr)
        return 1

    undrawable = sorted({r.isom for r in rules} - DRAWN_CODES)
    if undrawable:
        print(
            f"osm_shapes.py: {len(undrawable)} rule code(s) have no symbol and are skipped: "
            + ", ".join(undrawable),
            file=sys.stderr,
        )

    # A sentinel or a missing archive is a grid with no OSM features, which is normal.
    archives = [p for p in args.archives if p.is_dir() or zipfile.is_zipfile(p)]
    print(f"{len(rules)} rule(s), {len(archives)} archive(s) of {len(args.archives)}")

    with args.out.open("w") as out:
        counts = convert(archives, rules, out)

    for code, n in sorted(counts.items()):
        print(f"  {code:8s} {n:7d}")
    print(f"{counts.total()} feature(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
