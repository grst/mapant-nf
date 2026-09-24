// The style, metadata and preview page that make the vector pyramid a map.
//
// A vector tile carries classes, not colours, so unlike the raster pyramid this output is not
// viewable without its style -- and the style is where karttapullautin's palette and line widths
// are reproduced. Built once per run from the plan and the effective ini, so the colours match the
// parameters the tiles were actually rendered with.
process VECTOR_VIEWER {
    label 'process_single'

    input:
    path parent_tiles
    path effective_ini

    output:
    path 'tiles_vector/style.json', emit: style
    path 'tiles_vector/metadata.json', emit: metadata
    path 'tiles_vector/index.html', emit: viewer
    // The patterned area symbols -- undergrowth and marsh -- are drawn from this sprite, at both
    // pixel ratios because MapLibre asks for the @2x sheet on a display that has one and draws no
    // pattern at all if that request fails.
    path 'tiles_vector/sprite*.{png,json}', emit: sprite

    script:
    """
    make_vector_style.py \\
        --parent-tiles ${parent_tiles} \\
        --out-dir tiles_vector \\
        --base-zoom ${params.base_zoom} \\
        --max-zoom ${params.max_zoom} \\
        --ini ${effective_ini} \\
        --title '${params.map_title}'
    """

    // No stub: it is pure Python over the plan and the ini, both of which a stub run has for real.
    // That makes the style part of what tests/test_stub_wiring.sh checks -- and the style is the
    // half of a vector pyramid that a stubbed tile cannot stand in for.
}
