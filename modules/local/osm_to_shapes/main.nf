// Turn a grid's OSM extract into an ESRI Shapefile set, reprojected into the grid's own CRS.
//
// These used to be karttapullautin's input: it drew the shapes onto the rendered image. Now they
// are MAKE_VECTOR_TILES' input, and bin/osm_shapes.py matches them to their ISOM codes there --
// but the shape of this step is unchanged, because what it produces is the same archive and the
// reprojection is still needed (tippecanoe wants WGS84, and the tiler reprojects from the render's
// CRS along with everything else).
//
// Per grid rather than once for the region: the archive is joined to the tiles under a parent, so
// a parent stages only the grids it actually draws from, and a Bavaria-wide archive is never
// staged anywhere.
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
    #   -t_srs                      -- so that the shapes arrive on the same grid as the render,
    #                                  which is what lets the tiler reproject both with one
    #                                  transformer
    ogr2ogr \\
        --config OSM_USE_CUSTOM_INDEXING NO \\
        -skipfailures \\
        -f 'ESRI Shapefile' \\
        output_shapes \\
        ${grid_pbf} \\
        -overwrite \\
        -t_srs ${crs}

    if compgen -G 'output_shapes/*.shp' > /dev/null; then
        # Named after the grid, not 'map': a parent tile that draws from two grids stages both
        # archives into one directory, and two files called map.shp.zip would collide there.
        #
        # -j to flatten: osm_shapes.py pairs .shp with .dbf by name, so the layers have to sit at
        # the root of the archive.
        zip -q -j 'shapes/${grid_id}.shp.zip' output_shapes/*
        printf '%s: %s layer(s)\\n' '${grid_id}' "\$(ls output_shapes/*.shp | wc -l)" >&2
    else
        # No OSM features worth drawing in this grid. A sentinel rather than an empty archive, so
        # that the join downstream still has something to carry: osm_shapes.py skips any input that
        # is not a zip.
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
