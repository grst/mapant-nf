# Hand-off: the vector pyramid in mapant-nf

> **Superseded.** This describes the first, bitmap-based prototype. The current plan builds on
> @malpou's karttapullautin fork -- see `HANDOFF-malpou-stack.md` next to the repositories.

This describes the pipeline as prototyped on the branch **`feature/vector-tiles`** in this working
copy: `tiles_vector/{z}/{x}/{y}.pbf`, Mapbox vector tiles, and **no raster pyramid at all**.

It depends on karttapullautin gaining `output_geojson`; see `HANDOFF-karttapullautin.md` in that
repository. **Nothing here can go to production before that PR lands**, because the pipeline's
`karttapullautin` image is built from an upstream release.

---

## 1. What changed, and the one decision everything follows from

**The raster pyramid is gone.** `MAKE_TILES`, `TILE_VIEWER`, `bin/make_viewer.py`, the
`karttapullautin2tiles` dependency, `--vector_tiles`, `--tile_format`: all removed. The pipeline
builds one pyramid and it is vector.

That is what makes the rest possible. While a rendered pyramid existed, the OSM shapes had to be
matched to their ISOM codes *inside karttapullautin*, at the moment it drew them, or the two
pyramids would show different roads -- which is why the earlier version of this prototype needed a
change to `src/shapefile/render.rs`. With no rendered pyramid there is nothing to diverge from, so:

* karttapullautin is given **no shapefiles and an empty `vectorconf`**. It renders the LiDAR and
  nothing else, and skips the vector drawing pass entirely.
* `bin/osm_shapes.py` matches the shapes in `MAKE_VECTOR_TILES`, from the same archives
  `OSM_TO_SHAPES` already produced and the same rules file karttapullautin used to be handed.
* the upstream PR shrinks to a GeoJSON writer, four classified rasters and a bug fix -- additive,
  default-off, and nowhere near the renderer.

```
PLAN_GRIDS ──┬─(grid csv)──> PULLAUTA_GRID ──(out/<tile>_vec/)──┐
             └─(osm chunk)─> OSM_EXTRACT ─> OSM_TO_SHAPES ──────┴─> MAKE_VECTOR_TILES ─> tiles_vector/z/x/y.pbf
                                            (<grid>.shp.zip)
             └─(parent index)──────────────────────────────────────> VECTOR_VIEWER ─> style.json, sprite, metadata.json, index.html
```

A bundle is `<tile>.geojson.gz` plus the classified rasters
`<tile>_{vege,undergrowth,water,blocks}_bit.png/.pgw`, one directory per tile, so the fan-in is one
path per tile.

## 2. Files

**New**

| file | lines | what |
| --- | --- | --- |
| `bin/osm_shapes.py` | 353 | the ISOM matcher: rules file in, one matched shape per line out |
| `bin/make_vector_tiles.py` | 701 | routes a parent's bundles into per-layer GeoJSONL, traces the rasters, clips the shapes to the rendered ground, runs tippecanoe, prunes foreign tiles |
| `bin/make_vector_style.py` | 736 | the MapLibre style, the sprite, `metadata.json` and the preview page |
| `tests/test_osm_shapes.py` | 264 | 20 tests over the rule semantics |
| `tests/test_make_vector_tiles.py` | 525 | 16 tests over the routing, the zoom plan, the tracing and the pruning |
| `modules/local/make_vector_tiles/main.nf` | 69 | one task per parent: match, then cut |
| `modules/local/vector_viewer/main.nf` | 37 | one task per run |
| `containers/tiler/` | | the old `k2t` image, rebuilt around tippecanoe |

**Deleted**: `modules/local/make_tiles/`, `modules/local/tile_viewer/`, `bin/make_viewer.py`.

**Changed**: `main.nf`, `nextflow.config` and `nextflow_schema.json` (params dropped),
`conf/{base,containers,test_stub}.config`, `modules/local/osm_to_shapes/main.nf` (per-grid archive
names), `modules/local/pullauta_grid/main.nf` (no shapes, no images out),
`modules/local/render_ini/main.nf`, `bin/render_ini.py` (the vector keys are unconditional now, and
`vectorconf` is forced empty), `bin/run_pullauta.py` (always bundles; deletes the images
afterwards), `bin/plan_grids.py` (comments), `containers/{build,smoke}.sh`, the two workflows,
`docs/metro_map.mmd` and its SVG, `README.md`, `CLAUDE.md`, and four test files.

31 tracked files changed, 780 insertions, 638 deletions, plus the eight new ones.

## 3. The rules that make it work

Both of the first two exist because the pyramid is cut **one parent at a time**, by tasks that never
see each other.

**1. What a zoom shows is declared, never negotiated.** Every one of tippecanoe's size-driven
thinning options decides per tile, from whatever happens to be in it. Two neighbouring parents then
disagree along the line they share -- a lake drawn on an overview tile in one and missing in the
next -- and, measured on Immenstadt, every zoom including the deepest was pegged at the 500 KB
limit, so the level the OCAD export reads was truncated too. They are all off
(`--no-tile-size-limit --no-feature-limit --drop-rate=1`). Each feature instead carries a
`tippecanoe: {"minzoom": …}` member from the table in `make_vector_tiles.py`:

| from the base zoom | one level above the deepest | the deepest zoom only |
| --- | --- | --- |
| vegetation, water, index contours, roads, tracks, railways, streams, lakes, open land, marsh, settlement | plain contours and depressions, undergrowth, blocks, buildings, paved areas, small paths | form lines, knolls, slope lines, single-contour depressions, fences, power lines |

The cliff hatching is a texture, not features -- ~170k two-point ticks per km² of alpine terrain,
more than everything else put together -- so one tick in sixteen survives each zoom out, chosen by a
**hash of the tick's own position**. That makes the sample identical whichever parent cuts the tile,
and nested, so a tick on an overview tile is on every deeper one.

**2. Area boundaries are generalised on the raster, not on the polygons.** Two shades of green meet
along a pixel edge that belongs to both. Simplifying each polygon on its own moves that edge twice
and leaves a sliver of white paper between them. Each zoom is therefore traced from its own
mode-downsampled grid, at about one raster pixel per screen pixel, and the remaining smoothing is
`shapely.coverage_simplify`, which simplifies a shared edge once and only ever *drops* vertices --
which is also what keeps two neighbouring square kilometres meeting along the border they share.

**3. The OSM matcher is a transcription, not an equivalent.** `bin/osm_shapes.py` reproduces
`src/shapefile/render.rs` down to the parts that look like accidents, because each of them decides
whether a feature exists:

* a field the shapefile does not have -- or has, but not as a DBF *text* column -- reads as the
  empty string, so `bridge!=yes` is true of a way with no bridge tag, and `power!=` means "has a
  power tag";
* rules are tried in file order and the **first drawable match wins**: a rule whose ISOM code has no
  branch in karttapullautin's drawing cascade leaves the shape unclaimed for a later rule, rather
  than consuming it;
* an area code is only drawn from a polygon and a line code only from a polyline;
* a multi-part polyline becomes one feature per part, a polygon one feature per outer ring.

One deliberate divergence: the ISOM code is trimmed. karttapullautin does not trim it and then
compares against untrimmed literals, so its own `trench|516 |…` rule can never draw anything.

**4. A shape that is in two grids' extracts is written once.** Neighbouring extracts overlap by
`osm_buffer_m`, and `osmium --strategy smart` keeps a way crossing the cut whole, so a road near a
grid boundary is in both archives and identical in both -- and a parent tile stages every archive
under it. `osm_shapes.py` therefore hashes each serialised feature and writes it once. Drawn twice
it is invisible; exported to OCAD it is two objects on top of each other, which is the sort of thing
a mapper finds a week later.

**5. The shapes are clipped to the ground that was rendered** -- the union of the bounds of the
bundles under the parent -- not to each tile. The extract covers a grid plus `osm_buffer_m`, so
without the clip a road would run out past the last rendered square kilometre. Clipping per parent
is also why nothing arrives twice: under the old arrangement a shape crossing a tile border was
written into both tiles' GeoJSON and had to be deduplicated on `osm_id`.

## 4. What was measured

`-profile podman,test_immenstadt`, the full thing: 24 laz tiles downloaded from geodaten.bayern.de,
two grids, 8 core km² of alpine terrain rendered and cut. 12 tasks, 0 failed, twice (once before
and once after the karttapullautin fix in §5).

**The pyramid.** 95 tiles over z13-16 -- 4 at the base zoom, exactly the four rows of
`parent_tiles.csv` -- 16 MiB published, 7.3 MiB gzipped. No `tiles/`. QC files header-only.

**Against the same region cut by the previous prototype**, whose OSM came from karttapullautin,
tile by tile and layer by layer (`prototype/compare_pyramids.py`):

| | result |
| --- | --- |
| tiles | 95 in both, same paths |
| LiDAR layers (contours, formlines, knolls, cliffs, vegetation, undergrowth, water) | **identical in every tile** |
| OSM layers | same codes in the same tiles; **fewer features**, covering the same ground |

The OSM difference is the point of rule 5, and it is an improvement: a road crossing two rendered
square kilometres used to arrive as two features, one per tile's clip, and is now one. Total extent
per code per tile agrees to within 0.07 % -- a few hundred square units out of millions, in tile
coordinates where 4096 is a tile edge -- which is the residue of karttapullautin's centimetre
coordinate rounding and of quantising a merged polygon instead of two abutting ones.

**The deduplication is not theoretical.** Each of the two parents that draws from both grids found
**9,807 duplicate features out of 23,555** -- 42 % -- because the grids' extracts overlap by
`osm_buffer_m` and osmium keeps a crossing way whole in both. Without rule 4 every one of those
would be in the tiles twice. The published pyramid has **0 duplicated OSM features** across all 95
tiles and 4,832 OSM features.

**The matcher against karttapullautin's own output**, feature by feature, over the two tiles of the
earlier run whose GeoJSON still holds the shapes it drew: 262 and 324 features, **no disagreement in
either direction** -- same features, codes and `osm_id`s, geometry equal after rounding to
karttapullautin's centimetre.

**What the renderer produces without shapefiles**: 153,122 LiDAR features for 593_5269, exactly the
earlier run's 153,386 minus the 264 OSM features it used to carry. Taking OSM away from the renderer
changed nothing else about its output.

**Disk.** The whole run's work directory is 493 MB for 8 km². No laz survives (the task trap), and
no rendered image (`run_pullauta.py` deletes them once the bundles exist) -- the classified rasters
inside the bundles are the only PNGs left. A per-tile bundle is 0.8-2.7 MiB.

**It looks right.** `prototype/shoot.sh` screenshots the generated viewer; `prototype/shots_vonly/`
has the region at z13 and a z15 view of the Iller valley, with contours, vegetation, water, and the
OSM roads, railway, paths and buildings composited in karttapullautin's own order. Headless
screenshots race MapLibre's tile loading, so some come out half-drawn -- that is the screenshot, not
the map.

## 5. A karttapullautin bug this run found

The first full run left an 85 MB `pullautus.geojson` in each grid's work directory.
`process_tile` wrote the whole extent's GeoJSON whenever `output_geojson` was set, and
`batch_process` calls `process_tile` once per tile -- so a batch render wrote the entire
uncropped collection again for every tile, on top of the per-tile file it actually wanted. Tens
of megabytes of geometry per tile that nothing reads, and the last one left on disk.

Fixed on the karttapullautin branch (`!config.batch`), rebuilt, and the run repeated: the stray
files are gone, the work directory dropped from 622 MB to 493 MB, and the pyramid is identical
layer for layer in all 95 tiles. It belongs in the upstream PR as part of the GeoJSON commit --
see `HANDOFF-karttapullautin.md` §4.

## 6. What production still has to do

1. **`containers/tiler/Containerfile` has not been built.** It was rewritten here -- tippecanoe
   2.78 built from source in a first stage, then a pinned Python set including `pyshp`, with
   `karttapullautin2tiles` dropped because nothing uses it any more. The end-to-end run used the
   prototype's single image instead (`prototype/image/Containerfile`, Debian's tippecanoe 2.53 and
   the same Python packages), so the *contents* are exercised and the multi-stage build is not.
   Build it, run `containers/smoke.sh tiler`, and check the runtime `libsqlite3-0` is enough for
   the tippecanoe binaries copied out of the builder stage.
2. **Rename the image everywhere it is published.** `containers/k2t/` became `containers/tiler/`,
   which means the GHCR path changes and `conf/containers.config` points at
   `ghcr.io/grst/mapant-nf/tiler:latest`, which does not exist yet.
3. **Consider matching the shapes once per grid instead of once per parent.** `osm_shapes.py` runs
   in `MAKE_VECTOR_TILES`, so each parent re-matches the whole archive of every grid it draws from,
   and stages that archive (5.7 MiB for Immenstadt's) once per parent. Matching is cheap -- a few
   seconds and ~13 MB of GeoJSONL per parent here -- so this is about staging, not CPU. The tidier
   arrangement is a match inside `OSM_TO_SHAPES`, which already runs per grid, handing the tiler a
   gzipped GeoJSONL; it also moves `pyshp` out of the tiler image and into the gdal one. Note that
   the deduplication would have to move with it -- into whatever step first sees two grids' shapes
   together, which would then be the tiler. It was left alone here because the current shape is
   what got validated end to end.
4. **`bin/pack_pmtiles.py` does not exist yet.** The webapp's production tile source is a PMTiles
   archive. Someone has to pack `tiles_vector/` into one -- python `pmtiles` writer, gzipping each
   tile on the way in, since the pyramid is written uncompressed (`--no-tile-compression`) for plain
   static hosting. Deliberately a post-run tool and not a process: a step that consumes every tile
   is a reduction barrier over the whole run.
5. **CI.** `tests/test_stub_wiring.sh` runs the whole pipeline stubbed and needs no container;
   `tests/requirements.txt` has gained `pyshp`. Check the unit job's install still resolves on the
   Python it pins (3.13, chosen to match the tiler image's wheels).
6. **Decide the base and deepest zoom for production.** The zoom plan is written relative to
   `max_zoom`, so it follows whatever those become; but the table in §3 was reasoned about at a 1 km
   grid and z13-16. Re-read it before trusting it at another scale.
7. **`png_variant` is now vestigial.** The images are deleted after the bundles are made, so the
   parameter only decides which of them is pruned a moment earlier. Remove it, or give it a reason.

## 7. Things that will surprise the next person

* **The rendered images are deleted.** `run_pullauta.py` keeps them until the bundles exist --
  an image closed with an IEND chunk is how it knows a tile finished, and how karttapullautin knows
  not to render it again inside the attempt ladder -- and then removes them. Nothing downstream
  reads them, and at ~1.5 MB a tile they would be a hundred gigabytes of live work directories
  across Bavaria. A Nextflow retry gets a fresh work directory, so nothing depends on them
  surviving the task.
* **`blocks` and `water` are both empty with the shipped ini, and a default karttapullautin render
  draws neither.** They are two separate opt-ins, not consequences of `vege_bitmode`:
  `_blocks_bit.png` needs `detectbuildings=1` (the block detection), and `_water_bit.png` needs
  `waterclass`, `buildingsclass` or `waterelevation` (the blue-and-black ground detail image).
  `assets/pullauta.ini` sets none of them, so neither raster is written -- karttapullautin now skips
  an all-background mask rather than exporting a file that polygonises to nothing -- and the tiler
  produces no such layer. The style and the OCAD export both draw them when they are there.

  Worth knowing before turning `water` on: its class 2 is *buildings*, from the LiDAR
  classification, which duplicates the OSM 526 layer where both exist. On the Bavarian data class 2
  is all it ever produced -- the tiles carry no class-9 water points at all -- so what the "water"
  raster contributed there was a second, worse copy of the buildings. `detectbuildings=1` is the
  one of the two worth considering: stony ground is real orienteering content that OSM does not
  have. It costs a pass over the point cloud per tile and the upstream ini calls it "highly
  experimental".
* **Power lines never appear**, whatever the rules say. `ogr2ogr`'s default OSM `lines` layer has no
  `power` attribute, so `power line|516|power!=` matches nothing -- in this matcher and equally in
  karttapullautin's. Fixing it means an `osmconf.ini` with `power` added to the exposed fields.
* **The OSM archive is joined to the *tile*, not to the parent**, and the list is `unique()`d per
  parent. Staging the same path twice under one name is an error, and a parent takes one archive
  from every tile under it.

## 8. Gaps that affect this repo

* **Undergrowth areas are two to three times the area of the stripes** the map draws, because
  karttapullautin marks each 18 m sample with a ~16 m disc. In the right place since the
  karttapullautin fix, but generous.
* **Fences and power lines lose their ticks and pylon bars** -- a style cannot draw a symbol along a
  line. MapLibre's `line-pattern` with a sprite image could do both; the sprite already exists for
  undergrowth and marsh.
* **Elevation labels are not drawn.** Contours carry `e`, so a symbol layer could.
* **There is no raster product any more.** If one is ever wanted again -- for a printed map, or for
  the webapp's basemap -- the thing to revisit first is §1: two renderers drawing OSM from two code
  paths is exactly what this arrangement avoids, and the cheap answer is to rasterise the vector
  tiles rather than to bring `MAKE_TILES` back.

See `HANDOFF-known-gaps.md` next to the repositories for the list that spans all three.

## 9. How to verify

```sh
.venv/bin/pytest tests/                    # 79
tests/test_config_profiles.sh              # 22
PATH=".venv/bin:$PATH" tests/test_stub_wiring.sh   # 23
nextflow lint .
shellcheck tests/*.sh tests/stub_pullauta containers/*.sh containers/karttapullautin/pullauta
```

End to end (needs a container runtime and downloads LiDAR, ~20 min, ~5.4 GB):

```sh
nextflow run . -profile <engine>,test_immenstadt --outdir results
```

Then check, in this order:

* `results/tiles_vector/` holds only z(base)..z(max), and the z(base) tiles are exactly the rows of
  `results/pipeline_info/parent_tiles.csv`;
* `grep duplicate work/*/*/.command.log` -- a parent that draws from two grids should report
  collapsing several thousand features. Nothing reported means either a one-grid region or a broken
  rule 4, and the second looks exactly like the first;
* no OSM feature appears twice in a tile, and none reaches past the rendered ground. Both are worth
  re-checking with the scripts the prototype used, which are next to the repositories:
  `prototype/compare_pyramids.py old/tiles_vector new/tiles_vector` compares two runs layer by
  layer;
* open `results/tiles_vector/index.html` and pan across a **parent boundary** at every zoom -- that
  is where rules 1 and 2 fail if they fail. The viewer keeps `#zoom/lat/lon` in the URL;
* look at a road that crosses a grid boundary: continuous, drawn once, and stopping at the edge of
  the rendered area rather than running on into the buffer.
