# srpm-intree.bbclass
#
# Subclass of rpm.bbclass with parsing of the included specfile to
# expose its relevant contents as Bitbake metadata so they don't need
# to be duplicated.  The specfile to parse should be available in the
# layer metadata (and not fetched from a remote source), as it is used
# to fill SRC_URI, which has to be done before running any task such
# as do_fetch.
#
# Note that this extracts only information that is available before
# build, and is necessarily unable to extract a binary RPM's runtime
# Requires in the general case because some of them are generated
# during build, but would also not be able to resolve any of them that
# does not refer to a simple package name or a relationship already
# turned into a virtual package by dnf-bridge.  So the ones required
# by other packages will need to be expressed by hand (or their
# do_build will simply fail).

SRPM_NAME ?= "${PN}"
SPECFILE ?= "SPECS/${SRPM_NAME}.spec"
SRC_URI = "file://${SPECFILE}"
UNPACKDIR = "${WORKDIR}/build"
S = "${UNPACKDIR}"
RPM_SOURCES_SUBDIR ?= "SOURCES"
RPM_SOURCES_DIR = "${S}/${RPM_SOURCES_SUBDIR}"

# initialize to be empty to override bitbake.conf
PACKAGES = ""

# FIXME: we could leverage bitbake's path resolution instead of hardcoding
SPECFILE_fn ?= "${THISDIR}/${PN}/${SPECFILE}"

RPM_MACROS ?= ""

# FIXME must depend on file://'s in SRC_URI
# extract metadata from specfile: PV, PR, SRC_URI, DEPENDS, PACKAGES
python() {
    # this block depends on the specfile
    # FIXME: that's just the filename not its contents?  is that even the correct
    # variable for the filename?
    bb.parse.mark_dependency(d, d.getVar('SPECFILE_fn'))

    import re
    from specfile import Specfile
    import subprocess

    RPM_INVALID_CHARS = " ()[]:"
    NAME_SANITIZER = str.maketrans(RPM_INVALID_CHARS, "_" * len(RPM_INVALID_CHARS))

    # first, get rpm macros expanded, so we only parse the relevant
    # branches of a %if, and any macro in extracted values are resolved
    macro_defines = []
    for macrodef in d.getVar("RPM_MACROS").split():
        macro, expansion = macrodef.split("=", 1)
        macro_defines.extend(["-D", f"{macro} {expansion}"])
    # FIXME: make sure rpmspec does not (or does) use any system macros
    expanded_spec = subprocess.run(["rpmspec", "--parse", d.getVar('SPECFILE_fn'),
                                    ] + macro_defines,
                                   capture_output=True, check=True, text=True).stdout

    with Specfile(content=expanded_spec, sourcedir=d.getVar('RPM_SOURCES_DIR')) as spec:
        # basic info
        d.setVar('PV', spec.expand(spec.version))
        d.setVar('PR', spec.expand(spec.release))

        with spec.tags() as tags:
            for tag in tags:
                # Source files
                if tag.name.startswith("Source") or tag.name.startswith("Patch"):
                    d.appendVar("SRC_URI", f" file://{d.getVar('RPM_SOURCES_SUBDIR')}/{os.path.basename(tag.expanded_value)};unpack=0")
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
                        # deal with virtual packages using invalid chars, which cannot
                        # be dealt with using RPROVIDES otherwise
                        for char in RPM_INVALID_CHARS:
                            if char in breq:
                                breq = "virtual/" + breq.translate(NAME_SANITIZER)
                                break

                        d.appendVar("DEPENDS", " rpm/" + breq)

        # generated RPMs
        with spec.sections() as sections:
            for section in sections:
                if section.id == 'files':
                    d.prependVar("PACKAGES", "${SRPM_NAME} ")
                m = re.match(r"package +-n +([^ ]*)$", section.id)
                if m:
                    d.appendVar("PACKAGES", f" {m.group(1)}")
                    continue
                m = re.match(r"package +([^ ]*)$", section.id)
                if m:
                    d.appendVar("PACKAGES", f" ${{SRPM_NAME}}-{m.group(1)}")
                    continue

                # FIXME "Requires:" require parsing list[str] in section.data?
}

# include this last, it requires those fields we filled just above
inherit rpm
