PV = "8.99"

SRC_URI = " \
        file://Containerfile \
        file://build-rpm.sh \
        file://bindmount.repo \
        "

# For debugging of typos in repo files, patch from
# https://github.com/rpm-software-management/dnf/pull/2353 appplied to
# dnf-4.20.0-14.el10_0.alma.1.  Uncomment together with the 2 matching
# COPY statements in Containerfile.
#SRC_URI += "file://read.py"

S = "${UNPACKDIR}"

inherit rpm-builddeps

DEPENDS = " \
    rpm/bash \
    rpm/coreutils \
    rpm/curl \
    rpm/glibc-minimal-langpack \
    rpm/libcurl \
    rpm/libusbx \
    rpm/dnf \
    rpm/rootfiles \
    \
    rpm/dnf-plugins-core \
    rpm/rpm-build \
    rpm/fonts-srpm-macros \
    rpm/forge-srpm-macros \
    rpm/go-srpm-macros \
    rpm/pyproject-srpm-macros \
    rpm/python-srpm-macros \
    rpm/util-linux \
"

# Indirect dependencies that had to be removed in upstream-tuning.conf
# because of dependency loops, but which are necessary for dnf/rpm to
# be able to install the packages above
DEPENDS:append = " \
     rpm/rpm \
"

CONTAINERDIR = "${WORKDIR}/container"
CONTAINERIDFILE = "${CONTAINERDIR}/id"

# Create a container image, get the unique image id to refer to, and
# archive the image in shared state to be able to quickly restore it
# should it be externally purged from podman's datastore.  Set a tag
# on it to lower the odds, protecting it from a `podman image prune`.
# This MUST not use the layer cache, or newer packages in the bind-mounted
# repo will be ignored and a stale image id will be returned.
# FIXME: same tag for all projects and all archs.
addtask container_image after do_builddeps_repo before do_build
do_container_image() {
    rm -rf "${CONTAINERDIR}"
    mkdir -p "${CONTAINERDIR}"
    podman build \
        --no-cache \
        --security-opt label=disable \
        --platform "${CONTAINER_ARCH}" \
        --ulimit nofile=1024 \
        --network=none \
        --volume "${BUILDDEPSDIR}:/local-repos/bdeps:ro" \
        --iidfile="${CONTAINERIDFILE}" \
        --tag "localhost/dnf-bridge-build-env:latest" \
        -f ${S}/Containerfile ${S}
    IMAGEID=$(cat "${CONTAINERIDFILE}") # "sha256:...."
    IMAGEID=${IMAGEID#sha256:}
    podman save --format=docker-archive \
        --output="${CONTAINERDIR}/$IMAGEID.tar" \
        "$IMAGEID"

    cp "${UNPACKDIR}/build-rpm.sh" "${CONTAINERDIR}/"
}
# FIXME we MUST not do that, but for some reason disabling network
# fails with "cannot re-exec process to join the existing user namespace"
# NOTE: to full avoid this we would also need to separate fetching of
# the helper Alma container
do_container_image[network] = "1"

SSTATETASKS += "do_container_image"
do_container_image[sstate-inputdirs] = "${CONTAINERDIR}"
do_container_image[sstate-outputdirs] = "${BUILDENV_DEPLOY_DIR}"

addtask do_container_image_setscene
python do_container_image_setscene () {
    sstate_setscene(d)
}
