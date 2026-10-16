inherit rpm-base

BUILDDEPS_REPONAME = "bdeps"
BUILDDEPSDIR = "${WORKDIR}/${BUILDDEPS_REPONAME}"

# FIXME for each bdep package this pulls all packages built by the same recipe
addtask builddeps_repo after do_unpack
python do_builddeps_repo() {
    recdepdict = {} # binrpm -> recipe

    depends = d.getVar("DEPENDS").split()
    rdeps = [dep for dep in depends if dep.startswith("rpm/")]
    #bdeps = [dep for dep in depends if not dep.startswith("rpm/")]

    dnfbridge.rtdeps.accumulate_rdeps_from_list(d, recdepdict, rdeps)
    dnfbridge.rtdeps.create_dnfrepo_with_contents(d, d.getVar("BUILDDEPSDIR"), recdepdict.keys())
}

# do_builddeps_repo needs all DEPENDS' do_deploy_runtimedeps_
python () {
    PREFIX = 'rpm/'
    rpm_depends = [dep for dep in d.getVar("DEPENDS").split() if dep.startswith(PREFIX)]
    for dep in rpm_depends:
        dep_rpm = dep[len(PREFIX):]
        d.appendVarFlag("do_builddeps_repo", "depends", f" {dep}:do_deploy_runtimedeps_{dep_rpm}")
}
