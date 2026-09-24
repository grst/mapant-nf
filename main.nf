#!/usr/bin/env nextflow
/*
 * mapant -- generate a web-mercator vector map pyramid from a list of LiDAR tiles.
 *
 * Give it a CSV of laz tiles (url, checksum, bbox, CRS), an OSM extract and a karttapullautin
 * configuration, and it produces a z/x/y directory of Mapbox vector tiles, with the style that
 * draws them. Nothing here is specific to Bavaria; the input contract is assets/schema_tiles.json.
 *
 * karttapullautin does all the cartography: given a grid's LiDAR and its OSM shapes, it writes each
 * tile's map as per-layer GeoJSON in WGS84, already in its published form. MAKE_VECTOR_TILES hands
 * those files to tippecanoe untouched and cuts one parent's subtree.
 *
 * See README.md for the design, and each run's published plan_summary.txt for its own numbers.
 */

include { validateParameters ; paramsSummaryLog ; samplesheetToList } from 'plugin/nf-schema'

include { PLAN_GRIDS    } from './modules/local/plan_grids'
include { RENDER_INI    } from './modules/local/render_ini'
include { OSM_EXTRACT   } from './modules/local/osm_extract'
include { OSM_TO_SHAPES } from './modules/local/osm_to_shapes'
include { PULLAUTA_GRID } from './modules/local/pullauta_grid'
include { MAKE_VECTOR_TILES } from './modules/local/make_vector_tiles'
include { VECTOR_VIEWER  } from './modules/local/vector_viewer'

workflow {

    main:
    validateParameters()
    log.info(paramsSummaryLog(workflow))

    // The samplesheet is this pipeline's public contract, so it is checked up front rather than
    // being discovered to be wrong by a script three processes in. All 72k rows of Bavaria cost
    // about 18 s.
    def n_tiles = samplesheetToList(
        params.tiles_csv, "${projectDir}/assets/schema_tiles.json"
    ).size()
    log.info("Validated ${n_tiles} tile(s) against assets/schema_tiles.json")

    // ---------------------------------------------------------------------
    // Plan
    // ---------------------------------------------------------------------
    PLAN_GRIDS(channel.fromPath(params.tiles_csv, checkIfExists: true))

    // .first() makes this a value channel. Without it, a one-item queue channel would pair with
    // exactly one grid and silently starve every other grid of its ini -- the classic Nextflow trap.
    ch_ini = RENDER_INI(
        channel.fromPath(params.pullauta_ini, checkIfExists: true),
        params.pullauta_processes
    ).ini.first()

    ch_grid_csv = PLAN_GRIDS.out.grid_csvs
        .flatten()
        .map { csv -> tuple(csv.baseName, csv) }

    // Only the CRS is needed per grid; everything else in grids.csv is for the reader.
    ch_grid_crs = PLAN_GRIDS.out.grid_index
        .splitCsv(header: true)
        .map { row -> tuple(row.grid_id, row.crs) }

    // ---------------------------------------------------------------------
    // OSM shapes (optional: with no .pbf the map is contours and vegetation only)
    // ---------------------------------------------------------------------
    ch_chunks = params.osm_pbf
        ? PLAN_GRIDS.out.osm_chunks.flatten().map { chunk -> tuple(chunk.baseName, chunk) }
        : channel.empty()

    OSM_EXTRACT(
        ch_chunks,
        channel
            .fromPath(params.osm_pbf ?: "${projectDir}/assets/NONE", checkIfExists: true)
            .first()
    )

    OSM_TO_SHAPES(
        OSM_EXTRACT.out.pbf
            .flatten()
            .map { pbf -> tuple(pbf.baseName, pbf) }
            .join(ch_grid_crs)
    )

    // A grid with no shapes -- no pbf at all, or an extract with nothing drawable in it -- carries
    // the sentinel OSM_TO_SHAPES wrote instead of an archive, and remainder: true is what keeps it
    // in the run. Without it those grids would silently vanish before they were rendered.
    ch_grid_shapes = ch_grid_csv
        .join(OSM_TO_SHAPES.out.shapes, remainder: true)
        .map { grid_id, csv, shapes ->
            tuple(grid_id, csv, shapes ?: file("${projectDir}/assets/NONE"))
        }

    // ---------------------------------------------------------------------
    // Render
    // ---------------------------------------------------------------------
    PULLAUTA_GRID(
        ch_grid_shapes,
        ch_ini,
        file(params.vectorconf ?: "${projectDir}/assets/NONE")
    )

    // One item per rendered tile, keyed by its name for the join to its parent.
    ch_tile_vec = PULLAUTA_GRID.out.vectors
        .transpose()
        .map { _grid_id, bundle -> tuple(bundle.name.replaceAll(/_vec$/, ''), bundle) }

    // ---------------------------------------------------------------------
    // Tile
    // ---------------------------------------------------------------------
    // groupKey carries each parent's expected core-tile count, so groupTuple emits a parent as soon
    // as its last contributing tile arrives: tiling overlaps rendering instead of waiting on a
    // barrier over all ~72k tiles.
    //
    // `remainder: true` is not optional. When the key carries a size, groupTuple *discards* any group
    // that never reaches it -- so one tile that failed to render would silently delete every
    // web-mercator tile overlapping it, and the run would still report success. That is the exact
    // failure this pipeline exists to survive; tests/test_failure_injection.sh covers it.
    ch_parent_vec = PLAN_GRIDS.out.parent_index
        .splitCsv(header: true)
        .map { row ->
            tuple(
                row.tile,
                [z: row.z as int, x: row.x as int, y: row.y as int, crs: row.crs],
                row.n_core as int
            )
        }
        .combine(ch_tile_vec, by: 0)
        .map { _tile, parent, n_core, bundle -> tuple(groupKey(parent, n_core), bundle) }
        .groupTuple(remainder: true)
        .map { key, bundles -> tuple(key.getGroupTarget(), bundles) }

    MAKE_VECTOR_TILES(ch_parent_vec)

    // A vector tile carries classes, not colours, so the style is not a nicety the way a raster
    // viewer is: without it the pyramid cannot be drawn at all.
    ch_vector_style = VECTOR_VIEWER(PLAN_GRIDS.out.parent_index, ch_ini).style
        .mix(
            VECTOR_VIEWER.out.metadata,
            VECTOR_VIEWER.out.viewer,
            VECTOR_VIEWER.out.sprite.flatten()
        )

    // collectFile rather than a concatenation process: no container, no task, and the header is
    // kept exactly once.
    ch_render_failures = PULLAUTA_GRID.out.failures
        .collectFile(name: 'pullauta_failures.tsv', keepHeader: true, skip: 1, sort: true)
    ch_download_failures = PULLAUTA_GRID.out.download_failures
        .collectFile(name: 'download_failures.tsv', keepHeader: true, skip: 1, sort: true)

    // PULLAUTA_GRID is set to 'ignore' once its retries are spent, so one unrenderable grid cannot end
    // a run that has already spent days and terabytes on the others. The price is a green run with an
    // area missing from the map, so the gap has to be impossible to overlook. Anything reported here
    // is a whole grid that produced nothing -- distinct from the individual tiles in
    // qc/pullauta_failures.tsv, which the pipeline worked around.
    workflow.onComplete = {
        def ignored = workflow.stats.ignoredCount ?: 0
        def report = file("${params.outdir}/qc/failed_grids.txt")
        report.parent.mkdirs()
        if (ignored > 0) {
            report.text = """\
                |${ignored} task(s) failed permanently and were ignored so the run could finish.
                |Each is a grid that produced no tiles at all: those areas are missing from the map.
                |
                |Which, and why:
                |  grep -E 'FAILED|ABORTED' ${params.outdir}/pipeline_info/trace.txt
                |  # then read .command.log in the work directory of the hash it shows
                |
                |Individual tiles karttapullautin could not render, which the pipeline worked around,
                |are listed separately in ${params.outdir}/qc/pullauta_failures.tsv.
                |""".stripMargin()
            log.warn("${ignored} task(s) ignored after exhausting their retries -- the map has gaps. See ${report}")
        }
        else {
            report.text = 'No tasks failed permanently.\n'
        }
    }

    publish:
    // Each MAKE_VECTOR_TILES task owns a disjoint subtree of the pyramid, so publishing them all
    // into one directory is a plain union with no possibility of a collision.
    //
    // flatten() is for the end-of-run "Outputs:" summary, not for the publishing: Nextflow caps that
    // listing at ten *channel items* but prints each item whole, and one task's item is the whole
    // subtree under its parent. Ten of those is megabytes of tile names on the console. One path
    // per item makes the cap bite. Same files, same paths, same union.
    vector_tiles = MAKE_VECTOR_TILES.out.tiles.flatten().mix(ch_vector_style)
    qc = ch_render_failures.mix(ch_download_failures, PULLAUTA_GRID.out.log)
    plan = PLAN_GRIDS.out.summary.mix(
        PLAN_GRIDS.out.grid_index,
        PLAN_GRIDS.out.parent_index,
        ch_ini
    )
}

output {
    // path '.' keeps each file's task-relative path, so 'tiles_vector/13/4328/2862.pbf' lands at
    // <outputDir>/tiles_vector/13/4328/2862.pbf and the pyramid assembles itself from many tasks.
    vector_tiles {
        path '.'
        mode params.publish_mode
    }

    qc {
        path 'qc'
    }

    plan {
        path 'pipeline_info'
    }
}
