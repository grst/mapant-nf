#!/usr/bin/env nextflow
/*
 * mapant -- generate a web-mercator map pyramid from a list of LiDAR tiles.
 *
 * Give it a CSV of laz tiles (url, checksum, bbox, CRS), an OSM extract and a karttapullautin
 * configuration, and it produces a z/x/y directory of PNG tiles. Nothing in here is specific to
 * Bavaria; the input contract is assets/schema_tiles.json.
 *
 * See README.md for the design, and each run's published plan_summary.txt for its own numbers.
 */

include { validateParameters ; paramsSummaryLog ; samplesheetToList } from 'plugin/nf-schema'

include { PLAN_GRIDS     } from './modules/local/plan_grids'
include { RENDER_INI     } from './modules/local/render_ini'
include { OSM_EXTRACT    } from './modules/local/osm_extract'
include { OSM_TO_SHAPES  } from './modules/local/osm_to_shapes'
include { PULLAUTA_GRID  } from './modules/local/pullauta_grid'
include { MAKE_TILES     } from './modules/local/make_tiles'
include { TILE_OVERVIEWS } from './modules/local/tile_overviews'
include { TILE_VIEWER    } from './modules/local/tile_viewer'

workflow {

    main:
    validateParameters()
    log.info(paramsSummaryLog(workflow))

    // The samplesheet is this pipeline's public contract, so it is validated up front rather than
    // being discovered to be wrong by a script three processes in. Validating all 72k rows of
    // Bavaria costs about 18 s; if that ever becomes a problem, params.validate_tiles_in_python
    // moves the same check into PLAN_GRIDS, against the same schema file.
    def n_tiles = params.validate_tiles
        ? samplesheetToList(params.tiles_csv, "${projectDir}/assets/schema_tiles.json").size()
        : 0
    log.info(params.validate_tiles
        ? "Validated ${n_tiles} tile(s) against assets/schema_tiles.json"
        : 'Samplesheet validation skipped (params.validate_tiles = false)')

    // ---------------------------------------------------------------------
    // Plan
    // ---------------------------------------------------------------------
    PLAN_GRIDS(
        channel.fromPath(params.tiles_csv, checkIfExists: true),
        file("${projectDir}/assets/schema_tiles.json", checkIfExists: true)
    )

    // .first() makes this a value channel. Without it, a one-item queue channel would pair with
    // exactly one grid and silently starve every other grid of its ini -- the classic Nextflow trap.
    ch_ini = RENDER_INI(
        channel.fromPath(params.pullauta_ini, checkIfExists: true),
        params.pullauta_processes
    ).ini.first()

    ch_grid_csv = PLAN_GRIDS.out.grid_csvs
        .flatten()
        .map { csv -> tuple(csv.baseName, csv) }

    ch_grid_meta = PLAN_GRIDS.out.grid_index
        .splitCsv(header: true)
        .map { row -> tuple(row.grid_id, row) }

    // ---------------------------------------------------------------------
    // OSM vectors (optional: with no .pbf, karttapullautin draws contours and vegetation only)
    // ---------------------------------------------------------------------
    ch_chunks = params.osm_pbf
        ? PLAN_GRIDS.out.osm_chunks.flatten().map { chunk -> tuple(chunk.baseName, chunk) }
        : channel.empty()

    OSM_EXTRACT(
        ch_chunks,
        channel
            .fromPath(params.osm_pbf ?: "${projectDir}/assets/NO_SHAPES", checkIfExists: true)
            .first()
    )

    OSM_TO_SHAPES(
        OSM_EXTRACT.out.pbf
            .flatten()
            .map { pbf -> tuple(pbf.baseName, pbf) }
            .join(ch_grid_meta)
    )

    // remainder: true so that grids with no shapes -- either because there is no pbf at all, or
    // because their extract held no drawable features -- still reach PULLAUTA_GRID, carrying the
    // sentinel instead of an archive. Without it those grids would silently vanish from the run.
    ch_pullauta_in = ch_grid_csv
        .join(OSM_TO_SHAPES.out.shapes, remainder: true)
        .join(ch_grid_meta)
        .map { grid_id, csv, shapes, meta ->
            tuple(grid_id, csv, shapes ?: file("${projectDir}/assets/NO_SHAPES"), meta)
        }

    // ---------------------------------------------------------------------
    // Render
    // ---------------------------------------------------------------------
    PULLAUTA_GRID(
        ch_pullauta_in,
        ch_ini,
        file(params.vectorconf ?: "${projectDir}/assets/NO_VECTORCONF"),
        file(params.laz_local_dir ?: "${projectDir}/assets/NO_LOCAL_LAZ")
    )

    // One item per rendered tile. transpose() expands the per-grid lists of PNGs and PGWs in step;
    // they stay aligned because both globs are sorted and share their stems.
    ch_tile = PULLAUTA_GRID.out.rendered
        .transpose()
        .map { _grid_id, _meta, png, pgw ->
            tuple(png.simpleName.replaceAll(/_depr$/, ''), png, pgw)
        }

    // ---------------------------------------------------------------------
    // Tile
    // ---------------------------------------------------------------------
    // groupKey carries each parent's expected core-tile count, so groupTuple emits a parent as soon
    // as its last contributing tile arrives: tiling overlaps rendering instead of waiting on a
    // barrier over all ~72k tiles.
    //
    // `remainder: true` is not optional. When the key carries a size, groupTuple *discards* any group
    // that never reaches it -- so a single tile that failed to render would silently delete every
    // web-mercator tile overlapping it, and the run would still report success. That is the exact
    // failure this pipeline is supposed to survive; tests/test_failure_injection.sh caught it.
    ch_parent = PLAN_GRIDS.out.parent_index
        .splitCsv(header: true)
        .map { row ->
            tuple(
                row.tile,
                [z: row.z as int, x: row.x as int, y: row.y as int, crs: row.crs],
                row.n_core as int
            )
        }
        .combine(ch_tile, by: 0)
        .map { _tile, parent, n_core, png, pgw -> tuple(groupKey(parent, n_core), png, pgw) }
        .groupTuple(remainder: true)
        .map { key, pngs, pgws -> tuple(key.getGroupTarget(), pngs, pgws) }

    MAKE_TILES(ch_parent)

    // A single task, because a parent tile needs all four of its children and those come from four
    // different MAKE_TILES tasks. Guarded so a run that produced nothing reports that rather than
    // failing inside the reducer.
    TILE_OVERVIEWS(
        MAKE_TILES.out.base
            .collect(sort: true)
            .filter { base_tiles -> base_tiles.size() > 0 }
    )

    TILE_VIEWER(PLAN_GRIDS.out.parent_index)

    // collectFile rather than a concatenation process: no container, no task, and the header is
    // kept exactly once.
    ch_render_failures = PULLAUTA_GRID.out.failures
        .collectFile(name: 'pullauta_failures.tsv', keepHeader: true, skip: 1, sort: true)
    ch_download_failures = PULLAUTA_GRID.out.download_failures
        .collectFile(name: 'download_failures.tsv', keepHeader: true, skip: 1, sort: true)

    // PULLAUTA_GRID is set to 'ignore' once its retries are spent, so that one unrenderable grid
    // cannot end a run that has already spent days and terabytes on the others. The price of that is
    // a run which finishes green with an area missing from the map, so the gap has to be impossible
    // to overlook. Anything reported here is a whole grid that produced nothing -- distinct from the
    // individual tiles in qc/pullauta_failures.tsv, which the pipeline worked around.
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
    // Each MAKE_TILES task owns a disjoint subtree of the pyramid, so publishing them all into one
    // directory is a plain union with no possibility of a collision.
    tiles = MAKE_TILES.out.tiles.mix(TILE_OVERVIEWS.out.tiles, TILE_VIEWER.out.viewer)
    qc = ch_render_failures.mix(ch_download_failures, PULLAUTA_GRID.out.log)
    plan = PLAN_GRIDS.out.summary.mix(
        PLAN_GRIDS.out.grid_index,
        PLAN_GRIDS.out.parent_index,
        ch_ini
    )
}

output {
    // path '.' keeps each file's task-relative path, so 'tiles/12/2145/1423.png' lands at
    // <outputDir>/tiles/12/2145/1423.png and the pyramid assembles itself from many tasks.
    tiles {
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
