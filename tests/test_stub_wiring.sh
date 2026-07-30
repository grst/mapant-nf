#!/usr/bin/env bash
# Run the whole pipeline with stubbed processes and check that the plan and the published pyramid
# agree with each other.
#
# This is the only end-to-end test that needs neither the LiDAR nor a container runtime, so it is
# the one CI can run on every pull request. What it covers is the plumbing: the joins keyed on tile
# and grid ids, the groupKey/groupTuple fan-in from tiles to web-mercator parents, publishing a
# nested z/x/y tree assembled from many tasks, and -resume. Those are where this pipeline's
# regressions have actually been -- both the `remainder: true` bug and the outputDir-above-profiles
# bug would have been caught here.
#
# What it cannot cover is whether any pixel is correct. A stub run is green on a pipeline that
# renders garbage; tests/test_grid_independence.sh and `-profile test_local` are what check that,
# and both need testdata/.
#
# Usage:  tests/test_stub_wiring.sh
set -euo pipefail

readonly REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SCRATCH="${REPO}/.stub_wiring"
readonly OUT="${SCRATCH}/out"
readonly WORK="${SCRATCH}/work"

# From conf/test_stub.config. Kept here as literals rather than parsed out of the config so that a
# change to the profile shows up as a failing assertion instead of silently weakening the test.
readonly EXPECT_GRIDS=2
readonly BASE_ZOOM=13

cd "$REPO"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

pass=0
fail=0
check() {
    local label="$1"
    shift
    if "$@"; then
        printf '    ok   %s\n' "$label"
        pass=$((pass + 1))
    else
        printf '    FAIL %s\n' "$label"
        fail=$((fail + 1))
    fi
}

# One column of a trace file, header row dropped, looked up by name rather than by position: the
# field list lives in nextflow.config, and a reordering there would otherwise make the -resume checks
# below compare an empty column against another empty column and pass.
trace_column() {
    local trace="$1" want="$2"
    awk -F'\t' -v want="$want" -v trace="$trace" '
        NR == 1 {
            for (i = 1; i <= NF; i++) if ($i == want) col = i
            if (!col) {
                printf "no %s column in %s -- check trace.fields in nextflow.config\n", want, trace \
                    > "/dev/stderr"
                exit 1
            }
            next
        }
        { print $col }
    ' "$trace"
}

echo '==> stub run'
nextflow -log "${SCRATCH}/first.log" run . \
    -stub-run \
    -profile test_stub \
    -w "$WORK" \
    --outdir "$OUT" \
    > "${SCRATCH}/first.out" 2>&1 || {
    echo 'FATAL: the stub run itself failed:' >&2
    tail -40 "${SCRATCH}/first.out" >&2
    exit 1
}
# The resumed run at the end of this file overwrites the published trace (trace.overwrite = true), so
# take the first run's task list and statuses now, while they still describe the first run.
trace_column "${OUT}/pipeline_info/trace.txt" name | sort > "${SCRATCH}/first_tasks"
trace_column "${OUT}/pipeline_info/trace.txt" status > "${SCRATCH}/first_status"
printf '    %s task(s) executed\n' "$(wc -l < "${SCRATCH}/first_tasks")"

echo '==> the plan'
check 'grids.csv exists' test -s "${OUT}/pipeline_info/grids.csv"
check "the region split into ${EXPECT_GRIDS} grids" \
    test "$(($(wc -l < "${OUT}/pipeline_info/grids.csv") - 1))" -eq "$EXPECT_GRIDS"
check 'the effective ini was published' test -s "${OUT}/pipeline_info/effective.ini"
check 'plan_summary.txt was published' test -s "${OUT}/pipeline_info/plan_summary.txt"

# The point of the whole tail of the pipeline: every parent tile the plan promised has to come out
# of the pyramid. A broken join key, or a groupTuple that drops short groups, shows up here as
# missing parents while the run still reports success -- which is exactly how it showed up for real.
echo '==> every planned parent tile was published'
awk -F, 'NR > 1 { print $3 "/" $4 "/" $5 }' "${OUT}/pipeline_info/parent_tiles.csv" \
    | sort -u > "${SCRATCH}/planned_parents"
# Guarded rather than relying on find's exit status: under `set -o pipefail` a missing base-zoom
# directory would abort the script here, turning the most informative assertion in the file into an
# unexplained exit.
: > "${SCRATCH}/published_parents"
if [ -d "${OUT}/tiles/${BASE_ZOOM}" ]; then
    find "${OUT}/tiles/${BASE_ZOOM}" -name '*.png' \
        | sed -e "s|^${OUT}/tiles/||" -e 's|\.png$||' \
        | sort -u > "${SCRATCH}/published_parents"
fi
check 'planned and published parent tiles are the same set' \
    cmp -s "${SCRATCH}/planned_parents" "${SCRATCH}/published_parents"
if ! cmp -s "${SCRATCH}/planned_parents" "${SCRATCH}/published_parents"; then
    diff "${SCRATCH}/planned_parents" "${SCRATCH}/published_parents" | head -20 || true
fi
check 'more than one parent tile was produced' \
    test "$(wc -l < "${SCRATCH}/published_parents")" -gt 1

# The pyramid starts at the base zoom -- nothing below it is generated. The stub writes the base zoom
# and one level under it, so both must be published and no shallower level may appear.
echo '==> the pyramid starts at the base zoom'
for z in "$BASE_ZOOM" "$((BASE_ZOOM + 1))"; do
    check "zoom ${z} is present" test -d "${OUT}/tiles/${z}"
done
check 'nothing was published below the base zoom' \
    test ! -d "${OUT}/tiles/$((BASE_ZOOM - 1))"
check 'the viewer was published' test -s "${OUT}/tiles/index.html"
check 'the viewer starts the pyramid at the base zoom' \
    grep -q "minZoom: ${BASE_ZOOM}" "${OUT}/tiles/index.html"
check 'the viewer falls back to OSM below it' \
    grep -q 'tile.openstreetmap.org' "${OUT}/tiles/index.html"

echo '==> QC reporting'
# Header-only: a stub run has no failures, and a QC file that is empty rather than header-only means
# collectFile's keepHeader lost it.
check 'pullauta_failures.tsv is header-only' \
    test "$(wc -l < "${OUT}/qc/pullauta_failures.tsv")" -eq 1
check 'download_failures.tsv is header-only' \
    test "$(wc -l < "${OUT}/qc/download_failures.tsv")" -eq 1
check 'failures.tsv carries the bug-report columns' \
    grep -q 'panic_message' "${OUT}/qc/pullauta_failures.tsv"
check 'failed_grids.txt reports a clean run' \
    grep -q 'No tasks failed permanently' "${OUT}/qc/failed_grids.txt"

# -resume being a full cache hit is worth asserting: a task whose inputs are not stable across runs
# (a timestamp in a staged file, an unsorted collect) silently re-executes, which on a real run means
# re-downloading terabytes after a restart.
echo '==> -resume is a full cache hit'
nextflow -log "${SCRATCH}/resume.log" run . \
    -stub-run \
    -profile test_stub \
    -resume \
    -w "$WORK" \
    --outdir "$OUT" \
    > "${SCRATCH}/resume.out" 2>&1 || {
    echo 'FATAL: the resumed run failed:' >&2
    tail -40 "${SCRATCH}/resume.out" >&2
    exit 1
}
# Asserted from the published trace, not from what the run printed. Nextflow's end-of-run summary line
# comes from whichever log observer is active -- ANSI in a terminal, plain in CI, and a third format
# when NXF_AGENT_MODE/AGENT/CLAUDECODE is set in the environment -- so grepping the console for
# `cached=N` passed under an agent and quietly matched nothing in CI, where an empty match then failed
# all three checks. trace.txt has one row per task with a status, is a documented interface, and is the
# file qc/failed_grids.txt already sends the reader to.
trace_column "${OUT}/pipeline_info/trace.txt" name > "${SCRATCH}/resume_names"
trace_column "${OUT}/pipeline_info/trace.txt" status > "${SCRATCH}/resume_status"
sort "${SCRATCH}/resume_names" > "${SCRATCH}/resume_tasks"
readonly RESUMED="$(wc -l < "${SCRATCH}/resume_status")"
readonly CACHED="$(grep -cx 'CACHED' "${SCRATCH}/resume_status" || true)"
printf '    %s of %s task(s) came from the cache\n' "$CACHED" "$RESUMED"

# Without this the checks below would also pass on a first run that cached nothing to begin with.
check 'the first run executed its tasks rather than reusing an older cache' \
    test "$(grep -cx 'CACHED' "${SCRATCH}/first_status" || true)" -eq 0
check 'the resumed run traced at least one task' test "$RESUMED" -gt 0
check 'the resumed run ran the same set of tasks' \
    cmp -s "${SCRATCH}/first_tasks" "${SCRATCH}/resume_tasks"
if ! cmp -s "${SCRATCH}/first_tasks" "${SCRATCH}/resume_tasks"; then
    diff "${SCRATCH}/first_tasks" "${SCRATCH}/resume_tasks" | head -20 || true
fi
check 'every task came from the cache' test "$CACHED" -eq "$RESUMED"
if [ "$CACHED" -ne "$RESUMED" ]; then
    paste "${SCRATCH}/resume_names" "${SCRATCH}/resume_status" | grep -v 'CACHED' | head -20 || true
fi

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
rm -rf "$SCRATCH"
