# mapant-nf

[![tests](https://github.com/grst/mapant-nf/actions/workflows/ci.yml/badge.svg)](https://github.com/grst/mapant-nf/actions/workflows/ci.yml)
[![containers](https://github.com/grst/mapant-nf/actions/workflows/containers.yml/badge.svg)](https://github.com/grst/mapant-nf/actions/workflows/containers.yml)

`mapant-nf` is a [Nextflow](https://www.nextflow.io/) pipeline that automatically generates [orienteering](https://en.wikipedia.org/wiki/Orienteering) maps from LIDAR tiles and open street map annotations.
The heavy lifting is done by [karttapullautin](https://github.com/karttapullautin/karttapullautin), which writes each tile's map as vector data; [tippecanoe](https://github.com/felt/tippecanoe) cuts it into one [PMTiles](https://docs.protomaps.com/pmtiles/) archive of vector tiles in the schema of the [isom-maplibre](https://github.com/MetsaApp/isom-maplibre) style. 
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

where `tiles_csv` is an input samplesheet with download links to every tile (see an [example](./assets/laz_tiles_immenstadt.csv) and the [schema](./assets/schema_tiles.json)) and `osm_pbf` is a file containing open street map data (you can download e.g. from [geofabrik.de](https://download.geofabrik.de/)).

The map's look comes from a karttapullautin ini (`--pullauta_ini`) and an OSM rules file (`--vectorconf`). Where a region needs different settings for different tiles -- two generations of LiDAR, say -- the samplesheet can name an ini per tile in a `pullauta_ini` column (relative to the launch directory); it takes precedence over `--pullauta_ini`, which then only covers tiles that name none. Each configuration is rendered in grids of its own.

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

 * `PLAN_GRIDS` processes the samplesheet and separates it into grids that are processed by a single `karttapullautin` run.
   A grid consists of "core tiles" of one configuration and one ring of halo to avoid edge artifacts.
 * `RENDER_INI` generates the `.ini` file `karttapullautin` runs with, once per configuration.
 * `OSM_EXTRACT` uses `osmium` to extract OSM shapes that overlap with each grid.
   `OSM_TO_SHAPES` converts them to the `*.shp.zip` files `karttapullautin` reads, with a column for every OSM key the rules file tests.
 * `PULLAUTA_GRID` based on the grids defined earlier, downloads the tiles, runs `karttapullautin`, and cleans up the LAZ files. Downloading and processing is done within one process to save disk space. The process writes each tile's map as GeoJSON (contours, cliffs, vegetation, the matched OSM shapes) and collects
   processing errors in a TSV file.
 * `MAKE_VECTOR_TILES` sorts the GeoJSON into the style's tables and cuts one archive per web-mercator parent tile with `tippecanoe`; `MERGE_PMTILES` joins them into one.
 * `MAKE_VIEWER` writes a MapLibre style and a preview page for the archive.

## Output data

`map/mapant.pmtiles`, one PMTiles archive of 512 px vector tiles, with its bounds, centre and zoom range in the header, and next to it a style and an `index.html` that shows it (serve the directory with anything that answers range requests). The archive follows [isom-maplibre](https://github.com/MetsaApp/isom-maplibre)'s schema:

| layer | what | `isom_code` |
| --- | --- | --- |
| `contours` | contours, index contours, form lines | 101.000, 102.000, 103.000 |
| `knolls_points` | dot knolls, small depressions | 109.000, 111.000 |
| `cliffs` | cliffs, as karttapullautin's dashes | 201.000, 202.000 |
| `vegetation_areas` | open land, green, undergrowth, field edges | 401.000–410.000, 415.000 |
| `water` | lakes and their banks, streams, marsh | 301.000, 305.000, 308.000 |
| `paths` | roads, tracks, paths | 502.000–506.000 |
| `manmade` | railways, power lines, fences, buildings, settlements, paved areas | 501.000, 509.000, 510.000, 518.000, 520.000, 521.000 |
| `coverage` | the footprint of every rendered LiDAR tile | |

`isom_code` is ISOM 2017-2; the OSM shapes, which the rules file numbers in ISOM 2000, are translated with OpenOrienteering Mapper's crosswalk (`assets/isom2000-isom2017-2.crt`). Every feature also keeps karttapullautin's `layer` (its class) and `isom` (its own code). The archive spans `base_zoom - 1` to `max_zoom`: the shallowest level is an overview without contours, and what each deeper zoom shows is decided per feature in `bin/make_vector_tiles.py`.

## Transparent use of AI

The pipeline was implemented using Claude Opus 5. The prompts are documented in [prompts.md](prompt.md). 
This README is hand-crafted by a human.

## Contact

If you have feedback or questions regarding this pipeline, feel free to open [an issue](https://github.com/grst/mapant-nf/issues) or contact me via one of the options listed in my [GitHub profile](https://github.com/grst).