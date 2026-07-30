// Build the one karttapullautin ini the whole run uses, and publish it as the run's provenance.
//
// Once, not per grid: it is both cheaper and a guarantee that every grid was rendered with the same
// parameters.
process RENDER_INI {
    label 'process_single'

    input:
    // stageAs, because a staged input is a symlink to the *user's* file: editing it in place would
    // rewrite their ini.
    path(ini_in, stageAs: 'user.ini')
    val processes

    output:
    path 'effective.ini', emit: ini

    script:
    // Empty vectorconf disables vector rendering, which is what a region with no OSM extract needs.
    def vectorconf = params.osm_pbf ? 'osm.txt' : ''
    """
    render_ini.py \\
        --in-ini user.ini \\
        --out-ini effective.ini \\
        --processes ${processes} \\
        --vectorconf '${vectorconf}'
    """
}
