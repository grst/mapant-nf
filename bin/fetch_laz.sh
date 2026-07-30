#!/usr/bin/env bash
# Acquire every laz file a grid needs into ./in, and prove each one arrived intact.
#
# Checksums are not optional here. A truncated or half-written laz does not make karttapullautin
# fail -- it renders whatever points it managed to read, so the damage surfaces as a plausible but
# wrong map tile. Verifying is the only way the pipeline can tell "this tile is finished" from
# "this tile is finished badly", so every file is checked on every attempt, including files taken
# from a local directory.
#
# Exit status is a deliberate contract, because Nextflow's retry logic depends on telling apart
# "this will never work" from "the network had a bad minute":
#
#   0   every core tile is present and verified, OR the only missing core tiles failed
#       permanently (404/410/403, or a checksum that never matches). Those are written to the
#       failures TSV and left as holes in the map -- retrying cannot help.
#   1   at least one tile failed transiently after all retries (timeout, 5xx, connection reset),
#       or the disk filled. Retrying the whole grid is the right response, so fail and let
#       Nextflow's errorStrategy handle it.
#
# A permanently missing *halo* tile is only a warning: the neighbouring renders lose some of their
# 127 m of context, which is a slightly worse border rather than a wrong map.
set -euo pipefail

readonly SELF="${BASH_SOURCE[0]}"

usage() {
    sed -n '2,25p' "$SELF" | sed 's/^# \?//'
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# worker: fetch and verify exactly one tile, then record its outcome
# ---------------------------------------------------------------------------
# Invoked by the driver through xargs, as `fetch_laz.sh --fetch-one ...`. Re-execing rather than
# exporting a shell function keeps the quoting sane and means each tile's failure cannot take the
# driver down with it.
fetch_one() {
    local tile="$1" url="$2" sha="$3" size="$4" role="$5"
    local dest="${OUTDIR}/${tile}"
    local status_file="${STATUSDIR}/${tile}.status"
    local attempt rc http_code reason

    verify() {
        local path="$1" actual_size actual_sha
        [ -f "$path" ] || return 1
        # -L, because a file taken from --local-dir is a symlink and GNU stat reports the *link*
        # otherwise: its size is the length of the target path, which fails the check on every
        # locally supplied file.
        actual_size="$(stat -Lc %s "$path")"
        # Size first: it is free, and it catches the common truncation case without hashing
        # hundreds of megabytes to find out.
        if [ "$actual_size" != "$size" ]; then
            printf '%s: size mismatch (expected %s, got %s)\n' "$tile" "$size" "$actual_size" >&2
            return 1
        fi
        actual_sha="$(sha256sum "$path" | cut -d' ' -f1)"
        if [ "$actual_sha" != "$sha" ]; then
            printf '%s: sha256 mismatch (expected %s, got %s)\n' "$tile" "$sha" "$actual_sha" >&2
            return 1
        fi
        return 0
    }

    # Already there and intact? That happens on a Nextflow retry of the same task.
    if verify "$dest" 2> /dev/null; then
        printf 'ok\tcached\n' > "$status_file"
        return 0
    fi
    rm -f "$dest"

    # A local copy short-circuits the network but not the verification: the fixture path in the
    # test profiles has to exercise the same checks the real one does.
    if [ -n "${LOCALDIR:-}" ] && [ -e "${LOCALDIR}/${tile}" ]; then
        # Symlink rather than copy: a grid is tens of gigabytes and the source is read-only for the
        # lifetime of the task. `rm -rf in/` in the caller's trap removes the links, not the source.
        ln -sfn "$(readlink -f "${LOCALDIR}/${tile}")" "$dest"
        if verify "$dest"; then
            printf 'ok\tlocal\n' > "$status_file"
            return 0
        fi
        rm -f "$dest"
        printf 'permanent\tlocal copy failed verification\n' > "$status_file"
        return 0
    fi

    # The schema permits a bare path as well as a URL; curl needs a scheme.
    case "$url" in
        *://*) ;;
        /* | ./*) url="file://$(readlink -f "$url" 2> /dev/null || echo "$url")" ;;
    esac

    for ((attempt = 1; attempt <= RETRIES; attempt++)); do
        rc=0
        http_code="$(
            curl --silent --show-error --location --fail \
                --connect-timeout 30 \
                --speed-limit 1024 --speed-time 120 \
                --user-agent "$USER_AGENT" \
                ${LIMIT_RATE:+--limit-rate "$LIMIT_RATE"} \
                --write-out '%{http_code}' \
                --output "${dest}.part" \
                "$url" 2>> "${STATUSDIR}/${tile}.curlerr"
        )" || rc=$?

        if [ "$rc" -eq 0 ]; then
            mv -f "${dest}.part" "$dest"
            if verify "$dest"; then
                printf 'ok\tdownloaded\n' > "$status_file"
                return 0
            fi
            # A verified-bad download is worth one more try -- a truncated transfer happens -- but
            # if the server keeps handing us the same wrong bytes, the CSV's checksum is stale and
            # no number of retries will fix it.
            rm -f "$dest"
            reason="checksum/size mismatch"
        else
            rm -f "${dest}.part"
            case "$http_code" in
                # curl --fail turns these into exit 22; they are settled answers, not bad luck.
                400 | 401 | 403 | 404 | 410 | 451)
                    printf 'permanent\tHTTP %s\n' "$http_code" > "$status_file"
                    return 0
                    ;;
            esac
            reason="curl exit ${rc}, HTTP ${http_code}"
        fi

        if [ "$attempt" -lt "$RETRIES" ]; then
            # Exponential backoff with jitter: a hundred workers retrying in lockstep is how a
            # transient blip becomes a sustained outage for everyone else too.
            sleep "$((attempt * attempt * 5 + RANDOM % 5))"
        fi
    done

    printf 'transient\t%s after %s attempts\n' "$reason" "$RETRIES" > "$status_file"
    return 0
}

# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
main() {
    local csv='' failures='' jobs=4
    OUTDIR='in'
    RETRIES=3
    LOCALDIR=''
    LIMIT_RATE=''
    USER_AGENT='mapant/1.0 (+https://github.com/grst/mapant) nextflow pipeline'

    while [ $# -gt 0 ]; do
        case "$1" in
            --csv) csv="$2"; shift 2 ;;
            --outdir) OUTDIR="$2"; shift 2 ;;
            --failures) failures="$2"; shift 2 ;;
            --jobs) jobs="$2"; shift 2 ;;
            --retries) RETRIES="$2"; shift 2 ;;
            --local-dir) LOCALDIR="$2"; shift 2 ;;
            --limit-rate) LIMIT_RATE="$2"; shift 2 ;;
            --user-agent) USER_AGENT="$2"; shift 2 ;;
            -h | --help) usage 0 ;;
            *) echo "fetch_laz.sh: unknown argument: $1" >&2; usage 2 ;;
        esac
    done
    [ -n "$csv" ] || { echo 'fetch_laz.sh: --csv is required' >&2; exit 2; }
    [ -n "$failures" ] || failures='download_failures.tsv'

    mkdir -p "$OUTDIR"
    STATUSDIR="$(mktemp -d "${TMPDIR:-/tmp}/fetchstatus.XXXXXX")"
    # shellcheck disable=SC2064  # expand STATUSDIR now, while it is still set
    trap "rm -rf '$STATUSDIR'" EXIT
    export OUTDIR STATUSDIR RETRIES LOCALDIR LIMIT_RATE USER_AGENT

    # Fail before spending an hour on a download that cannot fit. The estimate is the grid's own
    # laz bytes plus room for karttapullautin's temporaries, which are comparable in size.
    local need_bytes avail_bytes
    need_bytes="$(awk -F, 'NR > 1 { s += $4 } END { printf "%.0f", s * 1.6 }' "$csv")"
    avail_bytes="$(($(df -Pk . | awk 'NR == 2 { print $4 }') * 1024))"
    if [ "$avail_bytes" -lt "$need_bytes" ]; then
        printf 'fetch_laz.sh: not enough free space here: need ~%s GiB, have %s GiB\n' \
            "$((need_bytes / 1024 / 1024 / 1024))" "$((avail_bytes / 1024 / 1024 / 1024))" >&2
        printf '  Lower params.grid_size, or lower maxForks for PULLAUTA_GRID, or point workDir at a bigger volume.\n' >&2
        exit 1
    fi

    # tile,url,sha256,size_bytes,role,... -- NUL-delimited so a field can never be re-split.
    #
    # `printf "%s%c", $i, 0` rather than a "\0" in the format string: awk's printf treats the format
    # as a C string, so an embedded NUL truncates it and only the first field is ever emitted. xargs
    # then groups five *tiles* into one call and the worker sees a tile name where it expects a URL.
    tail -n +2 "$csv" \
        | awk -F, 'NF >= 5 { for (i = 1; i <= 5; i++) printf "%s%c", $i, 0 }' \
        | xargs -0 -r -n 5 -P "$jobs" "$SELF" --fetch-one

    # Collect the per-tile outcomes. Done in the driver so a worker crash cannot corrupt the
    # summary, and so the ordering in the report is deterministic.
    local n_ok=0 n_perm=0 n_transient=0 core_missing=0 halo_missing=0
    printf 'tile\trole\toutcome\tdetail\n' > "$failures"
    while IFS=, read -r tile _url _sha _size role _rest; do
        local state detail
        if [ -s "${STATUSDIR}/${tile}.status" ]; then
            IFS=$'\t' read -r state detail < "${STATUSDIR}/${tile}.status"
        else
            state='transient'
            detail='worker produced no status (killed?)'
        fi
        case "$state" in
            ok) n_ok=$((n_ok + 1)) ;;
            permanent)
                n_perm=$((n_perm + 1))
                printf '%s\t%s\t%s\t%s\n' "$tile" "$role" "$state" "$detail" >> "$failures"
                [ "$role" = core ] && core_missing=$((core_missing + 1)) || halo_missing=$((halo_missing + 1))
                ;;
            *)
                n_transient=$((n_transient + 1))
                printf '%s\t%s\t%s\t%s\n' "$tile" "$role" "$state" "$detail" >> "$failures"
                ;;
        esac
    done < <(tail -n +2 "$csv")

    printf 'fetch_laz.sh: %s verified, %s permanently unavailable, %s transient failures\n' \
        "$n_ok" "$n_perm" "$n_transient" >&2

    if [ "$n_transient" -gt 0 ]; then
        printf 'fetch_laz.sh: giving up so the grid can be retried; see %s\n' "$failures" >&2
        sed -n '2,$p' "$failures" >&2
        return 1
    fi
    if [ "$halo_missing" -gt 0 ]; then
        printf 'fetch_laz.sh: WARNING %s halo tile(s) unavailable; borders next to them lose some of their 127 m context\n' \
            "$halo_missing" >&2
    fi
    if [ "$core_missing" -gt 0 ]; then
        printf 'fetch_laz.sh: WARNING %s core tile(s) permanently unavailable; they will be holes in the map\n' \
            "$core_missing" >&2
    fi
    return 0
}

if [ "${1:-}" = --fetch-one ]; then
    shift
    fetch_one "$@"
else
    main "$@"
fi
