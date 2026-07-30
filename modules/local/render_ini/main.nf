// Build the one karttapullautin ini the whole run uses, and publish it as provenance.
//
// A separate process rather than a few lines inside PULLAUTA_GRID for two reasons: the result is
// identical for every grid, so doing it once is both cheaper and a guarantee that all grids were
// rendered with the same parameters; and publishing it is what makes a finished map reproducible.
process RENDER_INI {
    label 'process_single'
    container params.container_k2t

    input:
    // stageAs, because a staged input is a symlink to the *user's* file: editing it in place would
    // silently rewrite their ini. render_ini.py reads this and writes a separate output.
    path(ini_in, stageAs: 'user.ini')
    val processes

    output:
    path 'effective.ini', emit: ini

    script:
    // Empty vectorconf disables vector rendering entirely, which is what a region with no OSM
    // extract needs -- karttapullautin then draws contours and vegetation only.
    def vectorconf = params.osm_pbf ? 'osm.txt' : ''
    """
    render_ini.py \\
        --in-ini user.ini \\
        --out-ini effective.ini \\
        --processes ${processes} \\
        --vectorconf '${vectorconf}'
    """
}
