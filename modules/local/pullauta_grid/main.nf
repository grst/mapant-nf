// Download a grid's laz files, verify them, render the core tiles, and leave nothing behind but
// the PNGs.
//
// This is deliberately one process. The input for Bavaria is 15+ TB, so the laz files cannot be
// staged as Nextflow inputs and cannot outlive the task that uses them: fetching, checksumming,
// rendering and deleting have to happen inside a single task or the intermediate data becomes the
// bottleneck. The cost is that this process does four jobs; the scripts in bin/ keep each of them
// separately readable and testable.
process PULLAUTA_GRID {
    tag "${grid_id}"
    label 'process_pullauta'
    container params.container_karttapullautin

    // false by default. `scratch true` does work under podman, but it puts the task's working
    // directory on the *container's* own filesystem, which is the wrong place for tens of
    // gigabytes of laz. The script cleans up after itself either way (see the trap below), so the
    // work dir is left holding only the rendered PNGs regardless of this setting. Turn it on when
    // the node has a fast local disk that the work dir is not already on.
    scratch params.scratch

    input:
    tuple val(grid_id), path(grid_csv), path(shapes_zip), val(meta)
    path effective_ini
    // Pinned to the name RENDER_INI writes into the ini's `vectorconf` key, so a user's shape
    // mapping file can be called anything without karttapullautin failing to find it.
    path(vectorconf, stageAs: 'osm.txt')
    // stageAs, because Nextflow would otherwise stage a directory under its own basename -- and the
    // obvious directory to point this at is called `in`, which is exactly where the laz files are
    // collected. The two would then be the same path, and linking a file "into" it would write into
    // the caller's read-only source tree.
    path(laz_local_dir, stageAs: 'laz_local_src')

    output:
    tuple val(grid_id), val(meta),
          path('out/*.png', arity: '1..*'),
          path('out/*.pgw', arity: '1..*'), emit: rendered, optional: true
    path "failures.${grid_id}.tsv", emit: failures
    path "pullauta.${grid_id}.log", emit: log
    path "download_failures.${grid_id}.tsv", emit: download_failures

    script:
    // Unused optional inputs arrive as named sentinel files from assets/, because Nextflow cannot
    // stage a path that does not exist; distinct sentinel names so two of them cannot collide on one
    // staged filename. This one is read from the param instead, because stageAs renames the sentinel
    // too -- laz_local_dir.name is 'laz_local_src' either way and cannot tell the cases apart.
    def local_opt = params.laz_local_dir ? '--local-dir laz_local_src' : ''
    def limit_rate = params.download_limit_rate ? "--limit-rate '${params.download_limit_rate}'" : ''
    // karttapullautin looks for *.zip in its lazfolder and unzips them itself; there is no separate
    // option for the shapefile set.
    def stage_shapes = shapes_zip.name == 'NO_SHAPES'
        ? "echo 'no OSM shapes for this grid; rendering contours and vegetation only' >&2"
        : "cp -L ${shapes_zip} in/map.shp.zip"
    """
    # karttapullautin never deletes its own temporaries: savetempfiles/savetempfolders control
    # extra *outputs*, not cleanup, which is why the prototype run in testdata/ still has four
    # 1.5 GB temp*.xyz.bin files sitting in it. Left alone, a Bavaria-scale run would fill the work
    # dir with terabytes of them.
    #
    # A trap rather than a plain rm at the end, because Nextflow keeps the task directory when a
    # task fails or is retried: without this, one failed grid strands ~30 GB and a hundred failed
    # grids fill the disk. The declared outputs live in out/ and are untouched here.
    trap 'rm -rf in temp temp[0-9]* ./*.xyz.bin pullautus*.png pullautus*.pgw temp_shapefiles' EXIT

    # karttapullautin's `processes` setting bounds only its tile workers. `image` and `imageproc` are
    # built with rayon and size their thread pools from the machine's core count, and
    # parallel_laz_decompression adds more threads still -- so the task ignores what it was allocated
    # and was measured at 677% CPU for a 2-CPU task. On one big node running several grids at once
    # that oversubscribes the box and makes maxForks arithmetic guesswork. Capping the rayon pool
    # keeps a task roughly within its allocation, which is what lets conf/c8id.config's sizing be
    # computed rather than found by trial.
    export RAYON_NUM_THREADS=${task.cpus}

    # 1. Acquire and verify. Exits non-zero only for failures a retry could fix, so a 404 becomes a
    #    recorded hole while a timeout becomes a Nextflow retry.
    fetch_laz.sh \\
        --csv ${grid_csv} \\
        --outdir in \\
        --failures download_failures.${grid_id}.tsv \\
        --jobs ${params.download_jobs} \\
        --retries ${params.download_retries} \\
        ${local_opt} ${limit_rate}

    ${stage_shapes}

    # 2. Render, surviving tiles that make karttapullautin panic.
    run_pullauta.sh \\
        --grid-id ${grid_id} \\
        --csv ${grid_csv} \\
        --ini ${effective_ini} \\
        --processes ${task.cpus} \\
        --max-attempts ${params.max_pullauta_attempts} \\
        --variant ${params.png_variant} \\
        --log pullauta.${grid_id}.log \\
        --failures failures.${grid_id}.tsv
    """

    stub:
    """
    mkdir -p out
    touch out/stub_depr.png out/stub_depr.pgw
    printf 'tile\\n' > failures.${grid_id}.tsv
    printf 'tile\\n' > download_failures.${grid_id}.tsv
    touch pullauta.${grid_id}.log
    """
}
