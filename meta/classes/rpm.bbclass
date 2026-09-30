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

    def get_do_build_hash(pn):
        taskdepdata = d.getVar("BB_TASKDEPDATA", False)
        for dep in taskdepdata:
            if taskdepdata[dep][1] == "do_build" and taskdepdata[dep][0] == pn:
                return taskdepdata[dep][6]
        bb.fatal("package hash not found")

    # Support per recipe PRSERV_HOST
    pn = d.getVar('PN')
    host = d.getVar("PRSERV_HOST_" + pn)
    if not (host is None):
        d.setVar("PRSERV_HOST", host)

    # PR Server not active, handle AUTOINC
    if not d.getVar('PRSERV_HOST'):
        bb.warn(f"PRSERV_HOST not set")
        d.setVar("PRAUTO", "0")
        return

    auto_pr = None
    pv = d.getVar("PV")
    version = d.getVar("PRAUTOINX")
    pkgarch = d.getVar("PACKAGE_ARCH")
    checksum = get_do_build_hash(pn)

    if d.getVar('PRSERV_LOCKDOWN'):
        auto_pr = d.getVar('PRAUTO_' + version + '_' + pkgarch) or d.getVar('PRAUTO_' + version) or None
        if auto_pr is None:
            bb.fatal("Can NOT get PRAUTO from lockdown exported file")
        bb.note(f"PRAUTO={auto_pr}, from lockdown exported file")
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
        --define "autorev +b${PRAUTO}" \
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


addtask checkinstall after do_build
do_checkinstall[noexec] = "1"

python check_install() {
    import subprocess
    import dnfbridge

    TASK_PREFIX = "do_checkinstall_"
    this_task = d.getVar("BB_RUNTASK")
    assert this_task.startswith(TASK_PREFIX)
    this_package = this_task[len(TASK_PREFIX):]

    bb.note(f"check_install for {this_package}")

    # prepare a dnf repo with all deps
    rdepsdir = f"{d.getVar('WORKDIR')}/rdeps/{this_package}"
    recdepdict = {} # binrpm -> recipe
    dnfbridge.accumulate_rdeps_from_list(d, recdepdict, [this_package])
    dnfbridge.create_dnfrepo_with_contents(d, rdepsdir, recdepdict.keys())

    maybe_bootstrap = "--bootstrap" if d.getVar('PACKAGE_NEEDS_BOOTSTRAP') else "--isarpm"

    cmd = ['env', 'XCPNG_OCI_RUNNER=podman', d.getVar('XCPNGDEV'), 'container', 'run', '9.0',
           maybe_bootstrap,
           '--platform', d.getVar('CONTAINER_ARCH'),
           '--debug',
           '--no-network', '--no-update', '--disablerepo=*',
           '--local-repo', f"rdeps:{rdepsdir}", '--enablerepo', 'rdeps',
           d.getVar('XCPNGDEV_BUILD_OPTS'),
           '--',
           'sudo', 'dnf', 'install', '-y', this_package]
    subprocess.check_call(cmd)
}

# do_checkinstall needs each PACKAGES' do_checkinstall_
python () {
    pn = d.getVar('PN')
    for pkg in d.getVar("PACKAGES").split():
        newtask = f"do_checkinstall_{pkg}"
        bb.build.addtask(newtask, "do_checkinstall", f"do_deploy_runtimedeps_{pkg}", d)
        # we apparently cannot set the task function directly, set it
        # to something falsy not non-None to avoid a warning, and use
        # prefuncs for what would be the task
        d.setVar(newtask, "")
        d.setVarFlag(newtask, "prefuncs", "check_install")
        # FIXME we MUST not do that, but for some reason disabling network
        # fails with "newuidmap: write to uid_map failed"
        d.setVarFlag(newtask, "network", "1")
}
