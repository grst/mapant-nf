#!/usr/bin/env bash
# Build the pipeline's container images locally.
#
# Usage:
#   containers/build.sh                  # build all images
#   containers/build.sh k2t gdal         # build a subset
#   REGISTRY=ghcr.io/you containers/build.sh --push
#
# The image names and tags produced here are the defaults in conf/containers.config; override
# `params.container_*` there (or on the command line) to point the pipeline somewhere else.
#
# ---------------------------------------------------------------------------
# Why the Containerfiles look the way they do
# ---------------------------------------------------------------------------
# Two constraints shape every image in this directory. Both come from the devcontainer this
# pipeline is developed in -- a rootless podman container nested inside another one -- and both are
# easy to trip over because neither failure names its own cause.
#
# 1. Every image must contain `bash` AND `ps`.
#
#    Nextflow runs its task wrapper under bash and shells out to `ps` to collect task metrics, so a
#    minimal image fails with
#
#        Command 'ps' required by nextflow to collect task metrics cannot be found
#
#    which is why every Containerfile here installs procps and none of them use a distroless or
#    -alpine base. This is not devcontainer-specific: it bites on any executor.
#
# 2. Container UID 0 is the only UID that exists, so apt must stay root.
#
#    apt normally drops privileges to the _apt user (UID 42) to download packages. Here
#    CAP_SYS_ADMIN is absent from the bounding set, so podman cannot map a UID range into the
#    container (see scripts/setup-container-runtime.sh), and apt's setgroups/seteuid calls fail:
#
#        E: setgroups 65534 failed - setgroups (1: Operation not permitted)
#        E: seteuid 42 failed - seteuid (22: Invalid argument)
#        E: Method http has died unexpectedly!
#
#    `-o APT::Sandbox::User=root` tells apt to stay root instead, which is safe here: the build
#    already runs unprivileged inside a user namespace, so apt's own sandbox is defence in depth we
#    cannot use rather than a boundary we are removing.
#
#    The same limitation applies to anything else in a build that changes UID -- useradd plus su,
#    gosu, npm dropping privileges -- and to a `USER nonroot` directive, which would make the
#    resulting image unable to *start*. Keep every image root-only. It also means CAP_MKNOD is
#    absent and /dev/fuse cannot be created, so a RUN step calling mknod always fails.
#
# Neither constraint costs anything on a normal workstation or an HPC cluster, so the images stay
# portable. README.md has the full picture.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REGISTRY="${REGISTRY:-mapant}"

# Image name -> tag. The karttapullautin tag tracks the upstream release it is built from, which is
# also what PULLAUTA_TAG passes to the build; keep the two in step.
readonly PULLAUTA_TAG="${PULLAUTA_TAG:-v2.13.0}"
declare -rA TAGS=(
    [karttapullautin]="${PULLAUTA_TAG#v}"
    [gdal]='latest'
    [osmium]='latest'
    [k2t]='latest'
)

# podman here, docker if that is what is available: the images are ordinary OCI images and neither
# runtime is special. `podman build` is used in the devcontainer because there is no docker daemon.
engine() {
    if command -v podman > /dev/null 2>&1; then
        echo podman
    elif command -v docker > /dev/null 2>&1; then
        echo docker
    else
        echo 'FATAL: neither podman nor docker found' >&2
        exit 1
    fi
}

push=0
images=()
for arg in "$@"; do
    case "$arg" in
        --push) push=1 ;;
        -h | --help)
            sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
        -*)
            echo "FATAL: unknown option: $arg" >&2
            exit 2
            ;;
        *) images+=("$arg") ;;
    esac
done
[ ${#images[@]} -gt 0 ] || images=(karttapullautin gdal osmium k2t)

readonly ENGINE="$(engine)"
echo "==> Using ${ENGINE}"

for name in "${images[@]}"; do
    [ -d "${SCRIPT_DIR}/${name}" ] || {
        echo "FATAL: no such image directory: containers/${name}" >&2
        exit 2
    }
    ref="${REGISTRY}/${name}:${TAGS[$name]:-latest}"
    echo "==> Building ${ref}"

    args=()
    # karttapullautin compiles three ISA variants from source, so the upstream tag is a build arg
    # rather than baked into the Containerfile -- see containers/karttapullautin/Containerfile.
    [ "$name" = karttapullautin ] && args+=(--build-arg "PULLAUTA_TAG=${PULLAUTA_TAG}")

    "$ENGINE" build "${args[@]}" -t "$ref" "${SCRIPT_DIR}/${name}"

    if [ "$push" -eq 1 ]; then
        echo "==> Pushing ${ref}"
        "$ENGINE" push "$ref"
    fi
done

echo '==> Done. Images:'
"$ENGINE" images --filter "reference=${REGISTRY}/*" \
    --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}' 2> /dev/null \
    || "$ENGINE" images
