I want to create an automatically generated orienteering map of entire bavaria ("mapant").

Input data: 
 - CSV file with links to tiles of laser scan data in laz format, including checksum and bounding boxes. Example in testdata folder
 - Openstreetmap pbf file with shapes
 - kartapullautin ini file
 - kartapullautin shape mapping file (in our case osm.txt)
 - grid size

 Output:
 - hierarchical directory with png tiles (web mercator).

 Tooling:
  - ogr2ogr to convert shapefiles
  - kartapullautin: https://github.com/karttapullautin/karttapullautin to generate map
  - kartapullautin2tiles https://github.com/grst/karttapullautin2tiles for making the output directory
  
The whole process should be orchestrated using a generic nextflow pipeline that could be applied to other regions/countries (pass in list of laz tiles, get map directory).

Example steps of a prototype of a limited region (ran manually without nextflow 
nextflow) are documented here: https://github.com/grst/mapant-bayern/

Additional considerations:
 * the input data are huge (15+TB for bavaria). I can't afford downloading
   everything upfront, so there must be a single process that does the
   following: (1) download laz files (2) verify checksums (3) run pullauta (4)
   remove all files except the generated png files. The nextflow work directory
   shouldn't be polluted with temp files. 
 * Processing single tiles will result in edge artifacts. Therefore, multiple
   tiles need to be processed in batch mode. But, to enable paralellization
   across nodes, and to avoid downloading everyting, we cannot process *all*
   files at once in batch mode. Therefore, we need to work in grids (e.g.
   10x10). The outer tiles would be thrown away and only the inner tiles used to
   avoid the edge artifacts. The grids therefore overlap to a certain extent,
   resulting in some double-processing -- a tradeoff we have to take. 
 * As a result, we need an initial process that reads the input csv file
   including the bounding boxes and figures out which tiles need to be processed
   together in grids. Each grid is then submitted to a nextflow process. 
 * be wary of different projections. There may be laz files in different UTM
   zones resulting in different rotations. Make sure to reproject as appropriate
   and avoid gaps between the tiles.
 * the pbf file is for entire bavaria. For better efficiency, it might be
   necessary to extract only the information relevant for each grid. Make sure
   that no objects that are required in a grid are lost.
 * kartapullautin benefits from modern cpu features such as avx-512. This
   machine only supports avx2, but make sure that when running on machines that
   support it a version compiled with avx-512 support is used. 
 * some tiles may fail due to a kartapullautin bug. This shouldn't stop the
   entire pipeline, instead tile coordinates and error messages should be
   collected such that this can be reported to the pullautin devs. 

Containers:
 * all process dependencies containerized
 * use separate containers for different processes. I.e. make a separate one for
   kartapullautin, kartapullautin2tiles. Although you can probably reuse
   kartapullautin2tiles for running a generic python script you may want to
   develop. 

Coding conventions:
 * use modern nextflow features, e.g. workflow outputs
 * separate modules in individual files
 * in general, nf-core conventions are great, but keep it lean (no full-blown
   template)

Example data:
From the testdata directory, the following is available: 
 * the CSV with the full list of laz files for bavaria
 * some downloaded laz files of one municipal district
 * the pbf file for bavaria, including the map.shp.zip generated from it using
   ogr2ogr
 * an example pullauta run of the above municipal district

Verify your pipeline on a small test region (around Immenstadt im Allgäu) that
is small enough to be processed on this laptop, but large enough to test the
grids. 

---

Simplifications (1)
---------------
 * prefer a python script over the massive fetch_laz or run_pullauta shell scripts
 * you can always assume that a tile size is larger than the 127m halo ring. No need to check for that, just use one ring. 
 * You can also drop any comments that grid size does not affect quality, that's obvious. 
 * the lat/lon columns in the samplesheet are unnecessary, just get them from the crs internall when needed. 
 * make comments less verbose, just state non-obvious stuff
 * remove the parameters region_tile_regex and min_laz_bytes and scratch and related code
 * always perform input validation, remove corresponding parameter
 * always perform input validation in nextflow. No redundant logic. Remove validate_tiles_in_python and associated code. 
 * container images are no parameters, they can be set in config files. Remove them as parameters. 
 * python has a configparser that can read ini files in the standard lib. Use this over manually parsing ini with regexes. 

Simplifications (2)
-------------------
 * no "tile overviews". In the viewer, just show OSM mapnik at lower zoom levels. 
 
Simplifications (3)
-------------------
 * the nextflow output log is too verbose -- don't print the name of every single tile generated.

Simplifications (4)
-------------------
 * remove the test_local profile, and everything else that points at machine-local configuration
   (the local_images profile, --laz_local_dir). Make test_immenstadt self-contained: put the
   corresponding subset of the laz CSV and a reduced pbf in assets/, so it runs on anyone's machine.
   testdata/ is not tracked by git and nothing in the repository may depend on it.
 
---

k2t optimizations

 - Compare png to lossless webp. How much storage could be saved? 
 - The source render has 14 colours; a z18 tile of the same ground has 2,659. Those come from k2t's resampling, not from the map. Quantising a z18 tile back to a 16-colour palette gives 75–88% smaller files — twice what WebP achieves — but it is lossy relative to the current output (26–63% of pixels change) and I haven't looked at whether it's visually acceptable. It may well look closer to the source render, since it's re-snapping blended pixels back to the cartographic palette, but that's a claim to verify by eye, not one I've tested.

---