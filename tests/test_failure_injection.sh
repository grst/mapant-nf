#!/usr/bin/env bash
# End-to-end: a corrupt input file must not stop the run, and must be reported.
#
# tests/test_run_pullauta.py covers the renderer panicking, with a stubbed renderer. This covers the
# other half of the same requirement with the real thing: a laz file that arrives but is wrong. It is
# the failure mode most likely to happen in practice -- a truncated download, a bad mirror, a stale
# checksum in the CSV -- and the one whose damage is quietest, because karttapullautin will happily
# render whatever points it managed to read and produce a plausible but wrong map tile. The only
# defence is the checksum, so this test exists to prove the checksum is actually load-bearing.
#
# The injection is a copy of assets/laz_tiles_immenstadt.csv with one core tile's url pointed at a
# different (real, much smaller) tile on the same server. Everything else about that row -- size,
# sha256, bbox -- is untouched, so the server delivers a complete file that is not the file the CSV
# describes. Rewriting the CSV rather than the bytes on disk is what lets this test run on a machine
# that has never seen the dataset.
#
# Expected outcome: the run finishes green, the three intact tiles are rendered and published, and the
# corrupt one is named in qc/download_failures.tsv and qc/pullauta_failures.tsv as a hole.
#
# Downloads ~2.6 GiB and takes ~10 minutes. Usage:  tests/test_failure_injection.sh [profile]
set -euo pipefail

readonly REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PROFILE="${1:-podman}"
readonly TILES_CSV="${REPO}/assets/laz_tiles_immenstadt.csv"
readonly BROKEN_TILE='591_5269'
# 589_5269.laz is the smallest file in the region (39 MB against the 136 MB the CSV claims for
# 591_5269, so the size check alone settles it), and it is a halo tile of this grid anyway.
readonly WRONG_FILE='589_5269.laz'
readonly SCRATCH="${REPO}/.failure_injection"

cd "$REPO"

rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

echo "==> writing a CSV that misdescribes ${BROKEN_TILE}.laz"
awk -F, -v OFS=, -v tile="${BROKEN_TILE}.laz" -v wrong="$WRONG_FILE" '
    NR > 1 && $1 == tile { sub(/[^\/]*$/, wrong, $2); n++ }
    { print }
    END { if (n != 1) { print "expected exactly one row for " tile ", got " n+0 > "/dev/stderr"; exit 1 } }
' "$TILES_CSV" > "${SCRATCH}/tiles.csv"
echo "    $(awk -F, -v t="${BROKEN_TILE}.laz" '$1 == t { print $1 " -> " $2 }' "${SCRATCH}/tiles.csv")"

# Easting 590-591 km x northing 5268-5269 km: one grid, four core tiles, one of them corrupt.
#
# download_retries 1 because the point of the injection is bytes that are wrong every time: the
# default 3 would re-fetch the same wrong file twice more, with backoff, to reach the same verdict.
echo '==> running the pipeline'
# `|| rc=$?` rather than a bare call: under `set -e` a failing run would abort the script here, and
# "the run finished successfully" is one of the things this test is asserting, not a precondition.
rc=0
nextflow -log "${SCRATCH}/nextflow.log" run . \
    -profile "${PROFILE},test_immenstadt" \
    -w "${SCRATCH}/work" \
    --outdir "${SCRATCH}/out" \
    --tiles_csv "${SCRATCH}/tiles.csv" \
    --region_bbox '590000,5268000,592000,5270000' \
    --download_retries 1 \
    > "${SCRATCH}/stdout.txt" 2>&1 || rc=$?

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
check 'the three intact tiles rendered' '590_5268 590_5269 591_5268 ' "$rendered"

check 'tiles were published' yes \
    "$([ "$(find "${SCRATCH}/out/tiles" -name '*.webp' 2> /dev/null | wc -l)" -gt 0 ] && echo yes || echo no)"

dl="${SCRATCH}/out/qc/download_failures.tsv"
check 'one download failure recorded' 1 "$(tail -n +2 "$dl" 2> /dev/null | wc -l)"
check 'and it names the corrupt tile' "${BROKEN_TILE}.laz" \
    "$(tail -n +2 "$dl" 2> /dev/null | cut -f1 | head -1)"
# Permanent, not transient. A whole file of the wrong bytes arrives again on every retry, so calling
# it transient would fail the task, exhaust its retries and lose the entire grid instead of one tile.
check 'as a permanent failure' permanent "$(tail -n +2 "$dl" 2> /dev/null | cut -f3 | head -1)"
check 'because verification failed' yes \
    "$(grep -q 'verification' "$dl" && echo yes || echo no)"

pf="${SCRATCH}/out/qc/pullauta_failures.tsv"
check 'one render failure recorded' 1 "$(tail -n +2 "$pf" 2> /dev/null | wc -l)"
check 'and it names the corrupt tile' "$BROKEN_TILE" \
    "$(tail -n +2 "$pf" 2> /dev/null | cut -f1 | head -1)"
# The report has to be actionable: coordinates and a URL, not just a name.
check 'with its coordinates' 591000 \
    "$(tail -n +2 "$pf" 2> /dev/null | cut -f5 | head -1 | cut -d. -f1)"
check 'and its source URL' yes \
    "$(grep -q 'geodaten.bayern.de' "$pf" && echo yes || echo no)"

check 'no temp files left behind' 0 \
    "$(find "${SCRATCH}/work" \( -name '*.laz' -o -name '*.xyz.bin' -o -name 'temp[0-9]*' \) | wc -l)"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
