// Build the karttapullautin ini a configuration renders with, and publish it as the run's
// provenance.
//
// Once per configuration (bin/plan_grids.py: the samplesheet's `pullauta_ini`, or the
// --pullauta_ini default), not per grid: it is both cheaper and a guarantee that every grid of a
// configuration was rendered with the same parameters.
process RENDER_INI {
    tag "${ini_id}"
    label 'process_single'

    input:
    // stageAs, because a staged input is a symlink to the *user's* file: editing it in place would
    // rewrite their ini.
    tuple val(ini_id), path(ini_in, stageAs: 'user.ini')
    val processes

    output:
    tuple val(ini_id), path("effective.${ini_id}.ini"), emit: ini

    script:
    // Empty vectorconf disables karttapullautin's shapefile pass, which is what a region with no
    // OSM extract needs.
    def vectorconf = params.osm_pbf && params.vectorconf ? 'osm.txt' : ''
    """
    render_ini.py \\
        --in-ini user.ini \\
        --out-ini effective.${ini_id}.ini \\
        --processes ${processes} \\
        --vectorconf '${vectorconf}'
    """
}
