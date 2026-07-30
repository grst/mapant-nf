// Read the tiles CSV and decide what gets rendered together.
//
// All the geometry lives in bin/plan_grids.py, which is where it can be unit-tested
// (tests/test_plan_grids.py); this module only marshals parameters.
process PLAN_GRIDS {
    label 'process_single'
    container params.container_k2t

    input:
    path tiles_csv
    // Staged rather than read from $projectDir: a process that reaches outside its own working
    // directory cannot run on an executor without a shared filesystem.
    path tiles_schema

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
    def regex = params.region_tile_regex ? "--tile-regex '${params.region_tile_regex}'" : ''
    def min_bytes = params.min_laz_bytes > 0 ? "--min-laz-bytes ${params.min_laz_bytes}" : ''
    // The samplesheet has already been validated by nf-schema in the workflow, so re-validating here
    // would only cost time. params.validate_tiles_in_python exists for the case where the row count
    // makes nf-schema too slow: same schema file either way.
    def schema = params.validate_tiles_in_python ? "--schema ${tiles_schema}" : ''

    """
    plan_grids.py \\
        --tiles-csv ${tiles_csv} \\
        --outdir . \\
        --grid-size ${params.grid_size} \\
        --halo-m ${params.pullauta_halo_m} \\
        --base-zoom ${params.base_zoom} \\
        --osm-buffer-m ${params.osm_buffer_m} \\
        --osm-chunk-size ${params.osm_chunk_size} \\
        ${region} ${regex} ${min_bytes} ${schema}
    """
}
