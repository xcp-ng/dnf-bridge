#
# Copyright OpenEmbedded Contributors
#
# SPDX-License-Identifier: MIT
#

BB_DEFAULT_TASK ?= "build"
CLASSOVERRIDE ?= "class-target"

inherit utils
inherit utility-tasks

die() {
	bbfatal_log "$*"
}


FILESPATH = "${@base_set_filespath(["${FILE_DIRNAME}/${BP}", "${FILE_DIRNAME}/${BPN}", "${FILE_DIRNAME}/files"], d)}"
# THISDIR only works properly with imediate expansion as it has to run
# in the context of the location its used (:=)
THISDIR = "${@os.path.dirname(d.getVar('FILE'))}"

def setup_hosttools_dir(dest, toolsvar, d, fatal=True):
    tools = d.getVar(toolsvar).split()
    origbbenv = d.getVar("BB_ORIGENV", False)
    path = origbbenv.getVar("PATH")
    # Need to ignore our own scripts directories to avoid circular links
    for p in path.split(":"):
        if p.endswith("/scripts"):
            path = path.replace(p, "/ignoreme")
    bb.utils.mkdirhier(dest)
    notfound = []
    for tool in tools:
        desttool = os.path.join(dest, tool)
        if not os.path.exists(desttool):
            # clean up dead symlink
            if os.path.islink(desttool):
                os.unlink(desttool)

            # Prefer gnu-prefixed binaries, if available
            srctool = (bb.utils.which(path, "gnu" + tool, executable=True) or
                       bb.utils.which(path, tool, executable=True))

            # gcc/g++ may link to ccache on some hosts, e.g.,
            # /usr/local/bin/ccache/gcc -> /usr/bin/ccache, then which(gcc)
            # would return /usr/local/bin/ccache/gcc, but what we need is
            # /usr/bin/gcc, this code can check and fix that.
            if os.path.islink(srctool) and os.path.basename(os.readlink(srctool)) == 'ccache':
                srctool = bb.utils.which(path, tool, executable=True, direction=1)
            if srctool:
                os.symlink(srctool, desttool)
            else:
                notfound.append(tool)

    if notfound and fatal:
        bb.fatal("The following required tools (as specified by HOSTTOOLS) appear to be unavailable in PATH, please install them in order to proceed:\n  %s" % " ".join(notfound))

# We can't use vardepvalue against do_fetch directly since that would overwrite
# the other task dependencies so we use an indirect function.
python fetcher_hashes_dummyfunc() {
    return
}
fetcher_hashes_dummyfunc[vardepvalue] = "${@bb.fetch.get_hashvalue(d)}"

addtask fetch
do_fetch[dirs] = "${DL_DIR}"
do_fetch[file-checksums] = "${@bb.fetch.get_checksum_file_list(d)}"
do_fetch[prefuncs] += "fetcher_hashes_dummyfunc"
do_fetch[network] = "1"
do_fetch[umask] = "${OE_SHARED_UMASK}"
python base_do_fetch() {

    src_uri = (d.getVar('SRC_URI') or "").split()
    if not src_uri:
        return

    try:
        fetcher = bb.fetch2.Fetch(src_uri, d)
        fetcher.download()
    except bb.fetch2.BBFetchException as e:
        bb.fatal("Bitbake Fetcher Error: " + repr(e))
}

addtask unpack after do_fetch
do_unpack[cleandirs] = "${UNPACKDIR}"

python base_do_unpack() {
    import shutil

    sourcedir = d.getVar('S')
    # Intentionally keep SOURCE_BASEDIR internal to the task just for SDE
    d.setVar("SOURCE_BASEDIR", sourcedir)

    src_uri = (d.getVar('SRC_URI') or "").split()
    if not src_uri:
        return

    basedir = None
    unpackdir = d.getVar('UNPACKDIR')
    if sourcedir.startswith(unpackdir):
        basedir = sourcedir.replace(unpackdir, '').strip("/").split('/')[0]
        if basedir:
            d.setVar("SOURCE_BASEDIR", unpackdir + '/' + basedir)

    try:
        fetcher = bb.fetch2.Fetch(src_uri, d)
        fetcher.unpack(d.getVar('UNPACKDIR'))
    except bb.fetch2.BBFetchException as e:
        bb.fatal("Bitbake Fetcher Error: " + repr(e))
}


addhandler base_eventhandler
base_eventhandler[eventmask] = "bb.event.ConfigParsed bb.event.BuildStarted"
python base_eventhandler() {
    # There might be no bb.event.ConfigParsed event if bitbake server is
    # running, so check bb.event.BuildStarted too to make sure ${HOSTTOOLS_DIR}
    # exists.
    if isinstance(e, bb.event.ConfigParsed) or \
            (isinstance(e, bb.event.BuildStarted) and not os.path.exists(d.getVar('HOSTTOOLS_DIR'))):
        # Works with the line in layer.conf which changes PATH to point here
        setup_hosttools_dir(d.getVar('HOSTTOOLS_DIR'), 'HOSTTOOLS', d)
        setup_hosttools_dir(d.getVar('HOSTTOOLS_DIR'), 'HOSTTOOLS_NONFATAL', d, fatal=False)
}

addtask build after do_unpack
do_build[deptask] += "do_deploy"
do_build () {
	:
}

python () {
    # To add a recipe to the skip list , set:
    #   SKIP_RECIPE[pn] = "message"
    pn = d.getVar('PN')
    skip_msg = d.getVarFlag('SKIP_RECIPE', pn)
    if skip_msg:
        bb.debug(1, "Skipping %s %s" % (pn, skip_msg))
        raise bb.parse.SkipRecipe("Recipe will be skipped because: %s" % (skip_msg))

    need_machine = d.getVar('COMPATIBLE_MACHINE')
    if need_machine and not bb.utils.to_boolean(d.getVar('PARSE_ALL_RECIPES', False)):
        import re
        compat_machines = (d.getVar('MACHINEOVERRIDES') or "").split(":")
        for m in compat_machines:
            if re.match(need_machine, m):
                break
        else:
            raise bb.parse.SkipRecipe("incompatible with machine %s (not in COMPATIBLE_MACHINE)" % d.getVar('MACHINE'))
}

addtask cleansstate after do_clean
python do_cleansstate() {
        sstate_clean_cachefiles(d)
}
addtask cleanall after do_cleansstate
do_cleansstate[nostamp] = "1"

python do_cleanall() {
    src_uri = (d.getVar('SRC_URI') or "").split()
    if not src_uri:
        return

    try:
        fetcher = bb.fetch2.Fetch(src_uri, d)
        fetcher.clean()
    except bb.fetch2.BBFetchException as e:
        bb.fatal(str(e))
}
do_cleanall[nostamp] = "1"


EXPORT_FUNCTIONS do_fetch do_unpack
