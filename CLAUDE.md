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
           containers/karttapullautin/pullauta scripts/*.sh
.venv/bin/pytest tests/               # 37 tests: plan_grids' geometry, run_pullauta's recovery ladder

# One test, one case
.venv/bin/pytest tests/test_plan_grids.py::test_tile_size_inference_uses_the_mode_not_the_mean -v

# The whole DAG offline: stubbed processes, no containers, no data (~1 min)
PATH="$PWD/.venv/bin:$PATH" tests/test_stub_wiring.sh

# Real runs. The first three need testdata/ (~52 GB, not in the repo)
nextflow run . -profile podman,test_local          # ~15 min, no network
tests/test_grid_independence.sh                    # ~10 min
tests/test_failure_injection.sh                    # ~8 min
nextflow run . -profile podman,test_immenstadt     # ~15 min, downloads ~7.4 GB

# Containers
containers/build.sh [name ...]        # builds mapant/<name>:{version,latest}
containers/build.sh --manifest       # names/tags/build-args as JSON; CI's single source of truth
containers/smoke.sh k2t              # per-image checks; also runs in CI on every build
nextflow run . -profile podman,local_images,test_local   # use locally built images
```

Python for tests and for `bin/*.py` outside a container: `python -m venv .venv &&
.venv/bin/pip install -r tests/requirements.txt`. `nextflow run` with no container profile executes
processes locally, so a stub run needs those packages on `PATH`.

`shellcheck` is not installed in the devcontainer — CI installs it, and the static release tarball
runs fine here. Nothing in `bin/` is shell any more; what is left is tests, container helpers and the
ISA wrapper. `.shellcheckrc` disables three style checks with the reasoning; everything else is a hard
failure. Workflow files are checked with `actionlint` (also not installed; it embeds shellcheck for
`run:` blocks, so run it with shellcheck on `PATH`).

## Architecture

`main.nf` wires seven processes, one per file under `modules/local/<name>/main.nf`. All non-trivial
logic lives in `bin/` so it can be tested without Nextflow; a module body should only marshal
parameters.

The pyramid spans `base_zoom`..`max_zoom` only. There is deliberately no overview step: below the base
zoom the generated viewer shows OSM's own raster tiles, which avoids a reduction barrier over every
finished tile at the end of a run.

The scale is what shapes everything: 15+ TB of input, ~72,000 tiles, ~3,000 CPU-hours. Nothing may be
downloaded up front and no intermediate may outlive the task that made it.

**`PULLAUTA_GRID` is deliberately one process** that downloads, checksums, renders and deletes. Its
`trap` removes karttapullautin's temporaries before the task ends — karttapullautin never cleans up
after itself, and `savetempfiles`/`savetempfolders` control extra *outputs*, not cleanup. Without the
trap a failed grid strands ~30 GB, because Nextflow keeps a failed task's directory.

Three upstream facts the design depends on, all established by reading karttapullautin's source:

1. **The halo is 127 m.** `batch_process` builds each tile from every input within 127 m of it, then
   crops back. Tiles are kilometres across, so one ring of neighbours always covers that reach: a tile
   renders byte-identically regardless of batch size, and grids can be partitioned freely.
   `tests/test_grid_independence.sh` proves it byte-for-byte; don't weaken it.
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

Per-grid OSM extraction is a feasibility requirement, not an optimisation: karttapullautin unzips its
shapefile archive per invocation and re-lists it per tile, so a country-wide archive would be
unpacked once per grid and scanned a hundred times.

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
  `stageAs` plus a copy for this reason, and the local-laz directory is staged as `laz_local_src`
  because staging it as `in` would collide with the download target and write into `testdata/`.
- **No `$projectDir` inside a process body**: on an executor without a shared filesystem that path
  does not exist. Stage the file as an input instead. The same rule rules out bind mounts and any
  host-absolute path in committed config.
- **The end-of-run `Outputs:` listing prints each channel item in full**, capping the *number of
  items* (ten, once there are more than twenty) but not their size. A published item that is a list
  of a task's files therefore prints every one of them: `MAKE_TILES.out.tiles` is a whole z11..z18
  subtree, ~22,000 paths per task. `main.nf` flattens the tiles channel before publishing so the cap
  applies per file; it is not a no-op, and removing it puts megabytes of tile names on the console.
- **Never assert on Nextflow's console output.** The end-of-run summary is written by whichever log
  observer is active: ANSI in a terminal, plain in CI, and a third `[SUCCESS] completed=… cached=…`
  format when `NXF_AGENT_MODE`, `AGENT` or `CLAUDECODE` is set — which is why a test can be green in
  an agent-driven shell and fail in CI with nothing else changed. Assert on published artifacts;
  `pipeline_info/trace.txt` has a status per task, including `CACHED`. `tests/test_stub_wiring.sh`
  checks `-resume` that way.
- **The interactive shell here is zsh**, where `"$var:tag"` eats `:t`/`:c`/`:h` as history modifiers.
  Use `"${var}:tag"` — this has produced a mis-tagged image and a broken `git rev-parse` already.

## Containers

Four images, one per process family, chosen per process by `withName` selectors in
`conf/containers.config` — images are config, not parameters. All are root-only with `bash` and
`procps`: Nextflow shells out to `ps` for task metrics, and in this devcontainer container UID 0 is the
only UID that exists, so apt needs `-o APT::Sandbox::User=root`. `containers/build.sh` explains both
constraints in full; a `USER nonroot` directive produces an image that cannot start.

`karttapullautin` compiles the pinned upstream tag three times (`x86-64`, `-v3`, `-v4`) with an
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

`testdata/` (~52 GB of LiDAR and OSM, plus a prototype run) is git-ignored and not distributable. The
small CSVs derived from it in `tests/fixtures/` **are** tracked. `tests/test_failure_injection.sh`
skips itself when `testdata/` is missing; `tests/test_grid_independence.sh` and `-profile test_local`
do not, so run them only where the LiDAR is present.
