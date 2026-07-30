// Download a grid's laz files, verify them, render the core tiles, and leave nothing behind but the
// PNGs.
//
// Deliberately one process: the input for Bavaria is 15+ TB, so the laz files cannot be staged as
// Nextflow inputs and must not outlive the task that uses them.
process PULLAUTA_GRID {
    tag "${grid_id}"
    label 'process_pullauta'

    input:
    tuple val(grid_id), path(grid_csv), path(shapes_zip)
    path effective_ini
    // Pinned to the name RENDER_INI writes into the ini's `vectorconf` key, so a user's shape
    // mapping file can be called anything.
    path(vectorconf, stageAs: 'osm.txt')
    // stageAs, because Nextflow would otherwise stage this directory under its own basename -- and
    // the obvious directory to point it at is called `in`, which is where the laz files are
    // collected. Linking a file "into" it would write into the caller's read-only source tree.
    path(laz_local_dir, stageAs: 'laz_local_src')

    output:
    tuple val(grid_id),
          path('out/*.png', arity: '1..*'),
          path('out/*.pgw', arity: '1..*'), emit: rendered, optional: true
    path "failures.${grid_id}.tsv", emit: failures
    path "pullauta.${grid_id}.log", emit: log
    path "download_failures.${grid_id}.tsv", emit: download_failures

    script:
    // Read from the param rather than from the staged name: stageAs renames the sentinel too, so
    // laz_local_dir.name is 'laz_local_src' either way and cannot tell the cases apart.
    def local_opt = params.laz_local_dir ? '--local-dir laz_local_src' : ''
    def limit_rate = params.download_limit_rate ? "--limit-rate '${params.download_limit_rate}'" : ''
    // karttapullautin looks for *.zip in its lazfolder and unzips them itself; there is no separate
    // option for the shapefile set.
    def stage_shapes = shapes_zip.name == 'NONE'
        ? "echo 'no OSM shapes for this grid; rendering contours and vegetation only' >&2"
        : "cp -L ${shapes_zip} in/map.shp.zip"
    """
    # karttapullautin never deletes its own temporaries -- savetempfiles/savetempfolders control
    # extra *outputs*, not cleanup. A trap rather than a plain rm at the end, because Nextflow keeps
    # the task directory when a task fails or is retried: without this, one failed grid strands
    # ~30 GB. The declared outputs live in out/ and are untouched here.
    trap 'rm -rf in temp temp[0-9]* ./*.xyz.bin pullautus*.png pullautus*.pgw temp_shapefiles' EXIT

    # karttapullautin's `processes` setting bounds only its tile workers: `image` and `imageproc` are
    # built with rayon and size their thread pools from the machine's core count, which measured 677%
    # CPU for a 2-cpu task. Capping the rayon pool keeps a task within its allocation, which is what
    # lets conf/c8id.config's sizing be computed rather than guessed.
    export RAYON_NUM_THREADS=${task.cpus}

    # Exits non-zero only for failures a retry could fix, so a 404 becomes a recorded hole while a
    # timeout becomes a Nextflow retry.
    fetch_laz.py \\
        --csv ${grid_csv} \\
        --outdir in \\
        --failures download_failures.${grid_id}.tsv \\
        --jobs ${params.download_jobs} \\
        --retries ${params.download_retries} \\
        ${local_opt} ${limit_rate}

    ${stage_shapes}

    run_pullauta.py \\
        --grid-id ${grid_id} \\
        --csv ${grid_csv} \\
        --ini ${effective_ini} \\
        --processes ${task.cpus} \\
        --max-attempts ${params.max_pullauta_attempts} \\
        --variant ${params.png_variant} \\
        --log pullauta.${grid_id}.log \\
        --failures failures.${grid_id}.tsv
    """

    // The stub names its outputs after the grid's actual core tiles, because the join it feeds is
    // keyed on those names: a stub that invented a name would make MAKE_TILES receive nothing while
    // the run still reported success.
    stub:
    def suffix = params.png_variant == 'plain' ? '' : '_depr'
    """
    mkdir -p out
    awk -F, 'NR > 1 && \$5 == "core" { sub(/\\.la[sz]\$/, "", \$1); print \$1 }' ${grid_csv} \\
        | while read -r stem; do
              : > "out/\${stem}${suffix}.png"
              : > "out/\${stem}${suffix}.pgw"
          done

    # The same headers the real scripts write: collectFile keeps the first one it sees, so a stub
    # emitting a different set would publish a QC file with the wrong columns.
    printf 'tile\\trole\\tgrid_id\\tcrs\\tmin_x\\tmin_y\\tmax_x\\tmax_y\\tsize_bytes\\turl\\tsha256\\tpullauta_version\\tpullauta_git_sha\\tisa_variant\\texit_code\\treason\\tpanic_message\\tlog_tail\\n' > failures.${grid_id}.tsv
    printf 'tile\\trole\\toutcome\\tdetail\\n' > download_failures.${grid_id}.tsv
    printf 'stub run: nothing was downloaded or rendered\\n' > pullauta.${grid_id}.log
    """
}
