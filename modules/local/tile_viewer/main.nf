// One HTML file that makes the finished pyramid viewable without a server.
//
// Replaces the viewer k2t can emit (`--include-viewer`), which passes a negated max_zoom into its
// template and so comes out with unusable zoom bounds. Built from the plan, so the extent and zoom
// range shown are the ones the run produced. Below the base zoom it shows OSM's own tiles, which is
// why the pipeline does not build overview levels.
process TILE_VIEWER {
    label 'process_single'

    input:
    path parent_tiles

    output:
    path 'tiles/index.html', emit: viewer

    script:
    """
    make_viewer.py \\
        --parent-tiles ${parent_tiles} \\
        --out tiles/index.html \\
        --base-zoom ${params.base_zoom} \\
        --max-zoom ${params.max_zoom} \\
        --tile-format ${params.tile_format} \\
        --title '${params.map_title}'
    """
}
