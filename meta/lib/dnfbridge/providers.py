# FIXME: find a way to cache this
def preferred_rpm_rproviders(d) -> dict[str, str]:
    # read defaults
    providers = dict(entry.split('=', 1)
                     for entry in (d.getVar('DEFAULT_RPM_RPROVIDERS') or '').split())

    # update with layers' preferences
    preferred =  dict(entry.split('=', 1)
                      for entry in (d.getVar('PREFERRED_RPM_RPROVIDERS') or '').split())
    providers.update(preferred)

    return providers
