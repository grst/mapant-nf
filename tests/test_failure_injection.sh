#!/usr/bin/env bash
# End-to-end: a corrupt input file must not stop the run, and must be reported.
#
# tests/test_run_pullauta_recovery.sh covers the renderer panicking, with a stubbed renderer. This
# covers the other half of the same requirement with the real thing: a laz file that is there but
# wrong. It is the failure mode most likely to happen in practice -- a truncated download, a bad
# mirror -- and the one whose damage is quietest, because karttapullautin will happily render
# whatever points it managed to read and produce a plausible but wrong map tile. The only defence is
# the checksum, so this test exists to prove the checksum is actually load-bearing.
#
# Expected outcome: the run finishes green, the three intact tiles are rendered and published, and the
# corrupt one is named in qc/download_failures.tsv and qc/pullauta_failures.tsv as a hole.
#
# Usage:  tests/test_failure_injection.sh [profile]
set -euo pipefail

readonly REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PROFILE="${1:-podman}"
readonly SOURCE_DIR="${REPO}/testdata/kemptner_wald/in"
readonly BROKEN_TILE='609_5285'
readonly SCRATCH="${REPO}/.failure_injection"

cd "$REPO"
[ -d "$SOURCE_DIR" ] || { echo "SKIP: ${SOURCE_DIR} not present" >&2; exit 0; }

rm -rf "$SCRATCH"
mkdir -p "${SCRATCH}/laz"

# A mirror of the real files, with one of them truncated. Symlinks for the intact ones so this costs
# no disk; the corrupt one is a real (short) file. Deliberately *not* committed as a fixture -- it is
# derived from testdata, which is not in the repository.
echo "==> building a corrupt mirror in ${SCRATCH}/laz"
for f in "$SOURCE_DIR"/*.laz; do
    ln -s "$f" "${SCRATCH}/laz/$(basename "$f")"
done
rm -f "${SCRATCH}/laz/${BROKEN_TILE}.laz"
head -c 1048576 "${SOURCE_DIR}/${BROKEN_TILE}.laz" > "${SCRATCH}/laz/${BROKEN_TILE}.laz"
echo "    ${BROKEN_TILE}.laz truncated to $(stat -Lc %s "${SCRATCH}/laz/${BROKEN_TILE}.laz") bytes"

# Easting 608-609 km x northing 5284-5285 km: one grid, four core tiles, one of them corrupt.
echo '==> running the pipeline'
nextflow -log "${SCRATCH}/nextflow.log" run . \
    -profile "${PROFILE},test_local" \
    -w "${SCRATCH}/work" \
    --outdir "${SCRATCH}/out" \
    --laz_local_dir "${SCRATCH}/laz" \
    --region_bbox '608000,5284000,610000,5286000' \
    > "${SCRATCH}/stdout.txt" 2>&1
rc=$?

pass=0
fail=0
check() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        printf '    ok   %s\n' "$label"
        pass=$((pass + 1))
    else
        printf '    FAIL %s: expected %q, got %q\n' "$label" "$expected" "$actual" >&2
        fail=$((fail + 1))
    fi
}

echo '==> checking'
check 'the run finished successfully' 0 "$rc"

# The whole point: one bad file must not cost the other three tiles in its grid.
rendered="$(find "${SCRATCH}/work" -name '*_depr.pgw' -printf '%f\n' 2> /dev/null \
    | sed 's/_depr\.pgw$//' | sort -u | tr '\n' ' ')"
check 'the three intact tiles rendered' '608_5284 608_5285 609_5284 ' "$rendered"

check 'tiles were published' yes \
    "$([ "$(find "${SCRATCH}/out/tiles" -name '*.webp' 2> /dev/null | wc -l)" -gt 0 ] && echo yes || echo no)"

dl="${SCRATCH}/out/qc/download_failures.tsv"
check 'one download failure recorded' 1 "$(tail -n +2 "$dl" 2> /dev/null | wc -l)"
check 'and it names the corrupt tile' "${BROKEN_TILE}.laz" \
    "$(tail -n +2 "$dl" 2> /dev/null | cut -f1 | head -1)"
check 'as a permanent failure' permanent "$(tail -n +2 "$dl" 2> /dev/null | cut -f3 | head -1)"
check 'because verification failed' yes \
    "$(grep -q 'verification' "$dl" && echo yes || echo no)"

pf="${SCRATCH}/out/qc/pullauta_failures.tsv"
check 'one render failure recorded' 1 "$(tail -n +2 "$pf" 2> /dev/null | wc -l)"
check 'and it names the corrupt tile' "$BROKEN_TILE" \
    "$(tail -n +2 "$pf" 2> /dev/null | cut -f1 | head -1)"
# The report has to be actionable: coordinates and a URL, not just a name.
check 'with its coordinates' 609000 \
    "$(tail -n +2 "$pf" 2> /dev/null | cut -f5 | head -1 | cut -d. -f1)"
check 'and its source URL' yes \
    "$(grep -q 'geodaten.bayern.de' "$pf" && echo yes || echo no)"

check 'no temp files left behind' 0 \
    "$(find "${SCRATCH}/work" \( -name '*.laz' -o -name '*.xyz.bin' -o -name 'temp[0-9]*' \) | wc -l)"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
