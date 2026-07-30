// Cut per-grid OSM extracts out of the source .pbf, several grids per pass.
//
// One osmium pass costs a full read of the input -- 809 MB for Bavaria -- so extracting one grid at
// a time would re-read it once per grid, thousands of times over a full run. osmium can write many
// extracts from a single pass, which is what the chunk config files from PLAN_GRIDS are for.
process OSM_EXTRACT {
    tag "${chunk_id}"
    label 'process_osm_extract'
    container params.container_osmium

    input:
    tuple val(chunk_id), path(chunk_json)
    path pbf

    output:
    path 'extracts/*.pbf', emit: pbf

    script:
    """
    mkdir -p extracts

    # --strategy=smart keeps ways and multipolygon relations whole instead of cutting them at the
    # boundary, so a road or lake that straddles the grid edge still renders as one object. Combined
    # with the buffer PLAN_GRIDS already applied to each bbox, that is what makes "no object a grid
    # needs is lost" true rather than approximately true.
    #
    # The memory cost of that guarantee is a per-extract id set, which is why the chunks are tens of
    # grids rather than hundreds -- see params.osm_chunk_size.
    osmium extract \\
        --config ${chunk_json} \\
        --strategy ${params.osm_strategy} \\
        --set-bounds \\
        --overwrite \\
        --no-progress \\
        ${pbf}

    # An extract with no OSM data at all is legitimate (open water, a military area) but silent, so
    # say so: it is otherwise indistinguishable from a broken bbox.
    for f in extracts/*.pbf; do
        printf '%s: %s\\n' "\$f" "\$(osmium fileinfo -e -g data.count.nodes "\$f" 2>/dev/null || echo '?') nodes" >&2
    done
    """
}
