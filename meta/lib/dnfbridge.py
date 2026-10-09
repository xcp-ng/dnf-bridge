from collections.abc import Iterable
import shutil

import oe.path

# FIXME: depdict could likely just be a set[str]
def accumulate_rdeps_from_file(d, depdict: dict[str, str], newdeps_fname: str) -> None:
    """Accumulate dependencies from file `newdeps_fname` into `depdict`

    `depdict` should usually start empty. It will contain a mapping of
    binrpm name towards recipe name.
    """
    bb.debug(1, f" accumulate_rdeps_from_file({newdeps_fname}) ...")
    with open(newdeps_fname) as fd:
        newdeps = fd.read().split()
    accumulate_rdeps_from_list(d, depdict, newdeps)

def accumulate_rdeps_from_list(d, depdict: dict[str, str], newdeps: list[str]) -> None:
    "Accumulate dependencies from `newdeps` list into `depdict`"
    bb.debug(1, f" accumulate_rdeps_from_list({newdeps}) ...")
    rtdepsdir = d.getVar("RUNTIMEDEPSDIR")
    for dep in newdeps:
        bb.debug(1, f"  accumulate_rdeps_from_list({dep}) ...")
        # strip any rpm/ suffix comming from a DEPENDS
        # FIXME: extract back into do_builddeps_repo?
        bindep = dep[4:] if dep.startswith("rpm/") else dep
        # leading slash for file provides
        bindep = bindep.lstrip('/')
        # turn virtual deps into real packages
        bindep = resolve_virtual_runtime_dep(d, bindep)

        dep = f"rpm/{bindep}"
        rpmname = dep[len("rpm/"):]
        bb.debug(1, f"  ... rpmname = {rpmname}")
        if rpmname in depdict:
            continue
        recipename = os.path.basename(os.readlink(os.path.join(rtdepsdir, "_", dep)))
        depdict[rpmname] = recipename
        accumulate_rdeps_from_file(d, depdict, os.path.join(rtdepsdir, recipename, f"{rpmname}.rtdeps"))

def create_dnfrepo_with_contents(d, target_dir: str, packages: Iterable[str]) -> None:
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    os.makedirs(target_dir)
    rpmsdeploy_dir = d.getVar("DEPLOY_DIR_RPMS")
    rtdepsdir = d.getVar("RUNTIMEDEPSDIR")
    for pkg in packages:
        recipename = os.path.basename(os.readlink(os.path.join(rtdepsdir, '_/rpm', pkg)))
        # FIXME we should really only copy the specific RPM, but we'd need its nvr for this
        oe.path.copyhardlinktree(os.path.join(rpmsdeploy_dir, recipename, "RPMS"),
                                 os.path.join(target_dir, pkg))

# FIXME this is likely already available in standard libs
# FIXME should warn/error when selected preferred does not RPROVIDES pkg
# FIXME should pick a default one in absence of PREFERRED_
def resolve_virtual_runtime_dep(d, pkg):
    if pkg.startswith("virtual/"):
        # dependencies against virtual providers need us to lookup PREFERRED_RPM_RPROVIDER_* ourselves?
        preferred = d.getVar(f"PREFERRED_RPM_RPROVIDER_{pkg}")
        if preferred is not None:
            return preferred

        bb.error(f"PREFERRED_RPM_RPROVIDER_{pkg} not set, dependency graph will be incomplete")

    return pkg
