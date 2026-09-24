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
 * `PULLAUTA_GRID` based on the grids defined earlier, downloads the tiles, runs `karttapullautin` on the LiDAR and the grid's OSM shapes, and cleans up the LAZ files. Downloading and processing is done within one process to save disk space. The process produces one vector bundle per tile -- the map as one GeoJSON per layer, in WGS84, OSM included -- and collects
   processing errors in a TSV file.
 * `MAKE_VECTOR_TILES` cuts one base-zoom parent's subtree with `tippecanoe`, handing it the
   bundles' files as they are.
 * `VECTOR_VIEWER` generates the MapLibre style, the sprite and a preview page.

## Output data

A `tiles_vector/{z}/{x}/{y}.pbf` pyramid of [Mapbox vector
tiles](https://github.com/mapbox/vector-tile-spec), plus the `style.json`, `sprite.png`,
`metadata.json` and `index.html` that draw it. The tiles carry classes rather than colours -- the
appearance lives entirely in the style -- and because they are vectors they stay sharp when zoomed
past the deepest level that was cut, so the pyramid stops where the LiDAR stops resolving rather
than where the screen does. They also carry the data needed to export an area as an editable OCAD
file.

All of it comes from `karttapullautin`'s own vector output (`vectorvege=1`, `geojson_wgs84=1`,
from the fork built on @malpou's work): contours generalised and broken around the knoll symbols,
cliff dashes chained into cliff lines, vegetation traced into polygons in Rust, and the OSM shapes
matched to their ISOM codes by the rules file (`--vectorconf`). Every feature carries `layer`
(karttapullautin's class) and `isom` (its symbol). The pipeline does not transform any of it.

Measured on `-profile test_immenstadt` (alpine terrain, 8 core km²): over zoom 13-16 the pyramid is
95 tiles, 2.9 MB as published and 1.6 MiB gzipped; the largest tile is 250 KB. Cutting a parent
takes under two seconds.

### What each zoom shows

The pyramid is cut one base-zoom parent at a time, so nothing about a tile may depend on what else
happens to be in it. tippecanoe's size-driven thinning breaks that rule, so it is switched off, and
a feature filter on each feature's own `isom` decides the zoom it appears at:

| from the base zoom | one level above the deepest | the deepest zoom only |
| --- | --- | --- |
| vegetation, open land, index contours, roads, tracks, railways, streams, lakes, marsh, settlement | plain contours, undergrowth, cliffs, buildings, paved areas, small paths | form lines, knolls, fences, power lines |

Two shades of green share their boundary vertex for vertex, because karttapullautin traces them from
one grid; `--detect-shared-borders` keeps tippecanoe from simplifying that boundary twice and
opening a sliver of white paper between them.

### How it differs from a karttapullautin render

The terrain is drawn with `karttapullautin`'s own palette and line widths, taken from its source.
The OSM shapes are not: it strokes them as flat coloured lines of a single width, so they are drawn
here as the ISOM 2017-2 symbols they were matched to instead -- a road with its black casing, a
vehicle track and a footpath with their own dashes, undergrowth and marsh with their stripes (from
the sprite next to the style). Widths and dash lengths are the symbol set's own.

Two symbols are approximated because a style cannot draw a symbol along a line: a fence loses its
cross ticks and a power line its pylon bars, both becoming plain lines.

Undergrowth is vectorised from karttapullautin's own 18 m undergrowth cells (`greendetectsize * 6`)
and is only ever ISOM 407; dense undergrowth (409) is not distinguished. Water and blocks
(`detectbuildings=1`) are drawn on karttapullautin's raster map but have no vector output yet, so
they are not in the tiles.

## Transparent use of AI

The pipeline was implemented using Claude Opus 5. The prompts are documented in [prompts.md](prompt.md). 
This README is hand-crafted by a human.

## Contact

If you have feedback or questions regarding this pipeline, feel free to open [an issue](https://github.com/grst/mapant-nf/issues) or contact me via one of the options listed in my [GitHub profile](https://github.com/grst).