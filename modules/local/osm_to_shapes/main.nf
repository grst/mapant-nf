// Turn a grid's OSM extract into an ESRI Shapefile set, reprojected into the grid's own CRS.
//
// They are karttapullautin's input: it matches each shape to its ISOM code by the rules file and
// writes it, cropped per tile, next to the LiDAR vectors. In the grid's CRS because that is the
// one karttapullautin renders in.
//
// Per grid rather than once for the region: each PULLAUTA_GRID task stages only its own grid's
// archive, and a Bavaria-wide archive is never staged anywhere.
process OSM_TO_SHAPES {
    tag "${grid_id}"
    label 'process_low'

    input:
    tuple val(grid_id), path(grid_pbf), val(crs)

    output:
    tuple val(grid_id), path('shapes/*'), emit: shapes

    script:
    """
    mkdir -p shapes

    #   OSM_USE_CUSTOM_INDEXING NO  -- the custom index needs scratch proportional to the input and
    #                                  buys nothing on an extract this small
    #   -skipfailures               -- OSM is full of geometries that cannot be expressed as a
    #                                  shapefile feature; one of them must not fail the grid
    #   -t_srs                      -- karttapullautin draws the shapes in the grid's own CRS,
    #                                  and reprojects them to WGS84 with everything else
    ogr2ogr \\
        --config OSM_USE_CUSTOM_INDEXING NO \\
        -skipfailures \\
        -f 'ESRI Shapefile' \\
        output_shapes \\
        ${grid_pbf} \\
        -overwrite \\
        -t_srs ${crs}

    if compgen -G 'output_shapes/*.shp' > /dev/null; then
        # -j to flatten: karttapullautin pairs .shp with .dbf by name, so the layers have to sit
        # at the root of the archive.
        zip -q -j 'shapes/${grid_id}.shp.zip' output_shapes/*
        printf '%s: %s layer(s)\\n' '${grid_id}' "\$(ls output_shapes/*.shp | wc -l)" >&2
    else
        # No OSM features worth drawing in this grid. A sentinel rather than an empty archive, so
        # that the join downstream still has something to carry: PULLAUTA_GRID stages nothing for it.
        printf 'no OSM features in this grid\\n' > 'shapes/${grid_id}.NONE'
        printf '%s: no OSM features; contours and vegetation only\\n' '${grid_id}' >&2
    fi

    rm -rf output_shapes
    """

    stub:
    """
    mkdir -p shapes
    : > 'shapes/${grid_id}.shp.zip'
    """
}
