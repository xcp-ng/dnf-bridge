RDEPENDS ?= ""

PROVIDES = "${@ ' '.join(f"rpm/{p}" for p in d.getVar('PACKAGES').split())}"

DEPLOY_DIR_RPMS = "${DEPLOY_DIR}/rpms"

addtask deploy after do_build
do_deploy() {
    rm -rf "${RECIPE_DEPLOY_DIR}"
    mkdir -p "${RECIPE_DEPLOY_DIR}"
    cp -la "${WORKDIR}/SRPMS" "${WORKDIR}/RPMS" "${RECIPE_DEPLOY_DIR}/"
}
