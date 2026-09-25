# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the design document and explains *why* each decision was made; this file is the
operating manual. `prompt.md` is the original brief plus a running list of requested simplifications —
read it before proposing changes, because some of what looks like an improvement is already on that
list and some of it is a deliberate rejection of one.

## Commands

```bash
# Static checks -- all four must stay clean; CI runs exactly these
nextflow lint .                       # strict v2 parser
tests/test_config_profiles.sh         # every profile resolves; derived config tracks its params
shellcheck tests/*.sh tests/stub_pullauta containers/*.sh \
           containers/karttapullautin/pullauta
.venv/bin/pytest tests/               # plan_grids' geometry, run_pullauta's recovery ladder,
                                      # fetch_laz's verdicts, the tiler's zoom plan, the style

# One test, one case
.venv/bin/pytest tests/test_plan_grids.py::test_tile_size_inference_uses_the_mode_not_the_mean -v

# The whole DAG offline: stubbed processes, no containers, no data (~1 min)
PATH="$PWD/.venv/bin:$PATH" tests/test_stub_wiring.sh

# Real runs. Both are self-contained -- their inputs are in assets/ -- but download from
# geodaten.bayern.de, so they are run by hand rather than in CI.
nextflow run . -profile podman,test_immenstadt     # ~20 min, downloads ~5.4 GB
tests/test_failure_injection.sh                    # ~10 min, downloads ~2.6 GB

# Containers
containers/build.sh [name ...]        # builds mapant/<name>:{tag,latest}
containers/build.sh --manifest       # names/tags/build-args as JSON; CI's single source of truth
containers/smoke.sh tiler            # per-image checks; also runs in CI on every build
# To run against those images, write the four withName selectors into a -c file; there is
# deliberately no profile for them (see the Quickstart in README.md).

# The README's metro map. nf-metro is a docs tool, not a test dependency, so it is not in
# tests/requirements.txt: uv pip install --python .venv/bin/python nf-metro==1.1.0 (2.x aborts on it)
nf-metro validate docs/metro_map.mmd  # cheap syntax check
# Regenerating it needs the exact flag set in the .mmd's header comment, which explains each one --
# rendering with the defaults produces a picture that is unreadable at the README's width.
```

Python for tests and for `bin/*.py` outside a container: `python -m venv .venv &&
.venv/bin/pip install -r tests/requirements.txt`. `nextflow run` with no container profile executes
processes locally, so a stub run needs those packages on `PATH`.

`shellcheck` is not installed in the devcontainer — CI installs it, the static release tarball runs
fine here, and `uv pip install --python .venv/bin/python shellcheck-py` puts the same binary at
`.venv/bin/shellcheck` without touching the system (the venv has no `pip` of its own). Nothing in `bin/` is shell any more; what is left is tests, container helpers and the
ISA wrapper. `.shellcheckrc` disables three style checks with the reasoning; everything else is a hard
failure. Workflow files are checked with `actionlint` (also not installed; it embeds shellcheck for
`run:` blocks, so run it with shellcheck on `PATH`).

## Architecture

`main.nf` wires eight processes, one per file under `modules/local/<name>/main.nf`. All non-trivial
logic lives in `bin/` so it can be tested without Nextflow; a module body should only marshal
parameters.

**The pipeline builds one map, a PMTiles archive of vector tiles, and karttapullautin does all of
its cartography.** It is given each grid's LiDAR *and* its OSM shapes, and with `vectorvege=1` and
`geojson_wgs84=1` (set by `bin/render_ini.py`; `epsg` per grid by `bin/run_pullauta.py`) writes each
tile's map as one GeoJSON per layer, in WGS84, already in its published form: contours generalised
and broken around the knolls, cliff dashes chained into lines, vegetation traced, OSM shapes matched
to their ISOM codes and cropped per tile. The pipeline does not open those files. That requires the
karttapullautin branch built on @malpou's fork (`feature/vector-stack`, grst/karttapullautin#3; see
`HANDOFF-malpou-stack.md` next to the repositories), which is what the image is built from, pinned
to a commit. Point it back at an upstream release once that work is released.

`docs/metro_map.mmd` restates that wiring by hand for the README's metro map, and **nothing checks
that the two still agree** — adding, removing or re-plumbing a process means editing it and
re-rendering, or the picture at the top of the README quietly starts lying. Its station ids are the
process names on purpose, which is also what lets `nf-metro serve` light it up live.

The map spans `base_zoom`..`max_zoom` only, in 512 px tiles (MapLibre's zooms; z15 shows as much
per screen pixel as z16 in 256 px tiles). There is deliberately no overview step: below the base
zoom the viewer shows OSM's own raster tiles.

The style is a template, `assets/viewer/style.json`, plain MapLibre JSON that Maputnik can edit.
`MAKE_VIEWER` (`bin/make_viewer.py`) fills in only what depends on the run -- widths in ground
metres at the region's latitude, the colours karttapullautin took from the ini, the undergrowth
patterns per zoom -- and draws the sprite. Nothing in it may assume Bavaria: the pipeline is meant
for any region, which is why the latitude comes from the plan and the LiDAR credit is a parameter.

`MAKE_VECTOR_TILES` hands the bundles' files to tippecanoe unaltered, one `--named-layer` per
file, the layer being the name karttapullautin gave the file. One task per base-zoom parent, each
writing its own `.pmtiles`, `groupKey` fan-in, `remainder: true`. `MERGE_PMTILES` joins them with
`tile-join`: the one step that waits for the whole run, and unavoidably so -- an archive has one
directory -- but it stages a few hundred archives, never the tiles. Three things are load-bearing:

- **Nothing may be left out to fit a budget.** Every one of tippecanoe's thinning options
  (`--drop-densest-as-needed` and the rest) decides per tile, from whatever happens to be in it, so
  two parents cutting the same zoom disagree about what the map contains. That is visible as content
  appearing and disappearing along the line where two parents meet -- a lake on an overview tile in
  one and not the other -- and it also truncated the *deepest* zoom, which is the one the OCD export
  in mapant-bayern reads. They are all off (`--no-tile-size-limit`, `--no-feature-limit`,
  `--drop-rate=1`); what each zoom shows is the zoom plan in `make_vector_tiles.py`, passed as a
  `--feature-filter` on each feature's own `isom` and `$zoom`, so it depends on the feature alone.
- **Two shades of green share their boundary vertex for vertex**, because karttapullautin traces them
  from one grid. `--detect-shared-borders` and `--no-simplification-of-shared-nodes` keep tippecanoe
  from simplifying that boundary twice, which would leave a sliver of white paper between them.
- **`--clip-bounding-box` clips geometry but still writes tiles outside the parent** when their
  buffer reaches in. They hold this parent's side of the border, and the neighbour's copy of the
  same tile holds the other side; `tile-join` merges the layers of a tile present in several
  inputs, so the merged tile has both. Do not prune them: that loses the buffer across every parent
  border. `--no-tile-size-limit` on `tile-join` matters for the same reason as on tippecanoe.

The scale is what shapes everything: 15+ TB of input, ~72,000 tiles, ~3,000 CPU-hours. Nothing may be
downloaded up front and no intermediate may outlive the task that made it.

**`PULLAUTA_GRID` is deliberately one process** that downloads, checksums, renders and deletes. Its
`trap` removes karttapullautin's temporaries before the task ends — karttapullautin never cleans up
after itself, and `savetempfiles`/`savetempfolders` control extra *outputs*, not cleanup. Without the
trap a failed grid strands ~30 GB, because Nextflow keeps a failed task's directory.

Three upstream facts the design depends on, all established by reading karttapullautin's source:

1. **The halo is 127 m.** `batch_process` builds each tile from every input within 127 m of it, then
   crops back. Tiles are kilometres across, so one ring of neighbours always covers that reach: a tile
   renders byte-identically regardless of batch size, and grids can be partitioned freely. It was
   verified by rendering one tile alone and again inside a 2x2 block and `cmp`-ing the PNGs (pin
   `PULLAUTA_ISA`, or the two builds' autovectorisation makes the comparison meaningless); the
   standing check is that `-profile test_immenstadt`'s two grids leave no seam between them.
2. **karttapullautin skips a file whose output PNG already exists**, while still reading it for the
   halo. `bin/run_pullauta.py` exploits this twice: empty placeholder PNGs suppress rendering of ring
   tiles (a third of the compute), and the same trick blacklists a tile that panics without degrading
   its neighbours.
3. **A panic aborts the whole process.** `bin/run_pullauta.py` is an attempt ladder that quarantines
   half-written output, drops to `processes=1` to attribute the panic to a tile, blacklists it, and
   records coordinates plus backtrace in `failures.tsv` for the upstream developers.

`PLAN_GRIDS` (`bin/plan_grids.py`) owns all geometry: the grid lattice on absolute indices (so grid
identity survives a `--region_bbox` change, and `-resume` still hits), the one-ring halo by lattice
index, lon/lat envelopes via `transform_bounds(densify_pts=21)`, and the tile→web-mercator-parent map
with each parent's core-tile count. The halo pool is deliberately the **whole** CSV, not the filtered
selection, so a `--region_bbox` run renders identically to a full one.

OSM extraction is per grid: each `PULLAUTA_GRID` task stages its own grid's archive into
karttapullautin's input folder as `map.shp.zip`, and the country-wide archive is never staged
anywhere. A grid with nothing drawable carries a `<grid>.NONE` sentinel instead, which is staged as
nothing -- only a `.shp.zip` is copied, because karttapullautin would try to unzip anything else.
Neighbouring extracts overlap by `osm_buffer_m`, which no longer matters: karttapullautin crops the
shapes per tile, so each piece of a road is written by exactly one tile.

## Traps that have already cost time

- **`groupTuple` discards incomplete groups** when the key carries a size. Without `remainder: true`
  one failed tile silently deletes every map tile overlapping it *and the run reports success*. This
  is the exact requirement the pipeline exists to satisfy; `tests/test_failure_injection.sh` exists
  because of it.
- **`outputDir = params.outdir` must stay below the `profiles` block** in `nextflow.config`. Above
  it, a profile's params have not merged yet, so it captures the default and silently publishes to
  the wrong place.
- **Nextflow 26.04 defaults to the strict v2 parser**: no top-level statements, no implicit `it`, no
  `for`/`while` in a workflow body, `channel.` not `Channel.`. `nextflow lint .` is the gate.
- **A one-item queue channel pairs with exactly one consumer.** `RENDER_INI.out.ini.first()` makes it
  a value channel; without `.first()` every grid but one starves.
- **Never edit a staged input in place** — it is a symlink to the user's file. `RENDER_INI` uses
  `stageAs` plus a copy for this reason.
- **No `$projectDir` inside a process body**: on an executor without a shared filesystem that path
  does not exist. Stage the file as an input instead. The same rule rules out bind mounts and any
  host-absolute path in committed config.
- **Never assert on Nextflow's console output.** The end-of-run summary is written by whichever log
  observer is active: ANSI in a terminal, plain in CI, and a third `[SUCCESS] completed=… cached=…`
  format when `NXF_AGENT_MODE`, `AGENT` or `CLAUDECODE` is set — which is why a test can be green in
  an agent-driven shell and fail in CI with nothing else changed. Assert on published artifacts;
  `pipeline_info/trace.txt` has a status per task, including `CACHED`. `tests/test_stub_wiring.sh`
  checks `-resume` that way.
- **The interactive shell here is zsh**, where `"$var:tag"` eats `:t`/`:c`/`:h` as history modifiers.
  Use `"${var}:tag"` — this has produced a mis-tagged image and a broken `git rev-parse` already.
- **podman never re-pulls a tag it already has**, and three of the four images are referenced as
  `:latest`. A working tree newer than the local image cache therefore runs against whatever was
  pulled weeks ago: a bumped image against a stale cache fails minutes into a run with an error --
  a missing `tippecanoe`, an option the old version did not have -- that points at anything but the
  image. `podman pull` the four images
  before trusting a red end-to-end run — a fresh machine, having no cache, is unaffected.

## Containers

Four images, one per process family, chosen per process by `withName` selectors in
`conf/containers.config` — images are config, not parameters. All are root-only with `bash` and
`procps`: Nextflow shells out to `ps` for task metrics, and in this devcontainer container UID 0 is the
only UID that exists, so apt needs `-o APT::Sandbox::User=root`. `containers/build.sh` explains both
constraints in full; a `USER nonroot` directive produces an image that cannot start.

`karttapullautin` compiles the pinned commit three times (`x86-64`, `-v3`, `-v4`) with an
explicit `--target x86_64-unknown-linux-gnu`, so `RUSTFLAGS` reaches only target artifacts and the v4
pass does not SIGILL while running its own build script on an AVX2 machine. A wrapper dispatches on
`/proc/cpuinfo` per invocation; `PULLAUTA_ISA` overrides it, which is what makes byte comparisons
between runs meaningful.

CI decides whether to rebuild from the **git tree hash of `containers/<name>/`**, published as a
`ctx-<hash>` tag. Consequences: pin every dependency you add (an unpinned one means the same hash can
produce different images, and a newer version is never picked up because nothing triggers a rebuild),
and don't add build inputs from outside that directory.

## Development environment

Rootless podman nested inside a rootless-podman devcontainer. **Only podman works** — Docker needs a
daemon and a tun device, Apptainer needs `/dev/fuse` or `CAP_SYS_ADMIN`, and none of that can be
created from in here. `scripts/setup-container-runtime.sh` is idempotent and documents each setting.
cgroups are not delegated, so a `memory` directive is only a scheduling hint locally while being a
hard OOM limit on a real node.

`testdata/` (~52 GB of LiDAR and OSM, plus a prototype run) is git-ignored and not distributable, and
**nothing in the repository depends on it any more**. The subsets derived from it that the tests need
are tracked: `assets/laz_tiles_immenstadt.csv` and `assets/immenstadt.osm.pbf` (the two inputs of
`-profile test_immenstadt`, which `tests/test_failure_injection.sh` also runs against) and the
fixtures in `tests/fixtures/`. Keep it that way — a test that reaches into `testdata/` is a test only
this machine can run.

The `test_*` line in `.gitignore` is anchored (`/test_*`) for the same reason it had to be: unanchored
it matches at every level, so every new file added under `tests/` is silently ignored.
