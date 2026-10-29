# loopcut.bbclass
#
# Break RPM-level dependency loop by removing chosen dependencies from
# the RDEPENDS where they are expressed but considering them where
# RDEPENDS are actually used, in accumulate_rdeps_from_list.
#
# Uses declarations of the form LOOPCUT[x] = "y z" to:
# - remove y and z from RDEPENDS:x
# - including y and z in runtime dependency lists whenever x is included

# This class handles the removal part, the rest is in
# rtdeps.accumulate_rdeps_from_list()

python() {
    loopcuts = d.getVarFlags("LOOPCUT") or {}
    if not loopcuts:
        return

    for pkg in (d.getVar("PACKAGES") or "").split():
        d.setVar(f"RDEPENDS:{pkg}:remove", loopcuts.get(pkg, ""))
}
