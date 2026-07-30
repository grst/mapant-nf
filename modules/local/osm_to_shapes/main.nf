// Turn a grid's OSM extract into the zipped ESRI Shapefile set karttapullautin renders vectors
// from, reprojected into the grid's own CRS.
//
// Doing this per grid is not an optimisation, it is what makes vector rendering feasible at all:
// karttapullautin unzips the shapefile archive once per invocation and then re-lists the unpacked
// directory *for every tile it renders*. Handing it the Bavaria-wide archive would mean unpacking
// 30 GB per grid task and walking it a hundred times.
process OSM_TO_SHAPES {
    tag "${grid_id}"
    label 'process_low'
    container params.container_gdal

    input:
    tuple val(grid_id), path(grid_pbf), val(meta)

    output:
    tuple val(grid_id), path('shapes/*'), emit: shapes

    script:
    """
    mkdir -p shapes

    # The flags come from the manual prototype this pipeline automates:
    #   OSM_USE_CUSTOM_INDEXING NO  -- the custom index needs scratch space proportional to the
    #                                  input and buys nothing on an extract this small
    #   -skipfailures               -- OSM is full of geometries that cannot be expressed as a
    #                                  shapefile feature; one of them must not fail the grid
    #   -t_srs                      -- karttapullautin cannot reproject, so the shapes have to
    #                                  arrive in the same CRS as the point cloud
    ogr2ogr \\
        --config OSM_USE_CUSTOM_INDEXING NO \\
        -skipfailures \\
        -f 'ESRI Shapefile' \\
        output_shapes \\
        ${grid_pbf} \\
        -overwrite \\
        -t_srs ${meta.crs}

    if compgen -G 'output_shapes/*.shp' > /dev/null; then
        # -j to flatten: karttapullautin expects the layers at the root of the archive.
        zip -q -j shapes/map.shp.zip output_shapes/*
        printf '%s: %s layer(s)\\n' '${grid_id}' "\$(ls output_shapes/*.shp | wc -l)" >&2
    else
        # ogr2ogr produced no layers, so this grid has no OSM features worth drawing. Emitting a
        # sentinel rather than an empty archive is deliberate: given a zip, karttapullautin takes its
        # has_zip path and expects the vector render to have produced its intermediate images, so
        # handing it an archive with nothing in it invites a confusing downstream failure.
        #
        # Written here rather than copied from assets/ because a process must not reach for
        # \$projectDir: on an executor without a shared filesystem that path does not exist.
        printf 'no OSM features in this grid\\n' > shapes/NO_SHAPES
        printf '%s: no OSM features; contours and vegetation only\\n' '${grid_id}' >&2
    fi

    rm -rf output_shapes
    """
}
