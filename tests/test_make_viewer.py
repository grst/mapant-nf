"""
Test bin/make_viewer.py and the style template it fills in.

Everything that goes wrong here goes wrong silently: a style layer naming a source layer the tiles
do not carry, or an image the sprite does not have, draws nothing; a width off by a factor draws a
map that looks almost right.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "assets" / "viewer"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mv = load("make_viewer", REPO / "bin" / "make_viewer.py")
mvt = load("make_vector_tiles", REPO / "bin" / "make_vector_tiles.py")

# The effective ini of the pipeline's own asset is what a real run fills the colours from.
INI = REPO / "assets" / "pullauta.ini"


@pytest.fixture
def viewer(tmp_path):
    parents = tmp_path / "parent_tiles.csv"
    parents.write_text("z,x,y,tile,crs,n_core\n13,4329,2862,590_5268,EPSG:25832,1\n")
    out = tmp_path / "viewer"
    assert mv.main([
        "--template-dir", str(TEMPLATES), "--parent-tiles", str(parents), "--ini", str(INI),
        "--base-zoom", "13", "--max-zoom", "15", "--title", "t", "--attribution", "credits",
        "--out-dir", str(out),
    ]) == 0
    return out


def test_every_placeholder_is_filled_in(viewer):
    style = json.loads((viewer / "style.json").read_text())
    assert "mapant:" not in json.dumps(style)
    assert style["name"] == "t"
    assert style["sources"]["mapant"] == {
        "type": "vector", "url": "pmtiles://mapant.pmtiles", "attribution": "credits",
    }
    assert {"style.json", "sprite.png", "sprite@2x.png", "index.html"} <= {
        p.name for p in viewer.iterdir()
    }


def test_an_unknown_placeholder_fails(tmp_path):
    filler = mv.Filler(13, 15, 47.5, {})
    with pytest.raises(SystemExit):
        filler.fill({"fill-color": "mapant:color:purple"})


def test_the_style_draws_only_layers_the_tiles_have(viewer):
    """A style layer naming a source layer the tiles do not carry draws nothing."""
    style = json.loads((viewer / "style.json").read_text())
    used = {layer["source-layer"] for layer in style["layers"] if "source-layer" in layer}
    assert used <= {layer.name for layer in mvt.LAYERS}


def test_every_image_the_style_names_is_in_the_sprite(viewer):
    """The undergrowth patterns are one per zoom, and the slope line is an icon."""
    style = json.loads((viewer / "style.json").read_text())
    named: set[str] = set()

    def collect(value):
        if isinstance(value, str):
            named.add(value)
        elif isinstance(value, list):
            for v in value[1:]:
                collect(v)

    for layer in style["layers"]:
        collect(layer.get("paint", {}).get("fill-pattern"))
        collect(layer.get("layout", {}).get("icon-image"))
    assert named, "the style names no images at all"
    for suffix in ("", "@2x"):
        sprite = json.loads((viewer / f"sprite{suffix}.json").read_text())
        assert named <= set(sprite), sorted(named - set(sprite))


def test_a_width_covers_its_ground_in_512_px_zooms():
    """
    MapLibre's zoom z is 512 * 2^z pixels round the equator, so at 47.5 N a pixel at z15 is
    78271.5 / 2^15 * cos(47.5) = 1.61 m. A 20 m line is ~12.4 px there, and doubles per zoom.
    """
    filler = mv.Filler(13, 15, 47.5, {})
    _, _, _, low, low_px, high, high_px = filler.metres(20.0)
    assert (low, high) == (13, 19)
    assert low_px == pytest.approx(20.0 / 1.6134 / 4, rel=1e-3)
    assert mv.pixels_per_metre(15, 47.5) == pytest.approx(1 / 1.6134, rel=1e-3)
    assert high_px / low_px == pytest.approx(2**6, rel=1e-3)
    # and far out, a line is still drawn
    assert filler.metres(0.01)[4] == 1.0


def test_undergrowth_stripes_keep_their_ground_spacing():
    """The pattern for each zoom puts the stripes as far apart on the ground as the PNG does."""
    patterns = mv.Filler(13, 15, 47.5, {}).patterns()
    for zoom in range(15, 18):
        period, *_ = patterns[f"undergrowth-z{zoom}"]
        assert period / mv.pixels_per_metre(zoom, 47.5) == pytest.approx(18.0, rel=0.1)
        dense, *_ = patterns[f"undergrowth-dense-z{zoom}"]
        assert dense == pytest.approx(period / 2, abs=1)


def test_the_colours_are_the_renders():
    """The green ramp is palette.rs's, and a symbol takes the darkest tone greenshadeisom gives it."""
    assert mv.green_shades(11, 200)[0] == (200, 254, 200)
    assert mv.green_shades(11, 200)[-1] == (0, 180, 0)

    colors = mv.ini_colors({"greenshades": "a|b|c", "lightgreentone": "200",
                            "greenshadeisom": "406|408|408", "buildingcolor": "10,20,30"})
    shades = mv.green_shades(3, 200)
    assert colors["green-406"] == mv.rgb(shades[0])
    assert colors["green-408"] == mv.rgb(shades[2])
    assert colors["green-410"] == colors["green-default"] == mv.rgb(shades[-1])
    assert colors["building"] == "rgb(10,20,30)"
