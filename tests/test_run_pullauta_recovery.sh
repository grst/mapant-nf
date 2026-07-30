#!/usr/bin/env bash
# Test bin/run_pullauta.sh's recovery from a renderer that panics.
#
# "Some tiles may fail due to a karttapullautin bug; that must not stop the pipeline, and the tile
# coordinates and error messages must be collected" is a requirement that is very easy to believe you
# have implemented and never actually exercise -- the offending tile is by definition one you do not
# have. So the renderer is stubbed (tests/stub_pullauta) to reproduce karttapullautin's failure
# behaviour exactly, and the recovery logic is tested against it.
#
# Usage:  tests/test_run_pullauta_recovery.sh
set -euo pipefail

readonly REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0

# run_pullauta.sh calls `pullauta`; the stub takes that name on PATH.
readonly BIN="${TMP}/bin"
mkdir -p "$BIN"
cp "${REPO}/tests/stub_pullauta" "${BIN}/pullauta"
chmod +x "${BIN}/pullauta"
export PATH="${BIN}:${REPO}/bin:${PATH}"

# The failure report reads the pullauta version from the image; there is no image here.
mkdir -p "${TMP}/fakeopt/karttapullautin"

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

# Build a case directory: a grid CSV plus empty stand-ins for the laz files.
setup_case() {
    local dir="$1"; shift
    local -a core=() halo=()
    local mode=core
    local arg
    for arg in "$@"; do
        case "$arg" in
            --halo) mode=halo ;;
            *) [ "$mode" = core ] && core+=("$arg") || halo+=("$arg") ;;
        esac
    done

    rm -rf "$dir"
    mkdir -p "${dir}/in"
    cp "${REPO}/assets/pullauta.ini" "${dir}/effective.ini"

    {
        echo 'tile,url,sha256,size_bytes,role,crs,min_x,min_y,max_x,max_y'
        local t
        for t in "${core[@]}"; do
            echo "${t}.laz,https://example.invalid/${t}.laz,$(printf '0%.0s' {1..64}),1000,core,EPSG:25832,0,0,1000,1000"
            : > "${dir}/in/${t}.laz"
        done
        for t in "${halo[@]}"; do
            echo "${t}.laz,https://example.invalid/${t}.laz,$(printf '0%.0s' {1..64}),1000,halo,EPSG:25832,0,0,1000,1000"
            : > "${dir}/in/${t}.laz"
        done
    } > "${dir}/grid.csv"
}

run_case() {
    local dir="$1" processes="${2:-2}"
    ( cd "$dir" && run_pullauta.sh \
        --grid-id test_grid \
        --csv grid.csv \
        --ini effective.ini \
        --processes "$processes" \
        --max-attempts 6 \
        --variant depr \
        --log pullauta.log \
        --failures failures.tsv > stdout.txt 2> stderr.txt ) && echo 0 || echo $?
}

rendered_tiles() { ls "$1"/out/*_depr.pgw 2> /dev/null | xargs -r -n1 basename | sed 's/_depr\.pgw$//' | sort | tr '\n' ' '; }
failure_rows() { [ -f "$1/failures.tsv" ] && tail -n +2 "$1/failures.tsv" | wc -l || echo 0; }
failed_tiles() { tail -n +2 "$1/failures.tsv" 2> /dev/null | cut -f1 | sort | tr '\n' ' '; }

# ---------------------------------------------------------------------------
echo '==> the happy path: every core tile rendered, halo tiles are not'
# The halo placeholder trick is the thing to verify here. Halo tiles exist in the input folder so
# their points are available, but they must not be *rendered*: at grid_size 10 the ring is 36% of the
# folder, and rendering it would be pure waste.
D="${TMP}/happy"
setup_case "$D" a1 a2 --halo h1 h2
rc="$(run_case "$D")"
check 'exit status' 0 "$rc"
check 'core tiles rendered' 'a1 a2 ' "$(rendered_tiles "$D")"
check 'no failures recorded' 0 "$(failure_rows "$D")"
check 'halo placeholders cleaned up' '' "$(ls "$D"/out/h1.png "$D"/out/h2.png 2> /dev/null || true)"

# ---------------------------------------------------------------------------
echo '==> one tile panics: it is recorded and skipped, the rest still render'
D="${TMP}/one_crash"
setup_case "$D" a1 a2 a3 --halo h1
rc="$(STUB_CRASH_TILES=a2 run_case "$D")"
check 'exit status is success' 0 "$rc"
check 'the other tiles rendered' 'a1 a3 ' "$(rendered_tiles "$D")"
check 'one failure recorded' 1 "$(failure_rows "$D")"
check 'and it names the right tile' 'a2 ' "$(failed_tiles "$D")"
check 'the panic message was captured' 1 \
    "$(grep -c 'could not read LAZ points' "$D/failures.tsv" || true)"
check 'the blacklist placeholder was removed' '' "$(ls "$D"/out/a2.png 2> /dev/null || true)"
# A zero-byte PNG left in out/ would be published as a corrupt map tile.
check 'no empty PNGs left behind' 0 "$(find "$D/out" -name '*.png' -empty | wc -l)"

# ---------------------------------------------------------------------------
echo '==> two tiles panic: the ladder isolates them one at a time'
D="${TMP}/two_crashes"
setup_case "$D" a1 a2 a3 a4
rc="$(STUB_CRASH_TILES='a2 a4' run_case "$D")"
check 'exit status is success' 0 "$rc"
check 'the good tiles rendered' 'a1 a3 ' "$(rendered_tiles "$D")"
check 'both failures recorded' 2 "$(failure_rows "$D")"
check 'and both are named' 'a2 a4 ' "$(failed_tiles "$D")"

# ---------------------------------------------------------------------------
echo '==> a tile abandoned mid-write is re-rendered, not published half-finished'
# karttapullautin's own resume logic only checks that <t>.png exists, so a tile whose write was
# interrupted by a sibling's panic would be skipped forever and shipped corrupt. The recovery code
# has to notice the missing IEND chunk and delete the quartet so it gets redone.
D="${TMP}/truncated"
setup_case "$D" a1 a2 a3
rc="$(STUB_CRASH_TILES=a2 STUB_TRUNCATE=a3 run_case "$D" 2)"
check 'exit status is success' 0 "$rc"
check 'the truncated tile was redone' 1 \
    "$(ls "$D"/out/a3_depr.pgw > /dev/null 2>&1 && echo 1 || echo 0)"
# The _depr variant, because the plain one is pruned at the end (params.png_variant).
check 'and its PNG is complete' '49454e44ae426082' \
    "$(tail -c 8 "$D/out/a3_depr.png" | od -An -tx1 | tr -d ' \n')"
check 'quarantining was reported' yes \
    "$([ "$(grep -c 'quarantined' "$D/stderr.txt" || true)" -ge 1 ] && echo yes || echo no)"

# ---------------------------------------------------------------------------
echo '==> a failure before any tile starts is not blamed on a tile'
# A bad ini or an unreadable shapefile archive is an environment problem. Recording it against
# whichever tile happened to be next would hide a real fault and corrupt the bug report, so the
# script must fail the task instead and let Nextflow retry it.
D="${TMP}/early"
setup_case "$D" a1 a2
rc="$(STUB_FAIL_EARLY=1 run_case "$D")"
check 'exit status is failure' 101 "$rc"
check 'no tile was blamed' 0 "$(failure_rows "$D")"

# ---------------------------------------------------------------------------
echo '==> a grid where everything panics fails rather than silently emitting nothing'
D="${TMP}/all_crash"
setup_case "$D" a1 a2
rc="$(STUB_CRASH_TILES='a1 a2' run_case "$D")"
check 'exit status is failure' 1 "$rc"
check 'both tiles recorded anyway' 2 "$(failure_rows "$D")"

# ---------------------------------------------------------------------------
echo '==> a core tile whose laz never arrived is a recorded hole, not a crash'
D="${TMP}/missing"
setup_case "$D" a1 a2
rm -f "${D}/in/a2.laz"
rc="$(run_case "$D")"
check 'exit status is success' 0 "$rc"
check 'the present tile rendered' 'a1 ' "$(rendered_tiles "$D")"
check 'the missing one is recorded' 'a2 ' "$(failed_tiles "$D")"
check 'with a useful reason' 1 "$(grep -c 'laz file unavailable' "$D/failures.tsv" || true)"

# ---------------------------------------------------------------------------
echo '==> resuming a partly finished grid re-renders only what is missing'
# This is karttapullautin's own skip-if-output-exists behaviour, which the pipeline leans on for both
# retries and the blacklist. If it broke, a retried grid would redo hours of work.
D="${TMP}/resume"
setup_case "$D" a1 a2 a3
run_case "$D" > /dev/null
rm -f "$D"/out/a2*.png "$D"/out/a2*.pgw
rc="$(run_case "$D")"
check 'exit status is success' 0 "$rc"
check 'all tiles present' 'a1 a2 a3 ' "$(rendered_tiles "$D")"
check 'only the missing tile was re-rendered' 1 \
    "$(grep -c 'in/a2.laz ->' "$D/pullauta.log" || true)"

# ---------------------------------------------------------------------------
printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
