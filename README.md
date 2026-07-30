# mapant

[![tests](https://github.com/grst/mapant-nf/actions/workflows/ci.yml/badge.svg)](https://github.com/grst/mapant-nf/actions/workflows/ci.yml)
[![containers](https://github.com/grst/mapant-nf/actions/workflows/containers.yml/badge.svg)](https://github.com/grst/mapant-nf/actions/workflows/containers.yml)

A Nextflow pipeline that turns a list of LiDAR tiles into a web-mercator PNG tile pyramid —
an automatically generated orienteering map, in the style of
[mapant.fi](https://mapant.fi). Built to produce a map of all of Bavaria from ~15 TB of open
LiDAR data, but nothing in it is Bavaria-specific: the input is a CSV satisfying
[`assets/schema_tiles.json`](assets/schema_tiles.json).

```
tiles.csv ──► PLAN_GRIDS ──┬─► grids/<grid_id>.csv    (which tiles render together)
  osm.pbf                  ├─► grids.csv
  pullauta.ini             ├─► parent_tiles.csv       (tile ⇄ web-mercator tile)
                           └─► osm_chunks/*.json

  OSM_EXTRACT ──► OSM_TO_SHAPES ──► per-grid map.shp.zip
                                             │
                    PULLAUTA_GRID  ◄──────────┘   download → verify → render → delete
                            │
                    MAKE_TILES ──► TILE_OVERVIEWS ──► tiles/{z}/{x}/{y}.png + index.html
```

It builds on [karttapullautin](https://github.com/karttapullautin/karttapullautin) for the
cartography and [karttapullautin2tiles](https://github.com/grst/karttapullautin2tiles) for the
pyramid, and automates the manual process documented in
[mapant-bayern](https://github.com/grst/mapant-bayern).

## Quickstart

```bash
bash scripts/setup-container-runtime.sh   # only in this devcontainer; idempotent

# ~10 minutes, no network traffic, renders from testdata/
nextflow run . -profile podman,test_local

# open results_test_local/tiles/index.html
```

Then a real region, downloading from the source server:

```bash
nextflow run . -profile podman,test_immenstadt
```

And a run of your own:

```bash
nextflow run . -profile podman \
    --tiles_csv my_tiles.csv \
    --osm_pbf my_region.osm.pbf \
    --grid_size 10 \
    --outdir results
```

`nextflow run . --help` lists every parameter with its documentation
([`nextflow_schema.json`](nextflow_schema.json)).

The four container images are pulled from
[GHCR](https://github.com/grst/mapant-nf/pkgs/container/mapant-nf%2Fkarttapullautin), where CI
publishes them from `main`; nothing needs building to run the pipeline. To use images built on this
machine instead — which is what you want while editing a `Containerfile` — build them and add one
profile:

```bash
containers/build.sh                                   # all four, tagged mapant/<name>:latest
containers/smoke.sh k2t                               # check one of them
nextflow run . -profile podman,local_images,test_local
```

## The input contract

One row per LiDAR file. `assets/schema_tiles.json` is the enforced definition; this is the
summary:

| Column | Required | Meaning |
| --- | --- | --- |
| `tile` | yes | bare `.laz`/`.las` filename, unique |
| `url` | yes | `http(s)://`, `s3://`, `gs://`, `file://` or a path |
| `size_bytes`, `sha256` | yes | checked before and after download |
| `crs` | yes | `EPSG:<code>` of the bbox below |
| `min_x`, `min_y`, `max_x`, `max_y` | yes | projected bounding box |
| `min_lon`, `min_lat`, `max_lon`, `max_lat` | no | derived with pyproj if absent |

Any other column is ignored, so a source-specific extra costs nothing — nf-schema logs a
harmless warning naming it. Tiles need not be uniform in size or form a complete lattice; holes
and ragged edges are handled.

**Checksums are mandatory, and load-bearing.** A truncated laz does not make karttapullautin
fail: it renders whatever points it managed to read and produces a plausible but wrong map. The
checksum is the only thing that distinguishes "this tile is finished" from "this tile is
finished badly", which is why every file is verified on every attempt — including files taken
from `--laz_local_dir`.

## How it works, and why

### The halo is 127 m, so grid size is not a quality setting

The obvious design here is wrong. Rendering tiles one at a time produces edge artifacts, so the
instinct is to batch many tiles and throw the outer ones away — trading compute against quality
via the batch size.

But reading karttapullautin's source (`src/process.rs::batch_process`, v2.13.0) shows it already
solves this. For each tile it takes that tile's own bounding box, expands it by exactly **127 m**,
reads points from *every* laz file in its input folder overlapping that box, renders, and crops
back to the tile. So:

- a tile rendered with all neighbours within 127 m present is **byte-identical** to the same
  tile rendered in any larger batch — there is no residual artifact to trade against;
- with 1 km tiles that means **one ring of neighbours**, always;
- `grid_size` is therefore purely a download/disk trade-off.

`tests/test_grid_independence.sh` renders the same tile alone and inside a 2×2 block and compares
the bytes, so this claim is checked rather than believed. `-profile test_local` checks the visible
consequence: it renders 8 tiles as *two independent grids* and the finished 4 km × 2 km map has no
seam at the grid boundary — contours and vegetation run straight through it.

The halo is expressed as a distance (`params.pullauta_halo_m`, default 127) and resolved with a
spatial index, which is what makes non-uniform tile sizes and dataset holes work for free.

One consequence worth stating: the halo is drawn from the **whole** CSV, not from the
region-filtered selection. Otherwise a `--region_bbox` run would render its edge tiles from fewer
points than a full run would, and a small test region would not be a faithful sample.

### Grid size is a download trade-off

Each grid fetches its core tiles plus a one-tile ring, and the ring is re-fetched by every grid
that borders it. Bigger grids amortise that better but need more disk per concurrent task. At
Bavaria's mean of 208 MB/tile:

| `grid_size` | files per grid | laz | + temporaries | peak disk per task | download amplification |
| --- | --- | --- | --- | --- | --- |
| 2 | 16 | 3.4 GB | 6 GB | ~10 GB | 4.00× |
| 3 | 25 | 5.3 GB | 6 GB | ~12 GB | 2.78× |
| 5 | 49 | 10.3 GB | 12 GB | ~23 GB | 1.96× |
| **10** (default) | 144 | 30 GB | 24 GB | **~55 GB** | **1.44×** |
| 16 | 324 | 68 GB | 48 GB | ~117 GB | 1.27× |
| 20 | 484 | 101 GB | 48 GB | ~150 GB | 1.21× |

The knee is around 10–16. Each run's published `plan_summary.txt` reports its own numbers before
any data moves — check it first.

### Ring tiles are never rendered

karttapullautin queues a file only if `<batchoutfolder>/<stem>.png` does not already exist
(`src/plan.rs`), while the halo lookup consults *every* file in the folder. An empty placeholder
PNG therefore makes a file invisible to the renderer but still available as a source of points.
The pipeline uses that to skip rendering ring tiles entirely — at `grid_size` 10 the ring is 36%
of the folder, so this is a third of the compute — and gets free intra-grid resume as a
side effect.

### One process does download, verify, render and delete

At 15 TB the LiDAR cannot be staged as a Nextflow input, and cannot outlive the task that uses
it. `PULLAUTA_GRID` therefore fetches, checksums, renders and prunes in a single task, and
installs a `trap` so its temporaries are removed even when it fails or is retried — otherwise one
failed grid would strand ~30 GB and a hundred would fill the disk. After a run, `work/` holds only
PNGs — 20 MB after `test_local` processes 10 GB of LiDAR.

karttapullautin does not do this itself. `savetempfiles`/`savetempfolders` control extra
*outputs*, not cleanup: it never deletes its `temp{n}/` directories or its `temp{n}.xyz.bin`
files, which is why the prototype run in `testdata/kemptner_wald/` still contains four 1.5 GB of
them.

`params.scratch` maps to Nextflow's `scratch` directive and is off by default. It does work under
podman, but it places the task directory on the *container's* own filesystem — the wrong place
for tens of gigabytes. Turn it on when the node has a fast local disk the work dir is not on;
correctness does not depend on it either way.

### Surviving the renderer

karttapullautin v2 uses `.unwrap()` throughout, so one malformed tile aborts the whole process —
and because `launch_threads` joins worker 1 first, a panic anywhere abandons up to
`processes - 1` other tiles mid-write. Collecting those crashes for an upstream bug report,
without losing the other ninety tiles in the grid, works like this:

1. Run. On failure, delete any half-written output — a PNG without a trailing `IEND` chunk, or an
   incomplete quartet — so the renderer's own resume logic picks those tiles up again instead of
   skipping them forever and shipping them corrupt.
2. If an attempt made no progress, retry with `processes=1`. The last `<in> -> <out>` line in the
   log is then unambiguously the tile that killed it.
3. Record that tile in `qc/pullauta_failures.tsv` with its coordinates, URL, checksum, the panic
   message, the karttapullautin version and the ISA variant — everything a filable issue needs —
   and blacklist it with a placeholder. Blacklisting rather than deleting the laz is what keeps
   its *neighbours* correct; removing the file would silently degrade their borders.

`tests/test_run_pullauta_recovery.sh` exercises all of this against a stub renderer that
reproduces karttapullautin's failure behaviour, because the tile that triggers the real bug is by
definition not one we have.

Exit status is a deliberate contract, so that Nextflow retries what retrying can fix:

| Situation | Exit | Result |
| --- | --- | --- |
| Some tiles blacklisted, others rendered | 0 | published, with rows in `qc/pullauta_failures.tsv` |
| A core tile permanently unavailable (404, bad checksum) | 0 | a recorded hole in the map |
| A halo tile unavailable | 0 | warned; slightly worse border |
| Timeout, 5xx, full disk, OOM | non-zero | retried, up to 3 times |
| Nothing rendered at all | non-zero | retried, then **ignored** |

That last row matters: after its retries a hopeless grid is *ignored* rather than failing the run,
because one unrenderable grid must not end a run that has already spent days and terabytes on the
others. The cost is that the run finishes green with a gap, so `workflow.onComplete` writes
`qc/failed_grids.txt` and logs a warning whenever that happens.

### Per-grid OSM extraction is required, not an optimisation

karttapullautin unzips the shapefile archive once per invocation and then re-lists the unpacked
directory *for every tile it renders*. Handing it the Bavaria-wide `map.shp.zip` would mean
unpacking 30 GB per grid task and walking it a hundred times. So `OSM_EXTRACT` cuts per-grid
extracts — several per osmium pass, because one pass reads the whole source `.pbf` — and
`OSM_TO_SHAPES` reprojects each into its grid's CRS. `--strategy=smart` plus a 2 km buffer keeps
ways and multipolygon relations whole, so nothing a grid needs is lost.

The `.pbf` is optional: without it, karttapullautin renders contours, cliffs and vegetation only.

### Tiling

`parent_tiles.csv` maps every tile to the web-mercator tiles it feeds, computed from densified
bounds (`pyproj.transform_bounds(densify_pts=21)`) rather than from four transformed corners — a
UTM box's edges are curves in lon/lat, so the corner-only envelope is *too small* and produces
thin missing slivers. It carries each parent's expected contributor count, which becomes a
Nextflow `groupKey`, so tiling of a finished parent starts while other grids are still rendering
instead of waiting on a barrier over all 72,000 tiles.

That needs `groupTuple(remainder: true)`. When the key carries a size, groupTuple **discards** any
group that never reaches it — so one tile failing to render would silently delete every map tile
overlapping it while the run still reported success. `tests/test_failure_injection.sh` exists
because that bug was in this pipeline until it caught it.

One task per base-zoom tile bounds memory (k2t holds every overlapping source render in one array:
~1.1 GB at z12 for 0.42 m/px sources). Because k2t's per-parent subtrees are disjoint, publishing
many tasks into one pyramid is a plain union.

Zoom levels below the base are built by halving finished tiles rather than by asking k2t for them.
That is both far cheaper and *more* accurate: k2t treats each output tile as linear in lon/lat,
an approximation that is excellent at high zoom and worst exactly at low zoom.

### AVX-512

`prompt.md` requires that a machine supporting AVX-512 gets an AVX-512 build. Upstream publishes
no such binary, so `containers/karttapullautin` compiles the pinned tag three times —
`x86-64`, `x86-64-v3`, `x86-64-v4` — and a wrapper dispatches on `/proc/cpuinfo` at every
invocation, logging its choice. `PULLAUTA_ISA` overrides it, which is what makes byte comparisons
between runs meaningful (different ISA levels are not guaranteed to produce identical pixels).

Each `cargo build` passes `--target x86_64-unknown-linux-gnu` explicitly. Without it, `RUSTFLAGS`
also applies to build scripts and proc macros, so the v4 pass would compile a build script with
AVX-512 and immediately execute it on the build machine — `SIGILL` on anything older than Ice
Lake. Naming the target makes cargo treat it as a cross-compile, which is what lets an AVX2-only
laptop build the AVX-512 binary. On this machine, `PULLAUTA_ISA=v4 pullauta` exits 132 (SIGILL),
which is the proof that the v4 binary really is different.

Since the requirement cannot be *executed* on a machine without AVX-512, `containers/smoke.sh`
verifies it by disassembly instead, which works anywhere: the v4 build uses 1453 `zmm` operands,
the v3 and baseline builds none. CI runs that check on every build of the image.

## Tuning

| | laptop (16 CPU, ~7 GB free, 168 GB) | `c8id.32xlarge` (128 vCPU, 256 GiB, 3.8 TB NVMe) |
| --- | --- | --- |
| `grid_size` | 2–3 | 16 |
| `pullauta_processes` | 2 | 16 |
| `PULLAUTA_GRID` `maxForks` | 1 | 7 |
| `base_zoom` | 13 | 12 |
| `max_zoom` / `min_zoom` | 16 / 9 | 18 / 8 |
| `publish_mode` | `copy` | `link` |

Disk, not CPU, is the binding constraint: peak usage is `maxForks × peak disk per task` from the
table above, and it all lives under `workDir`.

### A full Bavaria run on one node

`-profile c8id` sets the above. Two things to arrange outside the pipeline: put `workDir` on the
striped instance NVMe (`-w /mnt/nvme/work`), and publish somewhere durable, because instance
storage is lost when the instance stops.

| Phase | Work | On 128 vCPU |
| --- | --- | --- |
| Render | 71,979 tiles × 2.0–3.1 CPU-min ≈ 2,400–3,700 CPU-h | 21–32 h |
| Tiling | ~1,600 z12 parents, ~50 CPU-h | ~1 h |
| Download | ~19 TB at `grid_size` 16 | **see below — probably the binding constraint** |

**Compute is about a day and a half. Whether the whole run takes that long depends entirely on how
fast the data can be fetched, and on the evidence available here it cannot.**

The render figure is measured, not guessed, from four grids across two regions:

| Region | mean tile | CPU-s per tile |
| --- | --- | --- |
| Kemptner Wald (`test_local`) | ~400 MB | 171, 186 |
| Immenstadt (`test_immenstadt`) | ~166 MB | 116, 123 |

Bavaria's mean is 208 MB/tile, between the two, so ~2.5 CPU-min/tile is the central estimate. It
should carry across grid sizes, because karttapullautin re-reads a tile's neighbours for *every*
tile regardless of how many share the batch.

### The download is the part to worry about

Measured from this development machine against `geodaten.bayern.de`: **29 MB/s on one stream, and
30 MB/s across four in parallel.** The cap is on the total, not per connection, so adding streams
from one client buys nothing. At that rate 19 TB takes **more than seven days** — five times the
compute — and no amount of `maxForks` changes it.

What is not known from here is *whose* limit that is: ~30 MB/s is also almost exactly a 250 Mbit
consumer uplink, so it may be this laptop rather than the server. A c8id has 50 Gbps and may see
far more. **Benchmark from the machine that will do the run, before planning around a number:**

```bash
curl -s -o /dev/null -w '%{speed_download} B/s\n' \
    https://geodaten.bayern.de/odd_data/laser/590_5269.laz
```

Below ~200 MB/s sustained, the run is download-bound and worth restructuring — ask the provider
about a bulk transfer or a mirror rather than pulling 72,000 files over HTTP. Above it, the compute
figures above dominate and the estimate is a day and a half.

Recalibrate the render side the same way:

```bash
nextflow run . -profile podman,test_immenstadt
awk -F'\t' '$2 ~ /PULLAUTA_GRID/ {print $4, $8, $10}' \
    results_immenstadt/pipeline_info/trace.txt
# realtime x %cpu / core-tile count = CPU-seconds per tile
```

One thing to know before reading those numbers: karttapullautin uses far more threads than its
`processes` setting implies — `image` and `imageproc` are built with rayon and size their pools
from the machine's core count, and `parallel_laz_decompression` adds more. Uncapped, a 2-CPU task
measured **677%** CPU. `PULLAUTA_GRID` therefore exports `RAYON_NUM_THREADS=$task.cpus`, which
brought it to 194% and, unexpectedly, *reduced* total CPU work per tile by 36% (267 → 178 CPU-s)
while roughly doubling each grid's wall time. For a throughput-bound batch job that is the right
trade, and it is what makes the `maxForks` arithmetic above meaningful. `pullauta_processes` is the
single dial if you would rather have latency: it raises the worker count and the rayon cap
together.

Granite Rapids has AVX-512, so the v4 build is selected there — check for
`pullauta: ISA variant v4` in `qc/pullauta.*.log`.

**Before pulling terabytes from a public open-data server, talk to whoever runs it.**
`download_jobs` × concurrent grids × nodes is how hard this pipeline hits them;
`download_limit_rate` and `maxForks` are the brakes.

## Tests

The first five rows run in CI on every pull request and every push to `main`
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)). The rest need the ~52 GB of LiDAR in
`testdata/`, or a multi-gigabyte download, so they are run by hand before a release.

| Command | What it establishes | Time | CI |
| --- | --- | --- | --- |
| `pytest tests/` | planning geometry and the CSV schema — 34 tests | 1 s | ✅ |
| `bash tests/test_run_pullauta_recovery.sh` | crash recovery against a stub renderer — 30 assertions | 10 s | ✅ |
| `bash tests/test_stub_wiring.sh` | the whole DAG with fabricated outputs: joins, fan-in, nested publishing, `-resume` — 21 assertions | 1 min | ✅ |
| `bash tests/test_config_profiles.sh` | every profile resolves and derived settings track their params | 30 s | ✅ |
| `nextflow lint .` + `shellcheck` | strict-parser clean; scripts clean | 10 s | ✅ |
| `nextflow run . -profile podman,test_local` | end to end, no network: 8 tiles, 2 grids, **no seam at the grid boundary**, `work/` left at 20 MB | ~15 min | |
| `bash tests/test_grid_independence.sh` | a tile renders byte-identically alone and batched | ~10 min | |
| `bash tests/test_failure_injection.sh` | a corrupt laz leaves one hole, is reported twice, and does not stop the run | ~8 min | |
| `nextflow run . -profile podman,test_immenstadt` | end to end with real downloads; also the timing calibration | ~15 min | |
| `containers/smoke.sh <name>` | an image has `bash`, `ps` and the tool it exists for; for karttapullautin, that the v4 build really is AVX-512 | 30 s | ✅ (on build) |

All of the above pass. The Python tests need a few host packages:
`python -m venv .venv && .venv/bin/pip install -r tests/requirements.txt`.

A stub run is the cheapest test that can catch a *structural* bug, and worth understanding for that
reason. Every process that needs real inputs has a `stub:` block that fabricates correctly **named**
outputs — `PULLAUTA_GRID`'s reads its grid's CSV and emits one PNG per core tile — while
`PLAN_GRIDS`, `RENDER_INI` and `TILE_VIEWER` have none and run for real. So the plan is genuine, the
names everything joins on are genuine, and the assertion that every planned parent tile came out of
the pyramid is meaningful. It is also blind to every pixel: a stub run stays green on a pipeline that
renders garbage.

## Containers

Four images, one per process family, built and published by
[`.github/workflows/containers.yml`](.github/workflows/containers.yml) — or locally, identically, by
`containers/build.sh`. Both take their names, tags and build args from `build.sh --manifest`, so they
cannot drift apart.

**A new version is created only when an image's build inputs change.** Each image is tagged
`ctx-<hash>`, where the hash is the git tree hash of its `containers/<name>/` directory — precisely
the files that go into `docker build`. If that tag is already in the registry the build is skipped,
so a commit touching only the pipeline rebuilds nothing and a re-run costs four registry lookups.
Pull requests that touch `containers/**` build and smoke-test without publishing; `main` publishes
`ctx-<hash>`, the version tag and `latest`.

What a content hash cannot see is a floating dependency: `debian:stable-slim` moves, apt and pip
resolve to whatever is current that day. An image is therefore **not** rebuilt when its inputs change
upstream, only when this repository's description of them does — reproducible tags bought at the cost
of automatic freshness. Run the workflow manually with `force` to pick up a base-image or CVE update:

```
Actions ▸ containers ▸ Run workflow ▸ images: all ▸ force: true
```

One manual step, once: a package published by `GITHUB_TOKEN` starts out **private**, so
`podman pull` from a machine without credentials fails with `unauthorized` until each of the four is
set to public under *Packages ▸ Package settings ▸ Change visibility*. Nothing in a workflow can do
this — it needs a scope `GITHUB_TOKEN` does not have.

For a run whose toolchain has to be reconstructible later, pin the `ctx-` tags rather than `latest`:

```bash
nextflow run . --container_k2t ghcr.io/grst/mapant-nf/k2t:ctx-6033ed3b5add
```

## Known limitations

- **Input mixing several CRSs is rejected**, not handled. Grids are per-CRS in the code, so
  `--region_bbox` can process a multi-zone dataset one zone at a time. Full support needs a warp
  step upstream of `parent_tiles.csv`; it must not go inside `MAKE_TILES`, because k2t writes
  opaque white for nodata and two runs cannot be alpha-composited.
- **The halo is computed from the CSV's bounding boxes, karttapullautin's from the LAZ headers.**
  If they disagree the halo is subtly wrong with no error. Raising `params.pullauta_halo_m` above
  127 buys margin at the cost of fetching more; `test_local` renders real files, so it would show
  up there.
- **A corrupt laz that passes its checksum poisons its neighbours**, because they read it too.
  The recovery ladder will blacklist tiles one by one and ultimately fail the grid, which is
  reported but not worked around.
- The pipeline does not fetch the tiles CSV for you; producing it from a provider's catalogue is
  region-specific work. `testdata/laz_tiles.csv` is the Bavarian example.

---

## Container runtime in this devcontainer

The runtime is **rootless podman nested inside the devcontainer**, which is itself a
rootless podman container. The host's podman socket is deliberately not used —
`devcontainer-isolation` fails the container on every start if a runtime socket is
present, because a socket is a container escape.

Docker and Apptainer cannot work here at all. Rootless Docker needs a daemon and a tun
device; Apptainer needs `/dev/fuse` to mount a SIF unprivileged, or `CAP_SYS_ADMIN` for
its setuid mode. None of those exist inside this container, and none can be created
from within it.

`scripts/setup-container-runtime.sh` installs podman and Nextflow and writes
`~/.config/containers/{storage,containers}.conf`. It explains each setting inline; this
section covers what the limitations mean for writing pipelines.

### What works

Pulling and running images from any registry, building images with `podman build`,
Nextflow's `-resume` caching, and correct file ownership — task outputs land in `work/`
owned by `vscode`, not by root or nobody. Image storage lives in the `~/.cache` named
volume, so it survives a container rebuild, and uses the kernel's native rootless
overlay driver rather than copying every layer.

### What does not, and why

Everything below traces to one fact: **`CAP_SYS_ADMIN` is not in this container's
bounding set**, so no process inside can obtain it — not through `sudo`, and not
through a setuid-root binary, because file capabilities are masked by the bounding set.
Creating a *new* user namespace does grant full capabilities inside it, which is why
containers run at all; what is impossible is any operation needing privilege over the
*outer* namespace.

| Limitation | Consequence for a pipeline |
| --- | --- |
| **Only container UID 0 exists.** Giving a container a UID range means writing another process's `uid_map`, which requires `CAP_SYS_ADMIN` over the target namespace. Podman falls back to a single-UID mapping. | Images that switch to a non-root user cannot run — a `USER nonroot` directive makes an image unusable. Build steps that change UID fail too; see `containers/build.sh` for the `apt` workaround every Containerfile here uses. Image layers containing files owned by other UIDs still unpack, via `ignore_chown_errors`, but those files end up owned by root. |
| **No fresh procfs.** The kernel's `mount_too_revealing()` check rejects a new procfs while the visible one has *locked* submounts — the outer runtime masks a dozen paths under `/proc`. The outer `/proc` is bind-mounted in instead. | Containers see the outer process table. This forces sharing the PID namespace as well: a bind-mounted `/proc` plus a private PID namespace is incoherent and breaks anything that looks itself up in `/proc`, starting with `ps`. |
| **No UTS namespace.** Creating one is allowed, but `crun` then calls `sethostname` in it, which needs `CAP_SYS_ADMIN`. | Containers share this container's hostname. |
| **No `/dev/net/tun`**, and `CAP_MKNOD` is absent so it cannot be created. | Containers use the host network namespace; no per-container network isolation or port mapping. |
| **No cgroup v2 delegation** (`cgroup.subtree_control` is not writable). | `cpus` and `memory` directives are scheduling hints only — they control how many tasks run concurrently but nothing enforces them inside a task container. On a real node they are hard limits, so `PULLAUTA_GRID`'s memory is sized for that. |
| **`CAP_MKNOD` absent.** | A `RUN` step calling `mknod` always fails. |
| Image storage sits on a `nosuid` mount. | A setuid binary baked into an image will not elevate. |

Two further practical notes, neither specific to this devcontainer: every image needs
`bash` and `ps` (Nextflow runs its wrapper under bash and shells out to `ps` for task
metrics — `debian:stable-slim` fails on this), and images pinning UIDs above ~64k could
not work here even with a full mapping.

### Lifting the limitations

Every restriction above except cgroup delegation comes from how the *outer* container
is started. Adding to `runArgs` in `.devcontainer/devcontainer.json`:

```jsonc
"--cap-add=SYS_ADMIN",            // multi-UID mapping, private UTS namespace
"--security-opt=unmask=ALL",      // fresh procfs, so private PID namespace works
```

and rebuilding gives a normal rootless podman. Re-running the setup script afterwards
detects the change — it probes `newuidmap` rather than assuming — and configures the
full UID range automatically.

This is a real widening of the sandbox, which is why it is not the default. Both are
still confined by the outer rootless user namespace, so neither grants privilege on the
host; but `unmask=ALL` exposes kernel interfaces under `/proc` that the outer runtime
masks on purpose, and this devcontainer is built around an explicit isolation contract.
It is a judgement call, not an oversight — decide it deliberately.

If you would rather not touch the sandbox, [Wave](https://seqera.io/wave/) builds
container images remotely from conda specifications, which side-steps the local build
constraints entirely (`community.wave.seqera.io` is reachable from here).

## Licence and attribution

Pipeline code: see `LICENSE` if present. The map output inherits the licences of its inputs —
for Bavaria, LiDAR from [Geoportal Bayern](https://geodaten.bayern.de/opengeodata/) under
CC-BY-4.0, and OpenStreetMap data under ODbL. Attribute both.
