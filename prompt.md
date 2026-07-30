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
