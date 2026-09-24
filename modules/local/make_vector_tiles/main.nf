// Cut one web-mercator parent tile's vector pyramid from the vectors under it.
//
// One task per base-zoom parent, each writing a disjoint z/x/y subtree, so the pyramid assembles
// itself from many tasks with no merge step and no barrier at the end of the run.
//
// The bundles are karttapullautin's per-tile GeoJSON, already in WGS84 and in their published
// form, OSM included; they go to tippecanoe as they are.
process MAKE_VECTOR_TILES {
    tag "${p.z}/${p.x}/${p.y}"
    label 'process_tiles'

    input:
    tuple val(p), path(bundles)

    output:
    // Optional: the tile->parent map is deliberately conservative, so a parent can turn out to
    // have nothing under it after exact clipping.
    path 'tiles_vector/**/*.pbf', emit: tiles, optional: true

    script:
    """
    make_vector_tiles.py \\
        --parent ${p.z} ${p.x} ${p.y} \\
        --max-zoom ${params.max_zoom} \\
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
