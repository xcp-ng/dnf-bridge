# dnf-bridge.bbclass
#
# Class for a recipe taking prebuilt RPMs.  Expects the recipes to
# point to the SRPM and RPMs of a given package in their SRC_URI, and
# makes them available for later use.  Provides do_build
# implementation that just copies the prebuilt RPMs.
#
# Recipes using this class should usually be generated using the
# gen-dnf-proxy script.

inherit rpm-base

RECIPE_DEPLOY_DIR = "${DEPLOY_DIR_RPMS}/${PN}"

do_build() {
    rm -rf "${WORKDIR}/RPMS" "${WORKDIR}/SRPMS"
    mkdir -p "${WORKDIR}/RPMS" "${WORKDIR}/SRPMS"
    cp -la "${UNPACKDIR}"/*.rpm "${WORKDIR}/RPMS/"
    mv "${WORKDIR}/RPMS/"*.src.rpm "${WORKDIR}/SRPMS/"
}
