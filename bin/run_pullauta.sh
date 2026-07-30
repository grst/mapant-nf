#!/usr/bin/env bash
# Render a grid's core tiles with karttapullautin, surviving the tiles that make it panic.
#
# Why this is a loop rather than one invocation
# ---------------------------------------------
# karttapullautin v2 uses .unwrap()/.expect() throughout, so a malformed or degenerate laz file
# aborts the *process*, not just that tile. `launch_threads` joins worker 1 first, so a panic
# anywhere takes the whole run down while the other workers are mid-tile: one bad tile can cost up
# to `processes - 1` innocent tiles' work, and the last of the four output files can be left
# half-written. Collecting those crashes for an upstream bug report -- without losing the other
# ninety-odd tiles in the grid -- is an explicit requirement.
#
# Two facts from the karttapullautin source make a clean recovery possible:
#
#   * Plan::new_from_input_files() queues a laz file only if `<batchoutfolder>/<stem>.png` does not
#     already exist, so pullauta resumes by itself: re-running skips everything already done.
#   * plan.input_files() -- the list consulted for the 127 m halo -- contains *every* laz file,
#     including ones excluded from the queue by the check above.
#
# So an empty placeholder PNG makes a tile invisible to the renderer while its points remain
# available to its neighbours. That is used twice: once to stop halo tiles being rendered at all
# (they exist only to supply points), and once to blacklist a tile that panics. Blacklisting this
# way rather than deleting the laz is what keeps the *neighbours* correct -- removing the file
# would silently degrade their borders instead.
#
# Attribution: with several workers running, a panic cannot be pinned on a tile from the log alone.
# Rather than parse thread ids, an attempt that makes no progress is retried with processes=1, where
# the last "<in> -> <out>" line the renderer logged is unambiguously the tile that killed it.
set -euo pipefail

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------
grid_id=''
csv=''
ini='effective.ini'
processes=1
max_attempts=6
variant='depr'
log='pullauta.log'
failures='failures.tsv'

while [ $# -gt 0 ]; do
    case "$1" in
        --grid-id) grid_id="$2"; shift 2 ;;
        --csv) csv="$2"; shift 2 ;;
        --ini) ini="$2"; shift 2 ;;
        --processes) processes="$2"; shift 2 ;;
        --max-attempts) max_attempts="$2"; shift 2 ;;
        --variant) variant="$2"; shift 2 ;;
        --log) log="$2"; shift 2 ;;
        --failures) failures="$2"; shift 2 ;;
        -h | --help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
        *) echo "run_pullauta.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$csv" ] || { echo 'run_pullauta.sh: --csv is required' >&2; exit 2; }
[ -n "$grid_id" ] || grid_id='(unnamed grid)'

# Provenance for the failure report. Deliberately *not* obtained by probing the binary: with
# batch=1 in pullauta.ini and no arguments, `pullauta` starts rendering the whole grid, and with no
# ini present it silently writes a default one (Config::load_or_create_default) that would then be
# overwritten. The git sha is baked into the image; the version and the ISA variant both appear in
# the renderer's own first lines of output, so they are picked up from the real run's log below.
readonly PULLAUTA_GIT_SHA="$(cat /opt/karttapullautin/GIT_SHA 2> /dev/null || echo unknown)"
pullauta_version='unknown'
isa_variant='unknown'

# ---------------------------------------------------------------------------
# tile bookkeeping
# ---------------------------------------------------------------------------
# Stems, i.e. tile names without the .laz suffix, because that is what karttapullautin names its
# outputs after.
mapfile -t core_stems < <(awk -F, 'NR > 1 && $5 == "core" { sub(/\.[^.]*$/, "", $1); print $1 }' "$csv")
mapfile -t halo_stems < <(awk -F, 'NR > 1 && $5 == "halo" { sub(/\.[^.]*$/, "", $1); print $1 }' "$csv")
[ "${#core_stems[@]}" -gt 0 ] || { echo "run_pullauta.sh: ${csv} lists no core tiles" >&2; exit 2; }

mkdir -p out

# Field lookup for the failure report, so a bug report carries the tile's provenance.
tile_field() {
    awk -F, -v stem="$1" -v col="$2" '
        NR == 1 { for (i = 1; i <= NF; i++) hdr[$i] = i; next }
        { name = $1; sub(/\.[^.]*$/, "", name) }
        name == stem { print $(hdr[col]); exit }
    ' "$csv"
}

# A PNG that ends in an IEND chunk was closed properly. This is the check that distinguishes "this
# tile is done" from "the process was killed while writing this tile", which matters because
# pullauta's own resume logic only tests for the file's *existence*.
png_is_complete() {
    [ -s "$1" ] || return 1
    [ "$(tail -c 8 "$1" | od -An -tx1 | tr -d ' \n')" = '49454e44ae426082' ]
}

# pullauta writes <t>.png, <t>.pgw, <t>_depr.png, <t>_depr.pgw in that order, so the last of the
# four is the completion sentinel -- but check all of them, since a partial quartet must be redone.
tile_is_complete() {
    local stem="$1"
    local f
    for f in "out/${stem}.pgw" "out/${stem}_depr.pgw"; do
        [ -s "$f" ] || return 1
    done
    for f in "out/${stem}.png" "out/${stem}_depr.png"; do
        png_is_complete "$f" || return 1
    done
    return 0
}

declare -A blacklisted=()

# Remove a half-written quartet so pullauta's resume logic picks the tile up again. Doing nothing
# here would be the worst outcome: <t>.png exists, so the tile would be skipped forever and ship as
# a corrupt image.
quarantine_incomplete() {
    local stem quarantined=0
    for stem in "${core_stems[@]}"; do
        [ -n "${blacklisted[$stem]:-}" ] && continue
        # Nothing written at all is not damage, just not-done-yet.
        if [ -e "out/${stem}.png" ] || [ -e "out/${stem}_depr.png" ]; then
            if ! tile_is_complete "$stem"; then
                rm -f "out/${stem}".png "out/${stem}".pgw "out/${stem}_depr".png "out/${stem}_depr".pgw
                quarantined=$((quarantined + 1))
            fi
        fi
    done
    [ "$quarantined" -eq 0 ] || printf 'run_pullauta.sh: quarantined %s half-written tile(s)\n' "$quarantined" >&2
}

completed_count() {
    local stem n=0
    for stem in "${core_stems[@]}"; do
        [ -n "${blacklisted[$stem]:-}" ] && continue
        tile_is_complete "$stem" && n=$((n + 1))
    done
    echo "$n"
}

pending_stems() {
    local stem
    for stem in "${core_stems[@]}"; do
        [ -n "${blacklisted[$stem]:-}" ] && continue
        tile_is_complete "$stem" || echo "$stem"
    done
}

# Everything an upstream bug report needs: the exact file, where it is, how to fetch it, which
# build produced the crash, and the panic itself. Reporting "a tile failed" without these would
# make the requirement to collect them pointless.
printf 'tile\trole\tgrid_id\tcrs\tmin_x\tmin_y\tmax_x\tmax_y\tsize_bytes\turl\tsha256\tpullauta_version\tpullauta_git_sha\tisa_variant\texit_code\treason\tpanic_message\tlog_tail\n' > "$failures"

record_failure() {
    local stem="$1" exit_code="$2" reason="$3" panic="$4" tail_text="$5"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$stem" core "$grid_id" \
        "$(tile_field "$stem" crs)" \
        "$(tile_field "$stem" min_x)" "$(tile_field "$stem" min_y)" \
        "$(tile_field "$stem" max_x)" "$(tile_field "$stem" max_y)" \
        "$(tile_field "$stem" size_bytes)" \
        "$(tile_field "$stem" url)" "$(tile_field "$stem" sha256)" \
        "$pullauta_version" "$PULLAUTA_GIT_SHA" "$isa_variant" "$exit_code" "$reason" \
        "$(printf '%s' "$panic" | tr '\t\n' '  ')" \
        "$(printf '%s' "$tail_text" | tr '\t\n' '  ')" \
        >> "$failures"
}

# Skip a tile from now on, keeping its points available to its neighbours.
blacklist() {
    local stem="$1"
    blacklisted[$stem]=1
    rm -f "out/${stem}".png "out/${stem}".pgw "out/${stem}_depr".png "out/${stem}_depr".pgw
    : > "out/${stem}.png"
}

# ---------------------------------------------------------------------------
# tiles whose laz never arrived
# ---------------------------------------------------------------------------
# fetch_laz.sh has already decided these are permanently unavailable (it would have failed the task
# otherwise). Blacklisting them up front stops the ladder below spending an attempt per tile
# rediscovering that they cannot be rendered.
for stem in "${core_stems[@]}"; do
    if ! compgen -G "in/${stem}.la[sz]" > /dev/null; then
        printf 'run_pullauta.sh: %s was never downloaded; recording it as a hole\n' "$stem" >&2
        record_failure "$stem" '' 'laz file unavailable (see download_failures.tsv)' '' ''
        blacklist "$stem"
    fi
done

# Halo tiles are here only for their points. The placeholder is what stops pullauta rendering them,
# which is what makes grid_size a pure download trade-off instead of also wasting compute:
# at grid_size 10 the ring is 36% of the files in the folder.
halo_placeholders=0
for stem in "${halo_stems[@]}"; do
    if [ ! -e "out/${stem}.png" ]; then
        : > "out/${stem}.png"
        halo_placeholders=$((halo_placeholders + 1))
    fi
done
printf 'run_pullauta.sh: %s core tiles, %s halo tiles (%s placeholders so they are not rendered)\n' \
    "${#core_stems[@]}" "${#halo_stems[@]}" "$halo_placeholders" >&2

# ---------------------------------------------------------------------------
# the attempt ladder
# ---------------------------------------------------------------------------
# pullauta reads pullauta.ini from the current directory and there is no flag to point it elsewhere,
# so make a real, writable copy: the staged input is a symlink to a file we must not edit, and
# `processes` has to change between attempts.
cp -f "$(readlink -f "$ini")" pullauta.ini
chmod u+w pullauta.ini

set_processes() {
    # [[:blank:]] rather than [ \t]: inside a bracket expression sed does not expand \t, so the
    # latter would quietly mean "space, backslash or the letter t" and fail on a tab-indented key.
    sed -i -E "s/^([[:blank:]]*processes[[:blank:]]*)=.*/\1= $1/" pullauta.ini
    # rust-ini takes the first occurrence of a key, so confirm what pullauta will actually read
    # rather than trusting the substitution.
    local effective
    effective="$(grep -m1 -E '^[[:blank:]]*processes[[:blank:]]*=' pullauta.ini | sed -E 's/.*=[[:blank:]]*//')"
    [ "$effective" = "$1" ] || {
        echo "run_pullauta.sh: failed to set processes=$1 (effective value: ${effective:-none})" >&2
        exit 1
    }
}

: > "$log"
done_before=0
made_progress=1
attempt=0
exit_code=0

while [ "$attempt" -lt "$max_attempts" ]; do
    mapfile -t pending < <(pending_stems)
    [ "${#pending[@]}" -gt 0 ] || break
    attempt=$((attempt + 1))

    # Serialise only when the previous attempt achieved nothing: that is the situation where a
    # crash has to be attributed to a specific tile, and pullauta caps its own thread count at the
    # number of remaining files anyway.
    if [ "$made_progress" -eq 1 ]; then
        this_processes="$processes"
    else
        this_processes=1
    fi
    set_processes "$this_processes"

    printf '=== attempt %s/%s: %s tile(s) pending, processes=%s ===\n' \
        "$attempt" "$max_attempts" "${#pending[@]}" "$this_processes" | tee -a "$log" >&2

    attempt_log="$(mktemp)"
    exit_code=0
    pullauta > "$attempt_log" 2>&1 || exit_code=$?
    cat "$attempt_log" >> "$log"

    # Both of these are printed at the top of every run -- the ISA line by the dispatch wrapper,
    # the version by pullauta itself -- so read them here rather than probing the binary.
    if [ "$isa_variant" = unknown ]; then
        isa_variant="$(sed -n 's/^pullauta: ISA variant \([a-z0-9]*\).*/\1/p' "$attempt_log" | head -1)"
        : "${isa_variant:=unknown}"
    fi
    if [ "$pullauta_version" = unknown ]; then
        pullauta_version="$(sed -n 's/^Karttapullautin v\(.*\)$/\1/p' "$attempt_log" | head -1)"
        : "${pullauta_version:=unknown}"
    fi

    quarantine_incomplete
    done_now="$(completed_count)"
    made_progress=$([ "$done_now" -gt "$done_before" ] && echo 1 || echo 0)

    printf 'run_pullauta.sh: attempt %s exit=%s, %s/%s core tiles done\n' \
        "$attempt" "$exit_code" "$done_now" "${#core_stems[@]}" >&2

    if [ "$exit_code" -eq 0 ] && [ "$done_now" -eq "$((${#core_stems[@]} - ${#blacklisted[@]}))" ]; then
        rm -f "$attempt_log"
        done_before="$done_now"
        break
    fi

    if [ "$made_progress" -eq 0 ]; then
        # Did the renderer even get as far as a tile? If not, this is a configuration or
        # environment failure -- a missing shapefile zip, an unreadable ini, a full disk -- and
        # blaming a tile for it would hide a real problem and corrupt the bug report.
        last_tile="$(grep -oE '[^ ]+\.la[sz] -> ' "$attempt_log" | tail -1 | sed -E 's/ -> $//' || true)"
        if [ -z "$last_tile" ]; then
            printf 'run_pullauta.sh: pullauta failed without starting a tile; not a per-tile bug\n' >&2
            tail -40 "$attempt_log" >&2
            rm -f "$attempt_log"
            exit "${exit_code:-1}"
        fi
        if [ "$this_processes" -eq 1 ]; then
            stem="$(basename "$last_tile")"
            stem="${stem%.*}"
            panic="$(grep -E "panicked at|^Error|^thread " "$attempt_log" | tail -3 || true)"
            printf 'run_pullauta.sh: blacklisting %s (exit %s): %s\n' "$stem" "$exit_code" "${panic:-no panic message}" >&2
            record_failure "$stem" "$exit_code" 'karttapullautin aborted while rendering this tile' \
                "$panic" "$(tail -20 "$attempt_log")"
            blacklist "$stem"
            made_progress=1  # the blacklist *is* progress; go back to full parallelism
        fi
    fi
    rm -f "$attempt_log"
    done_before="$done_now"
done

# ---------------------------------------------------------------------------
# report and prune
# ---------------------------------------------------------------------------
mapfile -t still_pending < <(pending_stems)
for stem in "${still_pending[@]}"; do
    printf 'run_pullauta.sh: giving up on %s after %s attempts\n' "$stem" "$max_attempts" >&2
    record_failure "$stem" "$exit_code" "still unrendered after ${max_attempts} attempts" '' "$(tail -20 "$log")"
done

# The placeholders were a means, not a result: remove them by name so a zero-byte file can never be
# mistaken for a rendered tile downstream.
for stem in "${halo_stems[@]}" "${!blacklisted[@]}"; do
    [ -s "out/${stem}.png" ] || rm -f "out/${stem}.png"
done

# Keep only the requested variant. `<t>.png` and `<t>_depr.png` are the same map with and without
# depression markings; carrying both would double the intermediate storage of a full run
# (~90 GB -> ~175 GB for Bavaria) for output nobody consumes.
case "$variant" in
    # -delete rather than a pipe into xargs, and the alternation parenthesised: `-name a -o -name b`
    # binds looser than the implicit -and, so without the parentheses the -type f applies to only
    # the first branch.
    depr) find out -maxdepth 1 -type f \( -name '*.png' -o -name '*.pgw' \) ! -name '*_depr.*' -delete ;;
    plain) find out -maxdepth 1 -type f \( -name '*_depr.png' -o -name '*_depr.pgw' \) -delete ;;
    both) ;;
    *) echo "run_pullauta.sh: unknown --variant: ${variant}" >&2; exit 2 ;;
esac
# Anything else pullauta may have dropped in out/ (<t>_basemap.dxf.bin and friends) is not part of
# the map and must not reach the publish step.
find out -maxdepth 1 -type f ! -name '*.png' ! -name '*.pgw' -delete

rendered="$(find out -maxdepth 1 -name '*.pgw' | wc -l)"
n_failed="$(($(wc -l < "$failures") - 1))"
printf 'run_pullauta.sh: %s finished: %s tile(s) rendered, %s recorded as failures\n' \
    "$grid_id" "$rendered" "$n_failed" >&2

# Nothing at all came out: that is not a bad tile, that is a broken grid, and it should be retried
# rather than silently leaving a hole the size of the whole batch.
if [ "$rendered" -eq 0 ]; then
    printf 'run_pullauta.sh: no tiles rendered at all; failing so this grid is retried\n' >&2
    tail -40 "$log" >&2
    exit 1
fi
exit 0
