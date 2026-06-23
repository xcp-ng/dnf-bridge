# rpm.bbclass
#
# Support for building RPMs from a RPM source tree (unpacked specfile
# and the source files it references).  do_build relies on an external
# build-env class with the xcp-ng-build-env interface, and is provided
# with the exact list of required build dependencies by
# do_builddeps_repo.
#
# Metadata such as PN, PV, PR, DEPENDS, PACKAGES, RDEPENDS:*, PROVIDES
# must be provided to match the specfile.  This can be maintained
# manually, or through other means; see srpm-intree.bbclass for
# automated extraction from specfile.

inherit rpm-base

DEPENDS ?= ""
PACKAGE_NEEDS_BOOTSTRAP ?= "0"
XCPNGDEV_BUILD_OPTS ?= ""

# FIXME: xcp-ng-dev commands should be provided by build-env.class
XCPNGDEV = "${DEPLOY_DIR}/build-env/bin/xcp-ng-dev"


BUILDDEPS_REPONAME = "bdeps"
BUILDDEPSDIR = "${WORKDIR}/${BUILDDEPS_REPONAME}"

# FIXME for each bdep package this pulls all packages built by the same recipe
addtask builddeps_repo after do_unpack
python do_builddeps_repo() {
    import dnfbridge

    recdepdict = {} # binrpm -> recipe

    depends = d.getVar("DEPENDS").split()
    rdeps = [dep for dep in depends if dep.startswith("rpm/")]
    #bdeps = [dep for dep in depends if not dep.startswith("rpm/")]

    dnfbridge.accumulate_rdeps_from_list(d, recdepdict, rdeps)
    dnfbridge.create_dnfrepo_with_contents(d, d.getVar("BUILDDEPSDIR"), recdepdict.keys())
}

# do_builddeps_repo needs all DEPENDS' do_deploy_runtimedeps_
python () {
    PREFIX = 'rpm/'
    rpm_depends = [dep for dep in d.getVar("DEPENDS").split() if dep.startswith(PREFIX)]
    for dep in rpm_depends:
        dep_rpm = dep[len(PREFIX):]
        d.appendVarFlag("do_builddeps_repo", "depends", f" {dep}:do_deploy_runtimedeps_{dep_rpm}")
}

# produces ${WORKDIR}/SRPMS and ${WORKDIR}/RPMS
# FIXME: lacks control of parallel building?
# FIXME: set _topdir to ${WORKDIR} to stop polluting source
addtask build after do_builddeps_repo
# FIXME: do_deploy_runtimedeps should actually replace do_deploy there
do_build[depends] = "build-env:do_deploy build-env:${@'do_build_bootstrap' if ${PACKAGE_NEEDS_BOOTSTRAP} else 'do_build' }"
do_build() {
    rm -rf ${WORKDIR}/RPMS ${WORKDIR}/SRPMS

    case ${PACKAGE_NEEDS_BOOTSTRAP} in
    0) maybe_bootstrap=--isarpm ;;
    1) maybe_bootstrap=--bootstrap ;;
    esac

    env XCPNG_OCI_RUNNER=podman ${XCPNGDEV} container build "9.0" \
        $maybe_bootstrap \
        --platform "${CONTAINER_ARCH}" \
        --debug \
        --no-network --no-update --disablerepo="*" \
        --local-repo="${BUILDDEPSDIR}" --enablerepo="${BUILDDEPS_REPONAME}" \
        --output-dir="${WORKDIR}" \
        ${XCPNGDEV_BUILD_OPTS} \
        "${S}"
}

# FIXME we MUST not do that, but for some reason disabling network
# fails with "newuidmap: write to uid_map failed"
do_build[network] = "1"
