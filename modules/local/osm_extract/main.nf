// Cut per-grid OSM extracts out of the source .pbf, several grids per pass.
//
// One osmium pass costs a full read of the input -- 809 MB for Bavaria -- so extracting one grid at a
// time would re-read it thousands of times over a full run. The chunk config files from PLAN_GRIDS
// are what let one pass write many extracts.
process OSM_EXTRACT {
    tag "${chunk_id}"
    label 'process_osm_extract'

    input:
    tuple val(chunk_id), path(chunk_json)
    path pbf

    output:
    path 'extracts/*.pbf', emit: pbf

    script:
    """
    mkdir -p extracts

    # --strategy=smart keeps ways and multipolygon relations whole instead of cutting them at the
    # boundary, so a road or lake straddling the grid edge still renders as one object. Its cost is a
    # per-extract id set in memory, which is what bounds params.osm_chunk_size.
    osmium extract \\
        --config ${chunk_json} \\
        --strategy ${params.osm_strategy} \\
        --set-bounds \\
        --overwrite \\
        --no-progress \\
        ${pbf}

    # An extract with no OSM data is legitimate (open water, a military area) but otherwise
    # indistinguishable from a broken bbox.
    for f in extracts/*.pbf; do
        printf '%s: %s\\n' "\$f" "\$(osmium fileinfo -e -g data.count.nodes "\$f" 2>/dev/null || echo '?') nodes" >&2
    done
    """

    // The extract names have to come from the chunk config, not be invented: they are the grid ids
    // that OSM_TO_SHAPES' output is joined on downstream.
    stub:
    """
    mkdir -p extracts
    sed -n 's/.*"output": *"\\([^"]*\\)".*/\\1/p' ${chunk_json} \\
        | while read -r name; do : > "extracts/\${name}"; done
    """
}
