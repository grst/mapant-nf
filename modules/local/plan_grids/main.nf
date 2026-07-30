// Read the tiles CSV and decide what gets rendered together.
//
// All the geometry lives in bin/plan_grids.py, where it can be unit-tested; this module only
// marshals parameters.
process PLAN_GRIDS {
    label 'process_single'

    input:
    path tiles_csv

    output:
    path 'grids/*.csv', emit: grid_csvs
    path 'grids.csv', emit: grid_index
    path 'parent_tiles.csv', emit: parent_index
    path 'osm_chunks/*.json', emit: osm_chunks
    path 'plan_summary.txt', emit: summary

    script:
    def region = params.region_bbox
        ? "--bbox '${params.region_bbox}' --bbox-crs '${params.region_bbox_crs}'"
        : ''
    """
    plan_grids.py \\
        --tiles-csv ${tiles_csv} \\
        --outdir . \\
        --grid-size ${params.grid_size} \\
        --base-zoom ${params.base_zoom} \\
        --osm-buffer-m ${params.osm_buffer_m} \\
        --osm-chunk-size ${params.osm_chunk_size} \\
        ${region}
    """
}
