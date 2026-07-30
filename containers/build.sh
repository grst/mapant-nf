#!/usr/bin/env bash
# Build the pipeline's container images locally.
#
# Usage:
#   containers/build.sh                  # build all images
#   containers/build.sh k2t gdal         # build a subset
#   REGISTRY=ghcr.io/you containers/build.sh --push
#   containers/build.sh --manifest       # print names/tags/build-args as JSON (used by CI)
#
# `-profile local_images` points the pipeline at the images produced here; conf/containers.config has
# the published ones.
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
readonly ALL_IMAGES=(karttapullautin gdal osmium k2t)

# The upstream karttapullautin release to build, read out of the Containerfile's own ARG default
# rather than repeated here: the version is part of the image's build inputs, and CI decides whether
# an image needs rebuilding from the hash of exactly those inputs. A copy in this script would be a
# way for the two to disagree -- bump the ARG and CI would rebuild with the old tag.
readonly PULLAUTA_TAG="${PULLAUTA_TAG:-$(
    sed -n 's/^ARG PULLAUTA_TAG=//p' "${SCRIPT_DIR}/karttapullautin/Containerfile"
)}"
[ -n "$PULLAUTA_TAG" ] || {
    echo 'FATAL: no ARG PULLAUTA_TAG in containers/karttapullautin/Containerfile' >&2
    exit 1
}

# Image name -> tag. The karttapullautin tag tracks the upstream release it is built from.
declare -rA TAGS=(
    [karttapullautin]="${PULLAUTA_TAG#v}"
    [gdal]='latest'
    [osmium]='latest'
    [k2t]='latest'
)

# Image name -> build args, as `--build-arg` KEY=VALUE pairs. Read by the build loop below and
# emitted by --manifest, so a local build and the CI build cannot be given different arguments.
declare -rA BUILD_ARGS=(
    [karttapullautin]="PULLAUTA_TAG=${PULLAUTA_TAG}"
)

# What CI needs to know about the images, as JSON: which exist, what to tag them, what to build them
# with. Emitted from here rather than duplicated in a workflow file so that adding an image or
# bumping a version is a one-file change.
print_manifest() {
    local name sep=''
    printf '['
    for name in "${ALL_IMAGES[@]}"; do
        printf '%s{"name":"%s","tag":"%s","build_args":"%s"}' \
            "$sep" "$name" "${TAGS[$name]:-latest}" "${BUILD_ARGS[$name]:-}"
        sep=','
    done
    printf ']\n'
}

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
        --manifest)
            print_manifest
            exit 0
            ;;
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
[ ${#images[@]} -gt 0 ] || images=("${ALL_IMAGES[@]}")

readonly ENGINE="$(engine)"
echo "==> Using ${ENGINE}"

for name in "${images[@]}"; do
    [ -d "${SCRIPT_DIR}/${name}" ] || {
        echo "FATAL: no such image directory: containers/${name}" >&2
        exit 2
    }
    ref="${REGISTRY}/${name}:${TAGS[$name]:-latest}"
    echo "==> Building ${ref}"

    # karttapullautin compiles three ISA variants from a source checkout, so the upstream tag it is
    # built from is a build arg -- see containers/karttapullautin/Containerfile.
    args=()
    if [ -n "${BUILD_ARGS[$name]:-}" ]; then
        args+=(--build-arg "${BUILD_ARGS[$name]}")
    fi

    # Tagged twice: the version, and `latest`. `latest` is what nextflow.config's `local_images`
    # profile points at, so switching to locally built images needs no knowledge of which upstream
    # release they came from.
    tags=(-t "$ref")
    latest_ref="${REGISTRY}/${name}:latest"
    if [ "$ref" != "$latest_ref" ]; then
        tags+=(-t "$latest_ref")
    fi

    "$ENGINE" build "${args[@]}" "${tags[@]}" "${SCRIPT_DIR}/${name}"

    if [ "$push" -eq 1 ]; then
        for t in "${tags[@]}"; do
            [ "$t" != -t ] || continue
            echo "==> Pushing ${t}"
            "$ENGINE" push "$t"
        done
    fi
done

echo '==> Done. Images:'
"$ENGINE" images --filter "reference=${REGISTRY}/*" \
    --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}' 2> /dev/null \
    || "$ENGINE" images
