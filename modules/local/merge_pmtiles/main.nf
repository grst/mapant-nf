// Join the parents' archives into the one the map is served from.
//
// The only step that waits for the whole run, and it has to: a PMTiles archive has one directory.
// It stages one archive per parent -- a few hundred for Bavaria -- never the tiles themselves.
//
// A parent's archive also holds the tiles just outside it that its buffer reaches into, with its
// own side of the border in them. tile-join merges the layers of a tile that is in several inputs,
// so those tiles come out with both sides.
process MERGE_PMTILES {
    label 'process_tiles'

    input:
    path parents, stageAs: 'parents/*'
    val attribution

    output:
    path 'mapant.pmtiles', emit: pmtiles

    script:
    """
    # --no-tile-size-limit: tile-join drops any tile over 500 KB by default, which is exactly the
    # budget-driven thinning make_vector_tiles.py switches off in tippecanoe.
    tile-join \\
        --force \\
        --no-tile-size-limit \\
        --no-tile-stats \\
        --name '${params.map_title}' \\
        --attribution '${attribution}' \\
        --output mapant.pmtiles \\
        parents/*.pmtiles
    """

    // The names of the archives it was given, so a stub run can check that every parent arrived.
    stub:
    """
    ls parents > mapant.pmtiles
    """
}
