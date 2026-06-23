RDEPENDS ?= ""

PROVIDES = "${@ ' '.join(f"rpm/{p}" for p in d.getVar('PACKAGES').split())}"

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
            for bdep in d.getVar(f'RDEPENDS:{package}' or '').split():
                print(bdep, file=f)
    # keep a mapping of binrpms to recipes
    os.makedirs(os.path.join(rtdepsdir, "_/rpm"), exist_ok=True)
    for provided_rpm in (p for p in d.getVar('PROVIDES').split()
                         if p.startswith("rpm/")and not p.startswith("rpm/virtual/")):
        symlink_target = os.path.join(rtdepsdir, "_", provided_rpm)
        oe.path.remove(symlink_target)
        bb.debug(1, f"symlink {symlink_target} -> {os.path.join("../..", d.getVar('PN'))}")
        os.symlink(os.path.join("../..", d.getVar('PN')), symlink_target)
}

# FIXME: lacks depends on RDEPENDS:* - can't we just avoid rtdeps files?
python () {
    import dnfbridge
    # Create tasks to recursively deploy RDEPENDS packages, culling
    # the depgraph to necessary packages only (ie. do not follow
    # RDEPENDS from packages we're not interested in)
    for package in (d.getVar('PACKAGES') or "").split():
        newtask = f"do_deploy_runtimedeps_{package}"
        bb.build.addtask(newtask, None, "do_deploy do_deploy_rtdeps", d)
        d.setVarFlag(newtask, "noexec", "1")
        for rdep in (d.getVar(f'RDEPENDS:{package}') or '').split():
            if rdep == package:
                continue # avoid ref-to-self
            rdep = dnfbridge.resolve_virtual_runtime_dep(d, rdep)

            rdep_recipe = f"rpm/{rdep}"
            d.appendVarFlag(newtask, "depends", f" {rdep_recipe}:do_deploy_runtimedeps_{rdep}")
}
