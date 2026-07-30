#!/usr/bin/env bash
# Set up a container runtime and Nextflow *inside* this devcontainer, so pipeline
# processes can run in their own containers.
#
# Safe to re-run at any time: every step is a no-op once applied.
#
# ---------------------------------------------------------------------------
# Why this script exists, and why it is not just `apt-get install podman`
# ---------------------------------------------------------------------------
# This devcontainer is itself a rootless podman container (--userns=keep-id, most
# capabilities dropped). A pipeline that supplies its dependencies as containers
# therefore has to nest a runtime inside it. Mounting the host's podman socket is not
# an alternative: devcontainer-isolation fails the container on every start if a
# runtime socket is present, on purpose -- a socket is a container escape.
#
# Nesting works, but three of podman's defaults cannot work here. Each was found by
# testing, and each fails with an error that does not name its own cause, so they are
# documented next to the setting that fixes them. In short:
#
#   1. Only ONE UID is available inside pipeline containers, not the usual ~65k.
#      Everything else about this file follows from that. See configure_ids().
#   2. A fresh procfs cannot be mounted, so the outer /proc is bind-mounted in.
#   3. A UTS namespace cannot be created, so containers share the outer hostname.
#
# The root cause of all three is a single missing capability: CAP_SYS_ADMIN is not in
# this container's bounding set, so no process in here can obtain it -- not even via
# sudo, and not via a setuid-root binary, since file capabilities are masked by the
# bounding set. Creating a *new* user namespace does grant full capabilities inside
# it, which is why podman can start containers at all; what it cannot do is any
# operation that requires privilege over the *outer* namespace.
#
# Two consequences are worth knowing before debugging something that cannot work:
#
#   * An image whose processes switch to a non-root UID cannot run. Container UID 0
#     is the only UID that exists. Most bioinformatics images run as root and are
#     fine; `USER nonroot` images are not.
#   * CAP_MKNOD is also absent and /dev/fuse does not exist and cannot be created, so
#     a RUN step calling mknod always fails.
#
# Apptainer/Singularity is not an option here for the same underlying reason: without
# /dev/fuse it cannot mount a SIF unprivileged, and its setuid mode needs
# CAP_SYS_ADMIN. Neither can be obtained from inside this container.
#
# If you control how this devcontainer is started, adding
#   "--cap-add=SYS_ADMIN", "--security-opt=unmask=ALL"
# to runArgs in devcontainer.json removes all three limitations -- re-run this script
# afterwards and it will detect the change and configure the full mapping. That is a
# real widening of the sandbox, though, so it is deliberately not done by default;
# see README.md.
set -euo pipefail

readonly CONF_DIR="${HOME}/.config/containers"
readonly GRAPHROOT="${HOME}/.cache/containers/storage"
readonly NXF_HOME_DIR="${HOME}/.cache/nextflow"
readonly MARKER='# managed by scripts/setup-container-runtime.sh -- edits will be overwritten'

# Set by configure_ids(), consumed by configure_storage() and verify().
MULTI_UID=0

log() { printf '%s\n' "$*"; }

# write_if_changed <path>, content on stdin. Returns 1 when the file was already
# correct, which keeps a re-run quiet and avoids rewriting a file podman is reading.
write_if_changed() {
    local path="$1" tmp
    tmp="$(mktemp)"
    cat > "$tmp"
    if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
        rm -f "$tmp"
        return 1
    fi
    mkdir -p "$(dirname "$path")"
    mv "$tmp" "$path"
    chmod 0644 "$path"
    return 0
}

# ---------------------------------------------------------------------------
# 1. Packages
# ---------------------------------------------------------------------------
# fuse-overlayfs and slirp4netns are deliberately absent. They are what podman would
# normally reach for and both are useless here: there is no /dev/fuse and no
# /dev/net/tun, and neither device can be created without CAP_MKNOD. Installing them
# would only give podman a broken option to auto-detect.
#
# uidmap is installed even though the multi-UID path it enables cannot currently
# work, because configure_ids() probes for that path rather than assuming, and the
# probe needs newuidmap present to give a meaningful answer.
install_packages() {
    local pkgs=(podman uidmap crun openjdk-21-jre-headless)
    local missing=() p
    for p in "${pkgs[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
    done

    if [ ${#missing[@]} -eq 0 ]; then
        log '==> Packages already installed'
        return
    fi
    log "==> Installing: ${missing[*]}"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
}

# ---------------------------------------------------------------------------
# 2. UID mapping -- the setting everything else follows from
# ---------------------------------------------------------------------------
# Rootless podman normally gives a container ~65k UIDs by having the setuid-root
# helper newuidmap write a multi-range uid_map for the container's user namespace.
# That cannot work here, and not for the reason it first appears to.
#
# The distro default /etc/subuid range (100000..165535) is indeed wrong -- those UIDs
# do not exist inside this container's namespace, whose map is fragmented into three
# extents (0-999, 1000, 1001-65536), and the kernel requires each requested range to
# fall inside a single parent extent. Computing the correct range is what
# pick_subid_range() does, and with it newuidmap asks for something legitimate.
#
# It still fails, because writing *another process's* uid_map requires CAP_SYS_ADMIN
# over the target namespace (file_ns_capable() in the kernel's map_write), and
# CAP_SYS_ADMIN is not in this container's bounding set, so newuidmap cannot acquire
# it even as setuid-root. Writing one's *own* uid_map does work -- creating a
# namespace grants full capabilities within it -- which is exactly the single-UID path
# podman falls back to, and why containers run at all.
#
# So: probe, don't assume. If newuidmap works (e.g. because the outer container was
# given --cap-add=SYS_ADMIN), keep the computed range and get the full mapping. If it
# does not, clear the range so podman goes straight to its single-UID path instead of
# failing, and record that storage needs ignore_chown_errors.
pick_subid_range() {
    local map="$1" myid="$2"
    awk -v myid="$myid" '
        { start = $1 + 0; count = $3 + 0; end = start + count - 1 }
        myid >= start && myid <= end { next }        # never hand out our own ID
        count > best { best = count; bstart = start }
        END { if (best > 0) print bstart, best }
    ' "$map"
}

# Does newuidmap actually work? Writes a throwaway namespace's map and checks.
probe_newuidmap() {
    local start="$1" count="$2" pid rc=1
    unshare --user sleep 5 &
    pid=$!
    sleep 0.4
    newuidmap "$pid" 0 "$(id -u)" 1 1 "$start" "$count" >/dev/null 2>&1 && rc=0
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    return $rc
}

# set_subid_line <file> <user> [start:count] -- replaces just this user's line, leaving
# any other user's allocation alone. Omit the range to remove the line.
set_subid_line() {
    local file="$1" user="$2" range="${3:-}" tmp
    tmp="$(mktemp)"
    grep -v "^${user}:" "$file" 2>/dev/null > "$tmp" || true
    [ -n "$range" ] && printf '%s:%s\n' "$user" "$range" >> "$tmp"
    sudo install -m 0644 -o root -g root "$tmp" "$file"
    rm -f "$tmp"
}

configure_ids() {
    local user; user="$(id -un)"
    local range start count
    range="$(pick_subid_range /proc/self/uid_map "$(id -u)")"

    if [ -n "$range" ]; then
        read -r start count <<< "$range"
        set_subid_line /etc/subuid "$user" "${start}:${count}"
        set_subid_line /etc/subgid "$user" "${start}:${count}"

        if probe_newuidmap "$start" "$count"; then
            MULTI_UID=1
            log "==> Multi-UID mapping available: container UIDs 1..${count} -> ${start}..$((start + count - 1))"
            return
        fi
        # Leaving a range in place that newuidmap cannot apply is worse than having
        # none: podman would fail outright instead of falling back to single-UID mode.
        set_subid_line /etc/subuid "$user"
        set_subid_line /etc/subgid "$user"
    fi

    MULTI_UID=0
    log '==> Single-UID mode (CAP_SYS_ADMIN unavailable, so newuidmap cannot map a range)'
    log '    Container UID 0 is the only UID; images that switch to a non-root user will not run'
}

# ---------------------------------------------------------------------------
# 3. Storage
# ---------------------------------------------------------------------------
# graphroot must be on ext4. Podman's default (~/.local/share/containers) sits on the
# overlay filesystem this container's root is made of, and overlayfs cannot stack on
# itself, so podman would silently fall back to the vfs driver and copy every layer of
# every image instead of sharing them.
#
# ~/.cache is a named volume, so images also survive a container rebuild. Its one
# cost: it is mounted nosuid, so a setuid binary baked into an image will not elevate.
# Bioinformatics tooling essentially never relies on that.
#
# mount_program is empty on purpose. Left unset, podman looks for fuse-overlayfs and
# prefers it if installed; empty forces the kernel's native rootless overlay, the only
# thing that can work without /dev/fuse.
#
# ignore_chown_errors is required in single-UID mode and harmful otherwise. Image
# layers routinely contain files owned by other UIDs (alpine's /etc/shadow is group
# 42), and unpacking them calls lchown, which fails when only UID 0 exists. Without
# this, every `podman pull` of a normal image fails. With it, those files end up owned
# by container root -- acceptable, and the reason this is not set when a real range is
# available.
configure_storage() {
    mkdir -p "$GRAPHROOT" "/tmp/containers-run-$(id -u)"
    local chown_opt=''
    [ "$MULTI_UID" -eq 1 ] || chown_opt=$'\nignore_chown_errors = "true"'

    if write_if_changed "${CONF_DIR}/storage.conf" <<EOF
${MARKER}
[storage]
driver = "overlay"
graphroot = "${GRAPHROOT}"
runroot = "/tmp/containers-run-$(id -u)"

[storage.options.overlay]
mount_program = ""${chown_opt}
EOF
    then log '==> Wrote storage.conf (overlay driver, graphroot on the ~/.cache volume)'
    else log '==> storage.conf already current'
    fi
}

# ---------------------------------------------------------------------------
# 4. Engine
# ---------------------------------------------------------------------------
# These three settings are what make `podman run` work with no flags, which matters:
# they are properties of this machine, not of the pipeline, so keeping them here
# leaves nextflow.config portable to a normal workstation or HPC.
#
# netns = "host"
#   There is no /dev/net/tun, so slirp4netns and pasta cannot open a tunnel and the
#   default rootless network mode fails outright.
#
# utsns = "host"
#   Creating a UTS namespace is permitted, but crun then calls sethostname in it,
#   which needs CAP_SYS_ADMIN over that namespace. Sharing the outer UTS namespace
#   means crun never makes the call. Cost: containers see this container's hostname.
#
# volumes = ["/proc:/proc"]
#   A fresh procfs cannot be mounted here at all -- not by podman, and not by hand in
#   a bare nested namespace. The kernel's mount_too_revealing() check rejects a new
#   procfs when an existing one has *locked* submounts that the new mount would
#   reveal, and the outer runtime masks a dozen paths under /proc (/proc/sys,
#   /proc/kcore, /proc/acpi, ...). Those masks cannot be unmounted from in here, so
#   the only way to give a container a /proc is to bind-mount the existing one.
#
# pidns = "host"
#   Forced by the line above, and easy to miss. A bind-mounted /proc combined with a
#   private PID namespace is incoherent: the container's processes have PIDs that do
#   not appear in the /proc it can see, so anything that looks itself up there breaks.
#   procps fails outright ("ps: fatal library error, lookup self"), which also means
#   Nextflow's task metrics silently collect nothing. Sharing the PID namespace makes
#   /proc consistent again. Cost: containers see, and can signal, the outer process
#   table -- acceptable inside a sandbox that is already one trust domain.
#
# cgroupfs + file
#   No systemd user session, so the systemd cgroup manager has no bus and journald no
#   socket. Note also that cgroup v2 is not delegated (cgroup.subtree_control is not
#   writable), so Nextflow's cpus/memory directives are scheduling hints only --
#   nothing enforces them inside a task container.
configure_engine() {
    if write_if_changed "${CONF_DIR}/containers.conf" <<EOF
${MARKER}
[containers]
netns = "host"
utsns = "host"
pidns = "host"
volumes = ["/proc:/proc"]

[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
runtime = "crun"
EOF
    then log '==> Wrote containers.conf (host net/uts/pid ns, /proc bind, cgroupfs, crun)'
    else log '==> containers.conf already current'
    fi
}

# ---------------------------------------------------------------------------
# 5. Nextflow
# ---------------------------------------------------------------------------
# /usr/local/bin rather than ~/.local/bin: neither survives a rebuild, but the former
# is on PATH for non-interactive shells too, which is what a postCreateCommand-driven
# setup needs.
#
# NXF_HOME points into ~/.cache so plugins and remote pipeline assets land in the
# named volume instead of being re-fetched after every rebuild.
install_nextflow() {
    if command -v nextflow >/dev/null 2>&1; then
        log "==> Nextflow already installed ($(NXF_HOME="$NXF_HOME_DIR" nextflow -v 2>/dev/null || echo '?'))"
    else
        log '==> Installing Nextflow'
        # What get.nextflow.io serves *is* the nextflow launcher, so it is installed
        # directly rather than run as an installer. It only self-installs when piped
        # into bash from stdin; saved to a file and executed it just prints its help
        # and exits 0, leaving nothing behind. Piping it into bash instead trips
        # `set -o pipefail`, because it stops reading and curl reports a write error.
        # The launcher fetches its own JARs into NXF_HOME on first run.
        local tmp; tmp="$(mktemp -d)"
        curl -fsSL -o "${tmp}/nextflow" https://get.nextflow.io
        sudo install -m 0755 "${tmp}/nextflow" /usr/local/bin/nextflow
        rm -rf "$tmp"
        mkdir -p "$NXF_HOME_DIR"
        log "    $(NXF_HOME="$NXF_HOME_DIR" nextflow -v 2>&1 | tail -1)"
    fi

    mkdir -p "$NXF_HOME_DIR"
    local rc
    for rc in "${HOME}/.bashrc" "${HOME}/.zshrc"; do
        [ -f "$rc" ] || continue
        grep -q 'NXF_HOME' "$rc" || {
            printf '\n# Keep Nextflow plugins and assets on the persisted cache volume\nexport NXF_HOME="%s"\n' \
                "$NXF_HOME_DIR" >> "$rc"
            log "    Added NXF_HOME to $(basename "$rc")"
        }
    done
}

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------
# `podman info` succeeding proves little, so this runs an actual container. A vfs
# driver here means the overlay probe failed and every image operation will be slow --
# better reported now than discovered as an unexplained delay mid-pipeline.
verify() {
    log '==> Verifying'
    local driver
    driver="$(podman info --format '{{.Store.GraphDriverName}}' 2>/dev/null || echo unknown)"
    log "    storage driver: ${driver}"
    [ "$driver" = 'overlay' ] || log '    WARNING: expected "overlay"; image handling will be slow'

    if podman run --rm docker.io/library/alpine:3 true 2>/dev/null; then
        log '    podman run: OK'
    else
        log '    podman run: FAILED -- run `podman run --rm docker.io/library/alpine:3 true` to see why'
        return 1
    fi
    [ "$MULTI_UID" -eq 1 ] || log '    note: single-UID mode -- see the header of this script for what that rules out'
}

install_packages
configure_ids
configure_storage
configure_engine
install_nextflow
verify
log '==> Ready. Try: nextflow run main.nf -profile podman'
