# Bodenmais / Grosser Arber

A ~230 km² region on the Bavarian-Czech border: 13.10–13.32 E, 49.05–49.18 N. Bodenmais, the Grosser
Arber and the ridge the border follows. Sized for this laptop, but a real region rather than a test
fixture — 189 tiles rendered to z18.

```bash
nextflow run . -profile podman -c test_bodenmais/nextflow.config -w work_bodenmais
```

Add `-resume` to continue an interrupted run; grid identity is on absolute lattice indices, so it
survives everything except a change to `grid_size`.

## Before starting

The plan costs nothing and needs no containers, so look at it first:

```bash
.venv/bin/python bin/plan_grids.py \
    --tiles-csv testdata/laz_tiles.csv --outdir /tmp/plan_bodenmais \
    --grid-size 5 --base-zoom 13 \
    --bbox '13.104606133632375,49.052154510644336,13.317145624028138,49.18400975069573'
```

| | |
| --- | --- |
| rendered (core) tiles | 189, in 15 grids of 2–25 |
| halo-only tiles | 46 |
| mapped area | 54.6 GiB of laz, mean 296 MB/tile |
| transferred | 122 GiB (1.84× — halo tiles are fetched once per grid bordering them) |
| peak disk per grid | 15.8 GiB of laz + ~6 GiB of karttapullautin temporaries |
| free disk needed | ~25 GB under `-w`, since `maxForks` is 1 |
| output | 29 z13 parents, z13–z18, roughly 1 GB |

Runtime, **estimated, not measured**: ~1.2 h of transfer at the ~30 MB/s this network has managed
against `geodaten.bayern.de`, plus ~8 CPU-h of rendering — interpolated from the two measured regions
in README.md by mean tile size, 296 MB sitting between Immenstadt's 166 MB and Kemptner Wald's
400 MB. At `pullauta_processes = 2` that is ~4 h of wall clock, and `maxForks = 1` puts the transfer
in series with it. **Expect 5–6 h.** Recalibrate from `results_bodenmais/pipeline_info/trace.txt`.

Nothing is retained between grids: each `PULLAUTA_GRID` task deletes its laz files and temporaries
before it ends, so the 122 GiB never accumulates.

## Why this region

It is the only one of the three test regions that sits at the **edge of the dataset**, which is where
two properties of the design are actually load-bearing rather than merely true:

- The tiles CSV stops at the national border, so the selection is ragged. Grids hold 2 to 25 core
  tiles instead of 25, and the border-side halo ring does not exist, so those tiles render from fewer
  points on that side. Neither is special-cased — it is what a full Bavaria run meets at its own
  perimeter, in miniature.
- The OSM extract bboxes cross into Czechia, where `bayern.osm.pbf` has nothing. Grids there get
  shapes for the Bavarian part only, and the `remainder: true` join is what keeps a grid whose extract
  yields no drawable features from vanishing from the run.

A third thing comes free with the longitude: at 13 E this is EPSG:25832 data 1.3° past zone 32's
nominal eastern edge (Bavaria uses one zone statewide), where grid north and true north differ by
~3.2°. The lon/lat region box is therefore a visibly rotated quadrilateral on the 1 km lattice — the
case `transform_bounds(densify_pts=21)` exists for. Eastings run 799–815 km.

## Deviations from README.md's laptop column

`grid_size` 5 rather than 2–3, and `max_zoom` 18 rather than 16. These tiles average 296 MB against
Bavaria's 208 MB, so download amplification is the expensive axis here, not disk: 5 transfers 122 GiB
where 3 transfers 163 GiB, in exchange for 15.8 GiB of peak laz per grid instead of 10.5 GiB, which
this machine has. z18 matches karttapullautin's 0.42 m/px output and costs about 1 GB over an area
this small.
