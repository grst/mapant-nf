#!/usr/bin/env bash
# Check that an image can actually do the one job the pipeline gives it.
#
# Run by CI on every image it builds, and usable by hand against a local build:
#
#   containers/smoke.sh tiler                                 # localhost/mapant/tiler:latest
#   containers/smoke.sh karttapullautin ghcr.io/grst/mapant-nf/karttapullautin:c2a060f
#
# These are not unit tests for the tools; they are checks for the handful of things that have gone
# wrong here before, each of which produced a failure that named something other than its cause:
# a missing `ps` reported as a task metrics error, a missing libexpat reported as a rasterio import
# error, a gdal build without the OSM driver reported as an empty shapefile.
set -euo pipefail


engine() {
    if command -v docker > /dev/null 2>&1; then
        echo docker
    elif command -v podman > /dev/null 2>&1; then
        echo podman
    else
        echo 'FATAL: neither docker nor podman found' >&2
        exit 1
    fi
}

readonly NAME="${1:-}"
[ -n "$NAME" ] || {
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 2
}
readonly ENGINE="$(engine)"
readonly REF="${2:-localhost/mapant/${NAME}:latest}"

pass=0
fail=0

# Runs a command inside the image. The images set CMD but no ENTRYPOINT, so argv is the command.
run() { "$ENGINE" run --rm "$REF" "$@"; }

check() {
    local label="$1"
    shift
    if "$@" > /tmp/smoke.out 2>&1; then
        printf '    ok   %s\n' "$label"
        pass=$((pass + 1))
    else
        printf '    FAIL %s\n' "$label"
        sed 's/^/         | /' /tmp/smoke.out | head -10
        fail=$((fail + 1))
    fi
}

echo "==> ${REF} (${ENGINE})"

# Every image, no exceptions. Nextflow runs its task wrapper under bash and shells out to `ps` for
# task metrics; an image missing either fails every task assigned to it with a message about
# metrics collection rather than about the image. See containers/build.sh.
check 'bash is present' run bash -c 'command -v bash'
check 'ps is present (nextflow task metrics)' run bash -c 'command -v ps'

case "$NAME" in
    karttapullautin)
        # The pipeline does download, checksum verification, rendering and cleanup in one process, so
        # this image needs curl and the Python that drives them as much as the renderer.
        check 'curl is present' run bash -c 'command -v curl'
        check 'python3 runs bin/*.py' \
            run python3 -c 'import configparser, csv, hashlib, subprocess; print("stdlib ok")'
        check 'RUST_BACKTRACE is set (panic reports are useless without it)' \
            run bash -c '[ "${RUST_BACKTRACE:-0}" = 1 ]'

        # Both of these run on any x86-64 machine, so both are executed rather than merely inspected:
        # a binary built for the wrong ISA dies with SIGILL, which is exactly what the wrapper exists
        # to prevent. The banner is karttapullautin's own, so seeing it proves the binary ran.
        for isa in baseline v3; do
            check "the ${isa} build runs" \
                "$ENGINE" run --rm -e "PULLAUTA_ISA=${isa}" -w /tmp "$REF" \
                bash -c 'pullauta | grep -q Karttapullautin'
        done

        # The dispatch wrapper's own choice on this machine, whatever it is. On a runner with AVX-512
        # this is the only place the v4 binary is ever executed.
        printf '    ..   auto-selected ISA on this host: '
        run bash -c 'pullauta --version 2>&1 >/dev/null | head -1' || true

        # prompt.md requires that an AVX-512 machine gets an AVX-512 build, and that cannot be
        # verified by running the binary on a machine without AVX-512 -- which is every machine this
        # has been built on so far. Disassembling it instead works anywhere: zmm registers appear in
        # the v4 build and in neither of the others. This is the actual proof that the requirement is
        # met, so a missing objdump is a failure rather than a skip.
        if command -v objdump > /dev/null 2>&1; then
            for isa in baseline v3 v4; do
                run cat "/opt/karttapullautin/pullauta-${isa}" > "/tmp/pullauta-${isa}"
            done
            zmm() { objdump -d "/tmp/pullauta-$1" | grep -c 'zmm' || true; }
            check "the v4 build contains AVX-512 instructions ($(zmm v4) zmm operands)" \
                test "$(zmm v4)" -gt 0
            check "the v3 build contains none ($(zmm v3))" test "$(zmm v3)" -eq 0
            check "the baseline build contains none ($(zmm baseline))" test "$(zmm baseline)" -eq 0
            rm -f /tmp/pullauta-baseline /tmp/pullauta-v3 /tmp/pullauta-v4
        else
            printf '    FAIL objdump not available; cannot verify the AVX-512 build\n'
            fail=$((fail + 1))
        fi

        # Written by the build from the source checkout, and quoted in every failure report so the
        # karttapullautin developers know exactly which commit produced the panic. It has to be the
        # one the Containerfile pins.
        want="$(sed -n 's/^ARG PULLAUTA_REF=//p' "$(dirname -- "${BASH_SOURCE[0]}")/karttapullautin/Containerfile")"
        check "the pinned commit ${want:0:7} is recorded" \
            run bash -c "grep -qx '${want}' /opt/karttapullautin/GIT_SHA"
        ;;

    gdal)
        check 'ogr2ogr runs' run ogr2ogr --version
        # A gdal built without the OSM driver produces an empty shapefile set rather than an error,
        # which surfaces three processes later as a map with no roads on it.
        check 'the OSM driver is present' run bash -c "ogr2ogr --formats | grep -qi -- '^ *OSM '"
        # karttapullautin does not read a shapefile directory; it looks for a zip and unpacks it.
        check 'zip is present' run bash -c 'command -v zip'
        ;;

    osmium)
        check 'osmium runs' run osmium --version
        check 'osmium extract is available' run osmium extract --help
        ;;

    tiler)
        check 'tippecanoe runs' run tippecanoe --version
        # The flags MAKE_VECTOR_TILES and MERGE_PMTILES use, end to end: an option this tippecanoe
        # does not have fails every tiling task minutes into a run.
        check 'tippecanoe writes PMTiles and tile-join merges them' run bash -c '
            set -e; cd /tmp
            echo "{\"type\":\"Feature\",\"properties\":{},\"geometry\":{\"type\":\"Point\",\"coordinates\":[10.2,47.5]}}" > p.json
            tippecanoe --force --output=a.pmtiles --maximum-zoom=10 --no-tile-size-limit --no-feature-limit \
                --drop-rate=1 --detect-shared-borders --no-simplification-of-shared-nodes \
                --no-tile-stats --no-progress-indicator --named-layer=p:p.json 2> /dev/null
            tile-join --force --no-tile-size-limit --no-tile-stats --name t --attribution a \
                --output=b.pmtiles a.pmtiles a.pmtiles 2> /dev/null
            head -c 7 b.pmtiles | grep -q PMTiles'
        # What bin/*.py imports: pyproj for plan_grids.py, mercantile for the tile arithmetic, and
        # Pillow for the sprite make_viewer.py draws.
        check 'the python imports' run python -c 'import pyproj, mercantile, PIL'
        ;;

    *)
        echo "FATAL: no smoke test defined for '${NAME}'" >&2
        exit 2
        ;;
esac

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
