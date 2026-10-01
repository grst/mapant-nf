// Cut one web-mercator parent tile's share of the map from the vectors under it.
//
// One task per base-zoom parent, so tiling overlaps rendering instead of waiting for it. Each writes
// its own PMTiles archive; MERGE_PMTILES joins them.
//
// The bundles are karttapullautin's per-tile GeoJSON, already in WGS84 and in their published
// form, OSM included. The tiler sorts them into isom-maplibre's tables, gives the OSM shapes their
// ISOM 2017-2 code from the crosswalk, and adds the rendered tiles' footprints (from the plan) as
// the map's coverage.
process MAKE_VECTOR_TILES {
    tag "${p.z}/${p.x}/${p.y}"
    label 'process_tiles'

    input:
    tuple val(p), path(bundles)
    path crosswalk
    path parent_tiles

    output:
    // Optional: the tile->parent map is deliberately conservative, so a parent can turn out to
    // have nothing under it after exact clipping.
    path "${p.z}-${p.x}-${p.y}.pmtiles", emit: pmtiles, optional: true

    script:
    """
    make_vector_tiles.py \\
        --parent ${p.z} ${p.x} ${p.y} \\
        --max-zoom ${params.max_zoom} \\
        --crosswalk ${crosswalk} \\
        --parent-tiles ${parent_tiles} \\
        . ${p.z}-${p.x}-${p.y}.pmtiles
    """

    stub:
    """
    : > ${p.z}-${p.x}-${p.y}.pmtiles
    """
}
