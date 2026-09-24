# mapant-nf

[![tests](https://github.com/grst/mapant-nf/actions/workflows/ci.yml/badge.svg)](https://github.com/grst/mapant-nf/actions/workflows/ci.yml)
[![containers](https://github.com/grst/mapant-nf/actions/workflows/containers.yml/badge.svg)](https://github.com/grst/mapant-nf/actions/workflows/containers.yml)

`mapant-nf` is a [Nextflow](https://www.nextflow.io/) pipeline that automatically generates [orienteering](https://en.wikipedia.org/wiki/Orienteering) maps from LIDAR tiles and open street map annotations.
The heavy lifting is done by [karttapullautin](https://github.com/karttapullautin/karttapullautin), and a web-mercator pyramid of [Mapbox vector tiles](https://github.com/mapbox/vector-tile-spec) is cut from its vectors with [tippecanoe](https://github.com/felt/tippecanoe).
This workflow pipes these tools together in a scalable way, ready to generate a ["mapant"](https://mapant.net/) map of a whole country. 

Nextflow is a workflow manager that provides implicit parallelization and abstracts the compute environment. 
All it takes to adapt this workflow to run on a single machine, a HPC, or a cloud batch system is changing a few lines of configuration.
All dependencies are containerized.

![mapant metro map](docs/images/metro_map.svg)

## Quickstart

### Prerequisites

* [nextflow](https://nextflow.io/)
* a [container runtime](https://docs.seqera.io/nextflow/container), typically [docker](https://www.docker.com/), [podman](https://podman.io/), or [apptainer](https://apptainer.org/). 
* an [execution environment](https://docs.seqera.io/nextflow/executor), e.g. a local machine, a HPC, or a cloud batch system. 

### Test run

Run the pipeline on a predefined region around Immenstadt i. Allgäu. 
This helps checking if the everything is set up correctly on your system.  

```bash
# ~15 minutes; downloads ~5.4 GB of LiDAR from the source server
nextflow run grst/mapant-nf -profile podman,test_immenstadt
```

### Real run

The minimal command to execute the pipeline on custom data is the following. 
Adjust paths and container profile as needed.

```bash
nextflow run grst/mapant-nf \
  --tiles_csv laz_tiles.csv \
  --osm_pbf bayern-latest.osm.pbf \
  --outdir output \
  -profile docker \
  -resume
```

where `tiles_csv` is an input samplesheet with download links to every tile (see an [example](./assets/laz_tiles_immenstadt.csv)) and `osm_pbf` is a file containing open street map data (you can download e.g. from [geofabrik.de](https://download.geofabrik.de/)).

In a real production setting, you most likely want to specify additional parameters and provide them as yaml file via
`-params-file`. Additionally, it can make sense to optimize resource requirements for the individual processes and 
provide them as a nextflow config file (`-c`). For reference, the full configuration used to generate [Mapant Bayern](https://mapant.orienteering-allgaeu.de) is available [here](https://github.com/grst/mapant-bayern/tree/main/processing_pipeline).

All available pipeline parameters are documented in the [nextflow schema](./nextflow_schema.json) of this pipeline. 

## Performance and cost

This pipeline was used to generate [Mapant Bavaria](https://github.com/grst/mapant-bayern). 
Bavaria has an area of ca. 70,541 km². Downloading and processing the corresponding 71979 LIDAR tiles (ca. 15 TB) 
on a `c8id.32xlarge` AWS EC2 instance with 256GB or memory and 128 vCPU this completed in 27h wall time, 
consuming 5042 CPU hours. With on-demand pricing, this cost of the run was a little less than 200 USD. 

This corresponds to 0.07 CPUh or 0.0028 USD per tile.

## Processes

 * `PLAN_GRIDS` processes the samplesheets and separates it into grids that are processed by a single `karttapullautin` run. 
   A grid consists of "core tiles" and one ring of halo to avoid edge artifacts. 
 * `RENDER_INI` generates a `.ini` file for `karttapullautin` based on the pipeline parameters. 
 * `OSM_EXTRACT` uses `osmium` to extract OSM shapes that overlap with each grid.
   `OSM_TO_SHAPES` converts them to a `*.shp.zip` per grid, reprojected into the grid's own CRS.
 * `PULLAUTA_GRID` based on the grids defined earlier, downloads the tiles, runs `karttapullautin`, and cleans up the LAZ files. Downloading and processing is done within one process to save disk space. The process produces one vector bundle per tile -- its GeoJSON and its classified rasters -- and collects
   processing errors in a TSV file. It is given no shapefiles: the OSM shapes never reach the renderer.
 * `MAKE_VECTOR_TILES` cuts one base-zoom parent's subtree with `tippecanoe`. This is where the two
   halves of the map meet: `karttapullautin`'s vectors for the terrain, and the OSM shapes, which
   `bin/osm_shapes.py` matches to their ISOM codes here from the same rules file
   (`--vectorconf`) `karttapullautin` used to be given.
 * `VECTOR_VIEWER` generates the MapLibre style, the sprite and a preview page.

## Output data

A `tiles_vector/{z}/{x}/{y}.pbf` pyramid of [Mapbox vector
tiles](https://github.com/mapbox/vector-tile-spec), plus the `style.json`, `sprite.png`,
`metadata.json` and `index.html` that draw it. The tiles carry classes rather than colours -- the
appearance lives entirely in the style -- and because they are vectors they stay sharp when zoomed
past the deepest level that was cut, so the pyramid stops where the LiDAR stops resolving rather
than where the screen does. They also carry the data needed to export an area as an editable OCAD
file.

The terrain comes from `karttapullautin`'s own vectors (`output_geojson=1`, `vege_bitmode=1`) and
not from a rendered image, so nothing is traced back out of pixels that was a vector to begin with.

Measured on `-profile test_immenstadt`, which is alpine terrain with ~170k cliff hatch segments per
km² and therefore the worst case for vectors: over zoom 13-16 the pyramid is 95 tiles and 7.3 MiB
gzipped (15.1 MiB as published, uncompressed; the largest single tile is 1.0 MiB, almost all of it
cliff hatching). The raster pyramid this replaced was 340 tiles and 6.3 MiB of WebP over the same
zooms -- and needed z17 and z18 on top to stay sharp, which this does not.

### What each zoom shows

The pyramid is cut one base-zoom parent at a time, so nothing about a tile may depend on what else
happens to be in it. tippecanoe's size-driven thinning breaks that rule -- it drops whatever does
not fit the 500 KB budget, per tile, which puts a lake on an overview tile in one parent and leaves
it off in the next -- so it is switched off, and each feature instead declares the zoom it appears
at:

| from the base zoom | one level above the deepest | the deepest zoom only |
| --- | --- | --- |
| vegetation, water, index contours, roads, tracks, railways, streams, lakes, open land, marsh, settlement | plain contours and depressions, undergrowth, blocks, buildings, paved areas, small paths | form lines, knolls, slope lines, single-contour depressions, fences, power lines |

The cliff hatching is a texture rather than a set of features -- some 170k two-point ticks per
square kilometre here, more than everything else put together -- so one tick in sixteen
survives each zoom out, sampled by a hash of where the tick is. That makes the sample identical
whichever parent cuts the tile, and nested, so a tick drawn on an overview tile is drawn on every
deeper one.

The area layers are traced from `karttapullautin`'s classified rasters, and each zoom is traced
from its own mode-downsampled grid, at about one raster pixel per screen pixel. Generalising the
raster rather than simplifying the polygons is what keeps two shades of green sharing one boundary:
simplifying each polygon on its own moves the boundary twice, once per side, and leaves a sliver of
white paper between them.

### How it differs from a karttapullautin render

The terrain is drawn with `karttapullautin`'s own palette and line widths, taken from its source.
The OSM shapes are not: it strokes them as flat coloured lines of a single width, so they are drawn
here as the ISOM 2017-2 symbols they were matched to instead -- a road with its black casing, a
vehicle track and a footpath with their own dashes, undergrowth and marsh with their stripes (from
the sprite next to the style). Widths and dash lengths are the symbol set's own.

Which shapes are on the map, and what code each carries, is decided by `bin/osm_shapes.py` rather
than by `karttapullautin` -- from the same rules file, and deliberately with the same semantics
down to the details that look like accidents (a missing tag comparing equal to the empty string; a
rule whose code has no symbol leaving the shape for the next rule; an area code needing a closed
way). It was validated by matching the Immenstadt shapes both ways and comparing feature by
feature: same features, same codes, positions within 4 cm, the difference being that
`karttapullautin` rounds its GeoJSON coordinates to a centimetre.

Two symbols are approximated because a style cannot draw a symbol along a line: a fence loses its
cross ticks and a power line its pylon bars, both becoming plain lines.

The undergrowth areas are also more generous than the stripes the map draws. `karttapullautin`
samples undergrowth on an 18 m grid (`greendetectsize * 6`) and marks each sample in
`undergrowth_bit` with a disc about 16 m across the ground -- a little wider than the cell it stands
for -- so an area here comes out two to three times the area the stripes cover. It is in the right
place, which it was not until `undergrowth_bit` stopped being cropped as though it were on the
metre grid of the other three rasters: it is drawn at render resolution, 254/600 m per pixel, and
the fix is in the karttapullautin branch this pipeline needs.

The `blocks` layer is empty unless the ini sets `detectbuildings=1`. That is what makes
`karttapullautin` run its block detection at all, and `assets/pullauta.ini` leaves it off.

## Transparent use of AI

The pipeline was implemented using Claude Opus 5. The prompts are documented in [prompts.md](prompt.md). 
This README is hand-crafted by a human.

## Contact

If you have feedback or questions regarding this pipeline, feel free to open [an issue](https://github.com/grst/mapant-nf/issues) or contact me via one of the options listed in my [GitHub profile](https://github.com/grst).