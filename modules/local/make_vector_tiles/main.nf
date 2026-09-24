// Cut one web-mercator parent tile's vector pyramid from the vectors under it.
//
// One task per base-zoom parent, each writing a disjoint z/x/y subtree, so the pyramid assembles
// itself from many tasks with no merge step and no barrier at the end of the run.
//
// Two inputs meet here for the first time: karttapullautin's per-tile bundles, which are the
// LiDAR, and the OSM archives of the grids those tiles came from. Matching the shapes to their
// ISOM codes is the one thing karttapullautin used to do for this pipeline that it no longer does,
// and doing it here means a shape is cut once for the parent instead of once per square kilometre
// that touches it.
//
// Unlike a raster tiler this task holds a whole parent's *vectors* in memory while it sorts them
// into layers, which is what its memory follows from -- roughly 150k features per square kilometre
// of alpine terrain, nearly all of them cliff ticks.
process MAKE_VECTOR_TILES {
    tag "${p.z}/${p.x}/${p.y}"
    label 'process_tiles'

    input:
    tuple val(p), path(bundles), path(shapes)
    path effective_ini
    // Pinned to a fixed name so that a user's shape mapping file can be called anything.
    path(vectorconf, stageAs: 'osm.txt')

    output:
    // Optional for the same reason the raster tiler's was: the tile->parent map is deliberately
    // conservative, so a parent can turn out to have nothing under it after exact clipping.
    path 'tiles_vector/**/*.pbf', emit: tiles, optional: true

    script:
    // No OSM at all is normal -- a region with no extract renders as contours and vegetation.
    // Matched on the parameter rather than on the staged file, which is a sentinel under the same
    // name when there is none.
    def rules = params.osm_pbf && params.vectorconf ? 'osm.txt' : ''
    """
    # Whichever grids this parent draws from staged their archive here; a grid whose extract held
    # nothing drawable staged a .NONE sentinel instead, which this glob does not match. When there
    # is no archive at all the file is never written, and the tiler cuts the LiDAR alone -- it takes
    # --osm unconditionally and ignores a path that is not there.
    if [ -n '${rules}' ] && compgen -G '*.shp.zip' > /dev/null; then
        osm_shapes.py --rules '${rules}' --out osm.geojsonl ./*.shp.zip
    else
        echo 'no OSM shapes for this parent; cutting the LiDAR alone' >&2
    fi

    make_vector_tiles.py \\
        --parent ${p.z} ${p.x} ${p.y} \\
        --proj ${p.crs} \\
        --max-zoom ${params.max_zoom} \\
        --ini ${effective_ini} \\
        --osm osm.geojsonl \\
        . tiles_vector
    """

    // The parent plus its four children, so a stub run checks the thing that is easy to get wrong:
    // that a nested multi-depth tree survives collection from many tasks into one pyramid.
    stub:
    """
    mkdir -p 'tiles_vector/${p.z}/${p.x}'
    : > 'tiles_vector/${p.z}/${p.x}/${p.y}.pbf'

    if [ ${p.z} -lt ${params.max_zoom} ]; then
        for dx in 0 1; do
            for dy in 0 1; do
                mkdir -p "tiles_vector/\$((${p.z} + 1))/\$((${p.x} * 2 + dx))"
                : > "tiles_vector/\$((${p.z} + 1))/\$((${p.x} * 2 + dx))/\$((${p.y} * 2 + dy)).pbf"
            done
        done
    fi
    """
}
