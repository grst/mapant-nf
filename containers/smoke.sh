#!/usr/bin/env bash
# Check that an image can actually do the one job the pipeline gives it.
#
# Run by CI on every image it builds, and usable by hand against a local build:
#
#   containers/smoke.sh k2t                                   # localhost/mapant/k2t:latest
#   containers/smoke.sh karttapullautin ghcr.io/grst/mapant-nf/karttapullautin:2.13.0
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
        # this image needs the download tooling as much as the renderer.
        check 'curl is present' run bash -c 'command -v curl'
        check 'sha256sum is present' run bash -c 'command -v sha256sum'
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
        # karttapullautin developers know exactly which commit produced the panic.
        check 'the upstream commit is recorded' \
            run bash -c 'grep -Eq "^[0-9a-f]{40}$" /opt/karttapullautin/GIT_SHA'
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

    k2t)
        check 'k2t runs' run k2t --help
        check 'make-tiles is available' run k2t make-tiles --help
        # rasterio's wheel links libexpat but does not vendor it, and python:slim does not have it.
        # Importing is the only way to find out; pip install reports success either way.
        check 'the geo stack imports' \
            run python -c 'import karttapullautin2tiles, geopandas, shapely, pyproj, mercantile, rasterio, PIL, jsonschema'
        # This image is also the generic Python image, so it has to be able to run bin/*.py.
        check 'jsonschema is present for the samplesheet contract' \
            run python -c 'import jsonschema; print(jsonschema.__version__)'
        ;;

    *)
        echo "FATAL: no smoke test defined for '${NAME}'" >&2
        exit 2
        ;;
esac

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
