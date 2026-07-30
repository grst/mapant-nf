// One HTML file that makes the finished pyramid viewable without a server.
//
// This replaces the viewer k2t can emit (`--include-viewer`), which passes a negated max_zoom into
// its template and so comes out with unusable zoom bounds. Generating it once here, from the plan,
// also means the zoom range and extent shown are the ones the run actually produced.
process TILE_VIEWER {
    label 'process_single'
    container params.container_k2t

    input:
    path parent_tiles

    output:
    path 'tiles/index.html', emit: viewer

    script:
    """
    make_viewer.py \\
        --parent-tiles ${parent_tiles} \\
        --out tiles/index.html \\
        --min-zoom ${params.min_zoom} \\
        --max-zoom ${params.max_zoom} \\
        --title '${params.map_title}'
    """
}
