// Build the zoom levels below the base zoom from the finished base tiles.
//
// Default-on: a pyramid that starts at z12 cannot be zoomed out to a regional view, which makes the
// output unusable as a map even though every pixel in it is correct.
//
// A single task is enough -- all of Bavaria is about 1600 base tiles of 256 px -- and it has to be
// one task anyway, because a parent tile needs all four of its children and those come from four
// different MAKE_TILES tasks.
process TILE_OVERVIEWS {
    label 'process_low'
    container params.container_k2t

    input:
    path base_tiles

    output:
    // Optional because min_zoom == base_zoom is a legitimate setting meaning "no overviews", and a
    // path declaration with no match fails the task.
    path 'tiles/**/*.png', emit: tiles, optional: true

    script:
    """
    build_overviews.py \\
        --in-dir . \\
        --out-dir tiles \\
        --min-zoom ${params.min_zoom}
    """

    stub:
    """
    # Same halving the real reducer does, applied to the coordinates in the staged base-tile names,
    # so a stub run produces a pyramid with the zoom levels the run claims to have built.
    for f in base_*.png; do
        coords="\${f#base_}"; coords="\${coords%.png}"
        z="\${coords%%_*}"; rest="\${coords#*_}"
        x="\${rest%%_*}"; y="\${rest#*_}"
        while [ "\$z" -gt ${params.min_zoom} ]; do
            z=\$((z - 1)); x=\$((x / 2)); y=\$((y / 2))
            mkdir -p "tiles/\$z/\$x"
            : > "tiles/\$z/\$x/\$y.png"
        done
    done
    """
}
