SPECFILE = "SPECS/${PN}.spec"
SRC_URI = "file://${SPECFILE}"
UNPACKDIR = "${WORKDIR}/build"
S = "${UNPACKDIR}"
RPM_SOURCES_DIR ?= "${S}/SOURCES"

# FIXME: we could leverage bitbake's path resolution instead of hardcoding
SPECFILE_fn = "${THISDIR}/${PN}/${SPECFILE}"

# extract metadata from specfile: PV, PR, SRC_URI, DEPENDS, PACKAGES
python() {
    import re
    from specfile import Specfile
    with Specfile(d.getVar('SPECFILE_fn'), sourcedir=d.getVar('RPM_SOURCES_DIR')) as spec:
        # basic info
        d.setVar('PV', spec.expand(spec.version))
        d.setVar('PR', spec.expand(spec.release))

        with spec.tags() as tags:
            for tag in tags:
                # Source files
                if tag.name.startswith("Source") or tag.name.startswith("Patch"):
                    d.appendVar("SRC_URI", f" file://SOURCES/{os.path.basename(tag.expanded_value)};unpack=0")
                # BuildRequires -> DEPENDS
                if tag.name == 'BuildRequires':
                    # if the following breaks because of " " as
                    # separator, fix the specfile not this code
                    for breq in tag.expanded_value.split(','):
                        breq = breq.strip()
                        # get rid of version constraints
                        m = re.match(r"([^ ]+) (>=?|=) [^ ]+$", breq)
                        if m:
                            breq = m.group(1)
                        d.appendVar("DEPENDS", " rpm/" + breq)

        # generated RPMs
        with spec.sections() as sections:
            for section in sections:
                m = re.match(r"package -n ([^ ]*)$", section.id)
                if m:
                    d.appendVar("PACKAGES", f" {m.group(1)}")
                    continue
                m = re.match(r"package ([^ ]*)$", section.id)
                if m:
                    d.appendVar("PACKAGES", f" ${{PN}}-{m.group(1)}")
                    continue

                # FIXME "Requires:" require parsing list[str] in section.data?
}

# include this last, it requires those fields we filled just above
inherit rpm
