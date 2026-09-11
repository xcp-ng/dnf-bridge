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
