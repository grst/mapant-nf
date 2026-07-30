#!/usr/bin/env bash
# Prove that a tile's render does not depend on which grid it was batched into.
#
# This is the assumption the entire grid design rests on. karttapullautin builds each tile from every
# input file overlapping the tile's own bounds expanded by 127 m, then crops back to the tile
# (src/process.rs::batch_process), so as long as a tile's immediate neighbours are present its output
# should be byte-identical no matter how many other tiles shared the batch. If that were false,
# grid_size would be a quality setting rather than a download/disk trade-off, `params.grid_size`
# could not be tuned freely, and the map would have seams along every grid boundary.
#
# The test renders 608_5284 twice:
#   run A  alone      -- region is that one tile, so the grid has 1 core tile and 8 halo tiles
#   run B  batched    -- region is a 2x2 block, so the same grid has 4 core tiles and 12 halo tiles
# and compares the PNG bytes.
#
# Both runs use the same container, so the same ISA build; comparing across ISA levels would be
# meaningless because floating-point contraction and autovectorisation differ between them.
#
# Runs in about 8 minutes on a laptop. Usage:  tests/test_grid_independence.sh [profile]
set -euo pipefail

readonly REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PROFILE="${1:-podman}"
readonly TILE='608_5284'
readonly SCRATCH="${REPO}/.grid_independence"

cd "$REPO"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

# Pinning the ISA makes the comparison reproducible across machines as well as across runs.
export PULLAUTA_ISA="${PULLAUTA_ISA:-baseline}"

run_case() {
    local name="$1" bbox="$2" grid_size="$3"
    echo "==> ${name}: bbox=${bbox} grid_size=${grid_size}"
    nextflow -log "${SCRATCH}/${name}.log" run . \
        -profile "${PROFILE},test_local" \
        -w "${SCRATCH}/work_${name}" \
        --outdir "${SCRATCH}/out_${name}" \
        --region_bbox "$bbox" \
        --grid_size "$grid_size" \
        > "${SCRATCH}/${name}.stdout" 2>&1 \
        || { echo "FAIL: ${name} did not complete; see ${SCRATCH}/${name}.stdout" >&2; exit 1; }

    # The karttapullautin render itself is an intermediate, not a published output, so take it from
    # the task directory. There is exactly one grid containing this tile in each run.
    local found
    found="$(find "${SCRATCH}/work_${name}" -name "${TILE}_depr.png" -print -quit)"
    [ -n "$found" ] || { echo "FAIL: ${name} produced no ${TILE}_depr.png" >&2; exit 1; }
    cp "$found" "${SCRATCH}/${name}.png"
    echo "    ${name}: $(stat -Lc %s "${SCRATCH}/${name}.png") bytes"
}

# One tile on its own.
run_case alone '608000,5284000,609000,5285000' 2
# The same tile inside a 2x2 core block: easting 608-609 km, northing 5284-5285 km.
run_case batched '608000,5284000,610000,5286000' 2

echo '==> comparing'
count_core() {
    awk -F, '$5 == "core"' "$(find "$1" -name 'grid_*.csv' -print -quit)" | wc -l
}
echo "    core tiles in run A: $(count_core "${SCRATCH}/work_alone")"
echo "    core tiles in run B: $(count_core "${SCRATCH}/work_batched")"

if cmp -s "${SCRATCH}/alone.png" "${SCRATCH}/batched.png"; then
    echo "PASS: ${TILE} rendered identically alone and batched (ISA ${PULLAUTA_ISA})"
    echo "      grid_size is a download/disk trade-off, not a quality setting."
    exit 0
fi

echo "FAIL: ${TILE} differs between the two runs" >&2
ls -l "${SCRATCH}/alone.png" "${SCRATCH}/batched.png" >&2
cat >&2 <<'EOF'

That would mean a tile's output depends on its batch, i.e. the map has seams at grid boundaries and
params.grid_size cannot be tuned freely. Check first:
  * that both runs really used the same ISA build (grep 'ISA variant' in the qc logs),
  * that the halo in both runs contains the same files (the grid_*.csv files under each work dir),
  * whether karttapullautin's 127 m constant changed (params.pullauta_halo_m must be >= it).
EOF
exit 1
