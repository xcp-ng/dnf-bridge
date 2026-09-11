# rpm-base.bbclass
#
# Support for using RPM packages output by the recipe (whether built
# by the recipe or prebuilt upstream).  Provides do_deploy to make the
# output RPMs available to other recipes, and for each binary RPM
# package do_deploy_runtimedeps_* to recursively ensure all its
# runtime dependencies are similarly made available.

RDEPENDS ?= ""

# make PACKAGES and the names they RPROVIDE visible in the *recipe*
# namespace so other packages can (build-)DEPEND on them, in a "rpm/"
# sub-namespace to avoid clashes due to overlap between package and
# recipe namespaces
provided_packages = "${PACKAGES} ${@ ' '.join((d.getVar(f'RPROVIDES:{pkg}') or '') for pkg in (d.getVar('PACKAGES' or '')).split())}"
PROVIDES = "${@ ' '.join(f"rpm/{p}" for p in d.getVar('provided_packages').split())}"

DEPLOY_DIR_RPMS = "${DEPLOY_DIR}/rpms"
RECIPE_DEPLOY_DIR = "${DEPLOY_DIR_RPMS}/${PN}"

RUNTIMEDEPSDIR = "${TMPDIR}/rtdeps"

# FIXME: should be removed by do_clean?
addtask deploy after do_build
do_deploy() {
    # the package files
    rm -rf "${RECIPE_DEPLOY_DIR}"
    mkdir -p "${RECIPE_DEPLOY_DIR}"
    cp -la "${WORKDIR}/SRPMS" "${WORKDIR}/RPMS" "${RECIPE_DEPLOY_DIR}/"
}

# the runtime-dependency data
addtask deploy_rtdeps after do_build
python do_deploy_rtdeps () {
    rtdepsdir = d.getVar('RUNTIMEDEPSDIR')
    pkg_rtdepsdir = os.path.join(rtdepsdir, d.getVar('PN'))
    # FIXME: should remove old symlinks first, rather than later
    oe.path.remove(pkg_rtdepsdir, recurse=True)
    os.makedirs(pkg_rtdepsdir)
    for package in d.getVar('PACKAGES').split():
        with open(f'{pkg_rtdepsdir}/{package}.rtdeps', "wt") as f:
            for bdep in (d.getVar(f'RDEPENDS:{package}') or '').split():
                print(bdep, file=f)
        # keep a mapping of rprovides to packages
        for rprovides in (d.getVar(f'RPROVIDES:{package}') or '').split():
            if rprovides.startswith('virtual/'):
                # FIXME skipping virtual provides for now, only the PREFERRED one should be linked
                continue
            rprovides = rprovides.lstrip('/') # leading slash for file provides
            symlink_target = f'{pkg_rtdepsdir}/{rprovides}.rtdeps'
            bb.debug(1, f"symlink {pkg_rtdepsdir}/{package}.rtdeps -> {symlink_target}")
            os.makedirs(os.path.dirname(symlink_target), exist_ok=True)
            # FIXME that one would be nice to be made a relative one but subdirs make it tricky
            os.symlink(f'{pkg_rtdepsdir}/{package}.rtdeps', symlink_target)

    # keep a mapping of binrpms to recipes
    for provided_rpm in (p for p in d.getVar('PROVIDES').split()
                         if p.startswith("rpm/")and not p.startswith("rpm/virtual/")):
        symlink_target = os.path.join(rtdepsdir, "_", provided_rpm)
        # see above FIXME, symlinks are out f pkg_rtdepsdir
        oe.path.remove(symlink_target)
        bb.debug(1, f"symlink {symlink_target} -> {os.path.join("../..", d.getVar('PN'))}")
        os.makedirs(os.path.dirname(symlink_target), exist_ok=True)
        os.symlink(os.path.join("../..", d.getVar('PN')), symlink_target)
}
# FIXME check why this does not seem to work when fixing the snapping
# of the freetype/harfbuz deploop (eg. for building blktap)
python () {
    # record those variables, whose names are not constants in above
    # code, as influencing the task
    for package in (d.getVar('PACKAGES') or '').split():
        d.appendVarFlag("do_deploy_rtdeps", "vardeps", f'RDEPENDS:{package}')
}

# FIXME: should generate do_deploy_runtimedeps_ for PROVIDES as well
# or we cannot refer to them in Requires:
# FIXME: lacks depends on RDEPENDS:* - can't we just avoid rtdeps files?
python () {
    import dnfbridge
    # Create tasks to recursively deploy RDEPENDS packages, culling
    # the depgraph to necessary packages only (ie. do not follow
    # RDEPENDS from packages we're not interested in)
    for package in (d.getVar('PACKAGES') or '').split():
        newtask = f"do_deploy_runtimedeps_{package}"
        bb.build.addtask(newtask, None, "do_deploy do_deploy_rtdeps", d)
        d.setVarFlag(newtask, "noexec", "1")
        for rdep in (d.getVar(f'RDEPENDS:{package}') or '').split():
            if rdep == package:
                continue # avoid ref-to-self
            rdep = dnfbridge.resolve_virtual_runtime_dep(d, rdep)

            rdep_recipe = f"rpm/{rdep}"
            d.appendVarFlag(newtask, "depends", f" {rdep_recipe}:do_deploy_runtimedeps_{rdep}")
        # do_deploy_runtimedeps_ tasks for RPROVIDES, so those can be used in RDEPENDS
        for provides in (d.getVar(f'RPROVIDES:{package}') or '').split():
            new_provides_task = f"do_deploy_runtimedeps_{provides}"
            bb.build.addtask(new_provides_task, None, newtask, d)
            d.setVarFlag(new_provides_task, "noexec", "1")
}
