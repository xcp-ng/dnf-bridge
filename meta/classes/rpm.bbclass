inherit rpm-base

DEPENDS ?= ""
PACKAGE_NEEDS_BOOTSTRAP ?= "0"
XCPNGDEV_BUILD_OPTS ?= ""

# FIXME: xcp-ng-dev commands should be provided by build-env.class
XCPNGDEV = "${DEPLOY_DIR}/build-env/bin/xcp-ng-dev"


BUILDDEPS_REPONAME = "bdeps"
BUILDDEPSDIR = "${WORKDIR}/${BUILDDEPS_REPONAME}"

addtask builddeps_repo after do_unpack
python do_builddeps_repo() {
    import dnfbridge
    rtdepsdir = d.getVar("RUNTIMEDEPSDIR")
    def accumulate_rdeps(depdict, newdeps_fname):
        with open(newdeps_fname) as fd:
            newdeps = fd.read().split()
        _accumulate_rdeps(depdict, newdeps)
    def _accumulate_rdeps(depdict, newdeps):
        for dep in newdeps:
            bindep = dep[4:] if dep.startswith("rpm/") else dep
            bindep = dnfbridge.resolve_virtual_runtime_dep(d, bindep)

            dep = f"rpm/{bindep}"
            rpmname = dep[len("rpm/"):]
            if rpmname in depdict:
                continue
            recipename = os.path.basename(os.readlink(os.path.join(rtdepsdir, "_", dep)))
            depdict[rpmname] = recipename
            accumulate_rdeps(depdict, os.path.join(rtdepsdir, recipename, f"{rpmname}.rtdeps"))

    recdepdict = {} # binrpm -> recipe

    depends = d.getVar("DEPENDS").split()
    rdeps = [dep for dep in depends if dep.startswith("rpm/")]
    #bdeps = [dep for dep in depends if not dep.startswith("rpm/")]

    _accumulate_rdeps(recdepdict, rdeps)

    rpmsdeploy_dir = d.getVar("DEPLOY_DIR_RPMS")
    builddeps_dir = d.getVar("BUILDDEPSDIR")
    os.makedirs(builddeps_dir, exist_ok=True)
    for dep in recdepdict:
        recipename = os.path.basename(os.readlink(os.path.join(rtdepsdir, '_/rpm', dep)))
        # FIXME we should really only copy the specific RPM, but we'd need its nvr for this
        oe.path.copyhardlinktree(os.path.join(rpmsdeploy_dir, recipename, "RPMS"),
                                 os.path.join(builddeps_dir, dep))
}

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
