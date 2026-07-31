// Render one web-mercator parent tile, and every zoom level beneath it, from the karttapullautin
// PNGs that overlap it.
//
// One task per parent tile: k2t holds a parent's merged source imagery in memory (~1.1 GB for a z12
// parent at the source's 0.42 m/px), so the parent zoom is what bounds the task's footprint -- and
// the per-parent subtrees k2t writes are disjoint, so many tasks publish into one pyramid with no
// merge step.
process MAKE_TILES {
    tag "${p.z}/${p.x}/${p.y}"
    label 'process_tiles'

    input:
    tuple val(p), path(pngs), path(pgws)

    output:
    // Optional because k2t skips a parent whose sources turn out not to overlap it after
    // exact-geometry filtering; the tile->parent map is deliberately conservative.
    path "tiles/**/*.${params.tile_format}", emit: tiles, optional: true

    script:
    // k2t identifies a render by its .pgw sidecar; the default pattern selects the depression
    // variant. Keep it in step with params.png_variant or the task silently finds no input.
    def pattern = params.png_variant == 'plain' ? '*.pgw' : '*depr*.pgw'
    """
    # A one-line tile list instead of `k2t list-tiles`: list-tiles enumerates the whole bounding box
    # of whatever is in the directory, including parents with no data under them, and PLAN_GRIDS has
    # already computed the exact tile->parent mapping from the source bboxes.
    printf '{"x":%d,"y":%d,"z":%d}\\n' ${p.x} ${p.y} ${p.z} > tile.jsonl

    # --format is passed explicitly rather than left to k2t's default: the output glob above and the
    # viewer's tile URL both hardcode the extension, so an upstream change of default would publish
    # nothing rather than fail. (0.2.0 moved that default from png to webp.)
    k2t make-tiles \\
        --proj ${p.crs} \\
        --pattern '${pattern}' \\
        --max-zoom ${params.max_zoom} \\
        --format ${params.tile_format} \\
        --no-include-viewer \\
        . tiles tile.jsonl
    """

    // The parent and its four children, so `-stub-run` checks what is easy to get wrong here: that a
    // nested, multi-depth `tiles/z/x/y.png` tree survives being collected from many tasks and
    // published into one pyramid.
    stub:
    """
    mkdir -p 'tiles/${p.z}/${p.x}'
    : > 'tiles/${p.z}/${p.x}/${p.y}.${params.tile_format}'

    if [ ${p.z} -lt ${params.max_zoom} ]; then
        for dx in 0 1; do
            for dy in 0 1; do
                mkdir -p "tiles/\$((${p.z} + 1))/\$((${p.x} * 2 + dx))"
                : > "tiles/\$((${p.z} + 1))/\$((${p.x} * 2 + dx))/\$((${p.y} * 2 + dy)).${params.tile_format}"
            done
        done
    fi
    """
}
