# dnf-bridge.bbclass
#
# Expects the recipes to point to the SRPM and RPMs of a given package
# in their SRC_URI, and makes them available for later use.

inherit rpm-base

RECIPE_DEPLOY_DIR = "${DEPLOY_DIR_RPMS}/${PN}"

do_build() {
    rm -rf "${WORKDIR}/RPMS" "${WORKDIR}/SRPMS"
    mkdir -p "${WORKDIR}/RPMS" "${WORKDIR}/SRPMS"
    cp -la "${UNPACKDIR}"/*.rpm "${WORKDIR}/RPMS/"
    mv "${WORKDIR}/RPMS/"*.src.rpm "${WORKDIR}/SRPMS/"
}
