// The style, sprite and preview page that make the archive a map.
//
// A vector tile carries classes, not colours, so without its style the map cannot be drawn at all.
// The style is a template in assets/viewer; this fills in what depends on the run -- the colours
// the render took from the ini, and widths in ground metres at the region's latitude.
process MAKE_VIEWER {
    label 'process_single'

    input:
    path parent_tiles
    path effective_ini
    path templates, stageAs: 'template/*'
    val attribution

    output:
    // At the task root: published next to mapant.pmtiles, which index.html loads by that name.
    path '{style.json,sprite*,index.html}', emit: files

    script:
    """
    make_viewer.py \\
        --template-dir template \\
        --parent-tiles ${parent_tiles} \\
        --ini ${effective_ini} \\
        --base-zoom ${params.base_zoom} \\
        --max-zoom ${params.max_zoom} \\
        --title '${params.map_title}' \\
        --attribution '${attribution}' \\
        --out-dir .
    """

    // No stub: it is pure Python over the plan, the ini and the templates, all of which a stub run
    // has for real -- so tests/test_stub_wiring.sh checks the style too.
}
