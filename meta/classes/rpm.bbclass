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
    rtdepsdir = d.getVar("RUNTIMEDEPSDIR")
    def accumulate_rdeps(depdict, newdeps_fname):
        with open(newdeps_fname) as fd:
            newdeps = fd.read().split()
        _accumulate_rdeps(depdict, newdeps)
    def _accumulate_rdeps(depdict, newdeps):
        for dep in newdeps:
            bb.debug(1, f"_accumulate_rdeps handling {dep} ...")
            bindep = dep[4:] if dep.startswith("rpm/") else dep
            bindep = bindep.lstrip('/') # leading slash for file provides
            bindep = dnfbridge.resolve_virtual_runtime_dep(d, bindep)

            dep = f"rpm/{bindep}"
            rpmname = dep[len("rpm/"):]
            bb.debug(1, f" ... rpmname = {rpmname}")
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

# Get a bumped PRAUTO to bump package revision with "+b<N>" on
# rebuilds without source change.
# Adapted from yocto's package.bbclass
# FIXME should be tied to PF, not just PN (currently does not reset on EVR bump)
PRSERV_ACTIVE = "${@bool(d.getVar("PRSERV_HOST"))}"
PRSERV_ACTIVE[vardepvalue] = "${PRSERV_ACTIVE}"
package_get_auto_pr[vardepsexclude] = "BB_TASKDEPDATA"
package_get_auto_pr[vardeps] += "PRSERV_ACTIVE"
python package_get_auto_pr() {
    import oe.prservice

    def get_do_package_hash(pn):
        if d.getVar("BB_RUNTASK") != "do_package":
            taskdepdata = d.getVar("BB_TASKDEPDATA", False)
            for dep in taskdepdata:
                if taskdepdata[dep][1] == "do_package" and taskdepdata[dep][0] == pn:
                    return taskdepdata[dep][6]
        return None

    # Support per recipe PRSERV_HOST
    pn = d.getVar('PN')
    host = d.getVar("PRSERV_HOST_" + pn)
    if not (host is None):
        d.setVar("PRSERV_HOST", host)

    # dnf-bridge: fake PKGV value to minimize upstream deviation
    #pkgv = d.getVar("PKGV")
    pkgv = "AUTOINC"

    # PR Server not active, handle AUTOINC
    if not d.getVar('PRSERV_HOST'):
        d.setVar("PRSERV_PV_AUTOINC", "0")
        return

    auto_pr = None
    pv = d.getVar("PV")
    version = d.getVar("PRAUTOINX")
    pkgarch = d.getVar("PACKAGE_ARCH")
    checksum = get_do_package_hash(pn)

    # If do_package isn't in the dependencies, we can't get the checksum...
    if not checksum:
        bb.warn('Task %s requested do_package unihash, but it was not available.' % d.getVar('BB_RUNTASK'))
        #taskdepdata = d.getVar("BB_TASKDEPDATA", False)
        #for dep in taskdepdata:
        #    bb.warn('%s:%s = %s' % (taskdepdata[dep][0], taskdepdata[dep][1], taskdepdata[dep][6]))
        return

    if d.getVar('PRSERV_LOCKDOWN'):
        auto_pr = d.getVar('PRAUTO_' + version + '_' + pkgarch) or d.getVar('PRAUTO_' + version) or None
        if auto_pr is None:
            bb.fatal("Can NOT get PRAUTO from lockdown exported file")
        d.setVar('PRAUTO',str(auto_pr))
        return

    try:
        conn = oe.prservice.prserv_make_conn(d)
        if conn is not None:
            # FIXME dnf-bridge: doublecheck we don't miss anything removed yerehere
            auto_pr = conn.getPR(version, pkgarch, checksum)
            conn.close()
    except Exception as e:
        bb.fatal("Can NOT get PRAUTO, exception %s" %  str(e))
    if auto_pr is None:
        bb.fatal("Can NOT get PRAUTO from remote PR service")
    d.setVar('PRAUTO', str(auto_pr))
}
do_build[prefuncs] += "package_get_auto_pr"

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

SSTATETASKS += "do_build"
do_build[sstate-plaindirs] = "${WORKDIR}/SRPMS ${WORKDIR}/RPMS"

addtask do_build_setscene
python do_build_setscene () {
    sstate_setscene(d)
}

# FIXME we MUST not do that, but for some reason disabling network
# fails with "newuidmap: write to uid_map failed"
do_build[network] = "1"
