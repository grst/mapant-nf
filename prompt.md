# Original prompt

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

# Simplifications (1)

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

---

# Simplifications (2)

 * no "tile overviews". In the viewer, just show OSM mapnik at lower zoom levels. 

---

# Simplifications (3)

 * the nextflow output log is too verbose -- don't print the name of every single tile generated.

---

# Simplifications (4)
 
 * remove the test_local profile, and everything else that points at machine-local configuration
   (the local_images profile, --laz_local_dir). Make test_immenstadt self-contained: put the
   corresponding subset of the laz CSV and a reduced pbf in assets/, so it runs on anyone's machine.
   testdata/ is not tracked by git and nothing in the repository may depend on it.

---

# Vector tiles (1): plan and prototype

I developed a "mapant" nextflow pipeline that uses karttapullautin to convert
LIDAR data (.laz files) into orienteering maps. Karttapullautin generates png
images for each laz file, and the nextflow pipeline does the orchestration
around (downloading, batching, and generation of a tile pyramid).

Currently, karttapullautin generates pixel tiles (png) and my pipeline emits a
webp tile pyramid. However, it would be nice to have vector tiles instead: they
are likely smaller and have higher quality. Karttapullautin anyway uses vector
formats as intermediates.

On top of that, the vector data can be used when making "real" competition-grade
orienteering maps. A lot of field work is required, but the auto-generated map
is a good starting point. Therefore, it would be nice to convert the vector
tiles into `.ocd` format.

The new workflow has different components:

(1) make karttapullautin emit vector data in some interoperable format
(2) generate a vector tile pyramid in mapant-nf
(3) convert the vector tiles into ocd format

Considerations regarding (1): output vector data from karttapullautin:
 - This has been discussed in this issue:
   https://github.com/karttapullautin/karttapullautin/issues/116
 - The leading proposition is to generate geojson format. I tend to agree with
   this, as interoperability beats performance at this stage.
 - Nevertheless, some voices raised concerns that geojson would be too large
   when processing data at scale (whole country mapant map). Assess if this is
   the case, and consider if there are other, interoperable formats that could
   be better suited.
 - While for the prototype, you can patch karttapullautin locally as you see
   fit, a production-grade impelementation should be accepted as PR into
   karttapullautin at a later stage. The propostion should therefore align with
   the repo conventions and be minimally invasive in terms of code mofications.
 - For reference, another protoype PR has been made to karttapullautin2tiles:
   https://github.com/itadventurer/karttapullautin2tiles/pull/1 It achieves
   quite high quality, also for the vegetation layer, so it seems to have found
   the right vector data sources. However, it reads karttapullautin's internal
   data formats, so I don't consider it a sustainable solution.
 - Also the OSM shapes should be included in the vector tiles. If this would be
   too invasive at the karttapullautin level, we could probably also redraw
   them from the osm data ourselves later. But this would not be preferred,
   because then the vector tiles could diverge from the karttapullautin pngs.

Considerations regarding (2): generate a vector tile pyramid in mapant-nf
 - For now, this is done using karttapullautin2tiles, which generates a (pixel)
   web mercator tile pyramid
 - One option would be to extend this library to deal with vector tiles
 - However, I'm wondering if an interoperable format like geojson were used, an
   off-the-shelf solution would exist. I heard the tool tippecanoe being dropped
   in that context, but I'm not sure if it's suitable, or if others exist.

Considerations regarding (3): generate ocd from tile pyramid
 - Envisaged user experience: similar to "print" in the mapant-bayern webapp,
   choose an area and download as ocd
 - Check if all necessary information would be encoded in the vector tiles, or
   if additional information would be required
 - I don't know if there's an offical specification of the ocd format, but it
   is the de-facto-standard for editable orienteering maps. In any case,
   openorienteeringmapper has a working parser and writer.
 - I don't want to run a backend server for the webapp, so the conversion from
   tiles to ocd has to be done in the browser. This is the part I'm least
   familiar with. Maybe plain javascript would be enough for this? Or is this a
   use-case for rust+WASM?

Your task is to make a plan for the three components. Research feasibility and
pros/cons of different approaches. Then let's decide together on the best
approach and build a *prototype* to validate the approach on a small region in
bavaria (`test_immenstadt`) end-to-end. The final, production-grade
impelentation will be performed in separate sessions in the respective
repositories. As a last step (prototype validated and accepted by me), generate
hand-off files for the production implementations. The production impelmentation
will hinge on getting the vector-output functionality merged into
karttapullautin.

Follow-up:

 * I can see some issues with the vector tiles though: (1) vegetation: the
   output of karttapullautin is all based on squares. The vectorization
   sometimes simplifies the shapes. This is an issue if there's a border between
   different shades of green and one gets simplified and the other doesn't: the
   output will then show small blank areas between the two shapes. (2) OSM
   shapes (in particular streets) do not look like their ISOM symbols. There's
   also gaps in roads/paths where there are none in the pixel representation (3)
   There's apparent borders between different base zoom tiles. E.g a lake gets
   shown at lower zoom levels in one, but not in the other. Maybe tippecanoe can
   be forced to show the same things in all base tiles? Or tippecanoe needs to
   be run on the entire map in one go? Or maybe showing different things at
   different zoom levels is not even the right thing for orienteering maps. Or
   maybe we can customize deterministically what to show at which zoom level?
   E.g. show form lines only at z16, but regular contours at z15. Hide buildings
   at z14 etc.
 * Can you elaborate on the undergrowth issue? Can this be fixed downstream of
   karttapullautin to make the pixel and vector tiles look more similar?

---

# Vector tiles (2): no more raster tiles

 * Does karttapullautin do anything special with the osm symbols? Maybe, after
   all, it would be easier to run karttapullautin entirely without OSM shapes
   and add them at a later stage in the pipeline.
 * We can get rid of the raster tiles entirely. Please implement the prototype
   for test_immenstadt accordingly.
 * Are the bitmasks for water and block actually required? Does a pullauta
   render with default settings even include them in the png?

---

# Vector tiles (3): based on malpou's karttapullautin fork

Last time we worked on a prototype end-to-end pipeline to generate vector
mapant-style maps. The condition was to create changes to karttapullautin
minimal and work off the generated bitmaps. After consulting the karttapullautin
authors, this premise has changed:

- As per the discussion in
  https://github.com/karttapullautin/karttapullautin/issues/116, in the future
  there shall be no focus on the bitmap, but they envisage adding vectorization
  support to karttapullautin
- There has been done substantial work on this in @malpou's fork:
  https://github.com/malpou/karttapullautin.
- Any future work shall be based on top of @malpou's work

Tasks:

1) check the different feature branches and summarize which features they
   contain. Can all newly added features be stacked on each other?
2) Implement the prototype based on malpou's features. Stack the feature
   branches where applicable into one "final" version. The goal would be to
   directly feed the generated geojsons into tippecanoe without additional
   transformations on the mapant-nf side. Is there anything missing? What would
   have to be changed on the karttapullautin side?

Follow-up:

 * some contours do not perfectly match at the tile boundaries. There's a small
   offset/gap
 * very small hills do not appear as round shapes but instead are a single
   brown line
 * formline depressions are brown, not purple. Could they actually be turned
   into real depression symbols at the style level (add depression lines)
 * the cliffs. export them from kp in some form that makes them look closer to
   the original png version
 * (large) water bodies should be marked as "unpassable" at the style levels
   (add black border).
 * fix the export of the undergrowth such that the correct symbols are
   exported. In the original png, both symbols appear to be there

---

# Production-ready

The prototype works as expected. Now let's focus on getting things
production-ready. Work on the mapant-nf pipeline only for now (kp is fine with
the current changes) and we'll work on the webapp downstream.

 * Remove the "laz cache" feature entirely. This was useful for prototyping, but
   the production pipeline doesn't need this feature.
 * Evaluate if any of the changes can be simplified/solved more elegantly to
   keep the complexity of the code and the pipeline low
 * Build the kp container against my kp fork
   (https://github.com/grst/karttapullautin/pull/3)
 * add the option to generate a pmtiles in addition or instead of the tile
   pyramid (can tippecanoe generate pmtiles directly? How would one merge them
   from the different grids)?
 * tile size should be 512px
 * update the metro map
 * I don't like that the make viewer and make style are mixed into one script.
   Maybe some template files could go to `assets`. Could the style even be one
   static file? How much is gained from generating it from the script (happy to
   keep it if it makes things simpler and more readable)
 * remove options from the nextflow config and nextflow schema that were only
   applicable for pixel maps
 * Roll back the README changes. I'll update that file manually myself.

Follow-up:

 * tippecanoe can natively generate pmtiles. Would that feature be used or how
   do you intend to convert to pmtiles? -- ok, so no pbf intermediates? that
   sounds good.
 * I think it is important that the colors are read from the ini. Also the
   pipeline should be generic enough to work with any region in the world, not
   just bavaria. How does that change your assessment? Maybe a compromise could
   be to store templates as asset and modify some parts dynamically?

---

# isom-maplibre, single pmtiles, configs per tile

(OSM/ini config changes in mapant-bayern omitted)

Karttapullautin

- where to fix: in kp code, in the branch with my modifications. Docker
  container in mapant-nf to be updated accordingly.
- there are contours in some alpine lakes (e.g. großer Alpsee). Crop the
  contours against lakes (but not rivers) such that it looks better in the
  webapp when using the isom maplibre style.

mapant-nf

- update the pipeline to work better together with the isom-maplibre style. See
  mapant-bayern/HANDOFF-isom-maplibre.md.
- the pipeline should output a single merged pmtiles file, no directory tree
  with the different zoom levels
- pmtiles should have appropriate headers, e.g. bounding box
- tile size shall be 512px
- there shall be an additional lower zoom level in the vector tiles that does
  not have any contour lines
- adjust the pipeline such that it reads the pullauta configs from the
  samplesheet and plans the grids accordingly to deal with multiple configs. It
  shall still be possible to supply a single config via params, but the
  samplesheet takes precedence.

Generate test pmtiles (to be viewed in the mapant-bayern viewer directly) for
the region between 47.552616, 10.141566 and 47.617981, 10.272019. It contains
most of the issues mentioned above and serves as a manual test.

Follow-up:

 * Address also (4) cliffs by exporting the top edge from kp only. For the
   style, settle on the isom-maplibre style, at the expense of not taking the
   ini into account. I think it makes sense to keep the style independent, like
   that the appearance can be tuned without running the entire kp pipeline
   again.
 * powerlines have no poles. I'm quite sure pole positions are recorded in OSM
   data
 * make the contour lines even a bit smoother, at high zoom levels (18) there
   are still clear corners visible in what should be roundish hills.
 * Depressions don't show in the style. They should use "ticks" orthogonal to
   the slope line.
 * The ring as a center of this view showed as a depression (purple) in the old
   pixel version of mapant-bayern. However, now, it does not show as a
   depression. What's going on?

---

# Overview zoom levels

 * Which zoom levels does the pipeline generate currently in pmtiles and what do
   the respective layers contain? So z12 and 13 are effectively identical? Are
   both contained in the pyramid redundantly? Does tippecanoe offer a way to
   simplify vegetation at lower zoom levels?
 * at zoom 10 and 11 set the size-threshold. at 10/11, additionally do not show
   minor paths/roads.
 * Small (paved) roads should show at z11, but vehicle tracks should not. You
   can also apply the size filter to z12. I find it unintuitive to count in
   make_vector_tiles.py once from the top and once from the bottom when
   specifying details. I think it should all be from the bottom (highest zoom).
   So in our case 15 = 0, show at 14 = -1, show at 13 = -2 and so on.
 * Can you explain the overview_min_area_px better? Does it remove the same
   elements on all zoom levels, or can we make it remove more on z10, a little
   less on z11 and even less on z12? -- ok, change it as implemented and filter
   up to and including z13. At z10, it should remove significantly more than in
   the current version.
 * Ok, we can simplify that: Just remove objects below a certain size, even if
   they are at the tile border or at a block border. If in that case only half
   of an object gets removed and the other is visible, I'm fine with that. You
   can also use a much higher threshold, let's go for ~12ha cutoff at z10.

---

# More flexible input

 * update mapant-nf such that (1) checksums in the samplesheet are optional. If
   the col does not exist/is not filled for a given tile, the check is simply
   skipped. (2) Allow tiles to be packed in a .zip file. If tiles have that
   extension, unzip them automatically. This is to accommodate the laserscan
   data for different german federal states. Test with a few tiles from Sachsen
   (.zip) and NRW (no checksum).
 * Accept sha-1 checksums, by using a `sha1:` prefix in the checksum column.
   Assume sha256 if no prefix present. Make size_bytes optional.
