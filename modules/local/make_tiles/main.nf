// Render one web-mercator parent tile, and every zoom level beneath it, from the karttapullautin
// PNGs that overlap it.
//
// One task per parent tile, rather than one task over the whole output directory, for two reasons.
// k2t holds the merged source imagery for a parent in memory -- roughly 1.1 GB for a z12 parent at
// the source's 0.42 m/px -- so the parent zoom is what bounds the task's footprint. And the
// per-parent subtrees k2t writes are disjoint, so many tasks can publish into one pyramid with no
// merge step and no conflicts.
process MAKE_TILES {
    tag "${p.z}/${p.x}/${p.y}"
    label 'process_tiles'
    container params.container_k2t

    input:
    tuple val(p), path(pngs), path(pgws)

    output:
    path 'tiles/**/*.png', emit: tiles, optional: true
    // The same image again under a unique, self-describing name. Nextflow stages inputs by
    // basename, so collecting tiles/12/2145/1423.png from a thousand tasks would collide on
    // "1423.png" and lose the z/x that gives it meaning. TILE_OVERVIEWS needs those coordinates.
    path "base_${p.z}_${p.x}_${p.y}.png", emit: base, optional: true

    script:
    // k2t identifies a render by its .pgw sidecar; the default pattern selects the depression
    // variant. Keep it in step with params.png_variant or the task silently finds no input.
    def pattern = params.png_variant == 'plain' ? '*.pgw' : '*depr*.pgw'
    """
    # A one-line tile list instead of `k2t list-tiles`: list-tiles enumerates the whole bounding box
    # of whatever is in the directory, including parents with no data under them, and PLAN_GRIDS has
    # already computed the exact tile->parent mapping from the source bboxes.
    printf '{"x":%d,"y":%d,"z":%d}\\n' ${p.x} ${p.y} ${p.z} > tile.jsonl

    k2t make-tiles \\
        --proj ${p.crs} \\
        --pattern '${pattern}' \\
        --max-zoom ${params.max_zoom} \\
        --no-include-viewer \\
        . tiles tile.jsonl

    # k2t skips a parent whose sources turn out not to overlap it after exact-geometry filtering
    # (the tile->parent map is deliberately conservative), so this file is not guaranteed to exist.
    if [ -f 'tiles/${p.z}/${p.x}/${p.y}.png' ]; then
        cp 'tiles/${p.z}/${p.x}/${p.y}.png' 'base_${p.z}_${p.x}_${p.y}.png'
    else
        echo 'no imagery overlapped ${p.z}/${p.x}/${p.y}; nothing emitted' >&2
    fi
    """

    // Writes the parent and its four children, so `-stub-run` checks the thing that is actually
    // easy to get wrong here: that a nested, multi-depth `tiles/z/x/y.png` tree survives being
    // collected from many tasks and published into one pyramid.
    stub:
    """
    mkdir -p 'tiles/${p.z}/${p.x}'
    : > 'tiles/${p.z}/${p.x}/${p.y}.png'
    cp 'tiles/${p.z}/${p.x}/${p.y}.png' 'base_${p.z}_${p.x}_${p.y}.png'

    if [ ${p.z} -lt ${params.max_zoom} ]; then
        for dx in 0 1; do
            for dy in 0 1; do
                mkdir -p "tiles/\$((${p.z} + 1))/\$((${p.x} * 2 + dx))"
                : > "tiles/\$((${p.z} + 1))/\$((${p.x} * 2 + dx))/\$((${p.y} * 2 + dy)).png"
            done
        done
    fi
    """
}
