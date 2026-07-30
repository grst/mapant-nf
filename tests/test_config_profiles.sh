#!/usr/bin/env bash
# Check that every profile resolves, and that the settings derived from params survive being
# overridden by one.
#
# The specific bug this exists to prevent: `outputDir = params.outdir` was originally written above
# the `profiles` block in nextflow.config, where a profile's params have not been merged yet. Every
# profile therefore published its pyramid to results/ while its own trace report went to
# results_<name>/, and nothing failed. Config bugs of that shape are invisible in a run that only
# checks whether the pipeline succeeded, so they get their own test.
#
# Usage:  tests/test_config_profiles.sh
set -euo pipefail

readonly REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Every profile that sets params, plus the container-engine profiles it is normally combined with.
readonly PROFILES=(
    'test_stub'
    'test_local'
    'test_immenstadt'
    'c8id'
    'podman,test_stub'
    'podman,local_images,test_local'
    'docker,test_local'
    'apptainer,test_immenstadt'
    'singularity,c8id'
)

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

value_of() {
    # `nextflow config` prints Groovy-ish assignments; take the last one for the key, strip quotes.
    sed -n "s/^ *${2} *= *//p" "$1" | tail -1 | tr -d "'\""
}

for profile in "${PROFILES[@]}"; do
    echo "==> -profile ${profile}"
    out="$(mktemp)"
    if ! nextflow config -profile "$profile" > "$out" 2> "${out}.err"; then
        printf '    FAIL profile does not resolve\n'
        head -20 "${out}.err"
        fail=$((fail + 1))
        rm -f "$out" "${out}.err"
        continue
    fi
    pass=$((pass + 1))
    printf '    ok   profile resolves\n'

    outdir="$(value_of "$out" outdir)"
    output_dir="$(value_of "$out" outputDir)"
    check "outputDir (${output_dir:-unset}) follows outdir (${outdir:-unset})" \
        test -n "$output_dir" -a "$output_dir" = "$outdir"

    # A missing image is a task that fails minutes into a run with "unable to pull".
    for key in container_karttapullautin container_gdal container_osmium container_k2t; do
        check "${key} is set" test -n "$(value_of "$out" "$key")"
    done

    rm -f "$out" "${out}.err"
done

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
