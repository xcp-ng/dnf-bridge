#!/bin/bash
set -eux

die() {
    echo >&2 "ERROR: $*"
    exit 1
}
die_usage() {
    usage >&2
    die "$*"
}
usage() {
    cat <<EOF
Usage: $0 [<options>]

Options:
    --src <DIR>                 directory containing the srpm files
    --uid <NUMBER>              uid of host user, to replicate in container
    --autorev <SUFFIX>          suffix to append to Release: to identify rebuilds
    --install-first <PACKAGES>  space-separated list of rpm packages to install before build
EOF
}

SRCDIR=""
USERID=""
AUTOREV=""
INSTALL_FIRST=""
while [ $# -ge 1 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --src)
            [ $# -ge 2 ] || die_usage "$1 needs an argument"
            SRCDIR="$2"
            shift
            ;;
        --uid)
            [ $# -ge 2 ] || die_usage "$1 needs an argument"
            USERUID="$2"
            shift
            ;;
        --autorev)
            [ $# -ge 2 ] || die_usage "$1 needs an argument"
            AUTOREV="$2"
            shift
            ;;
        --install-first)
            [ $# -ge 2 ] || die_usage "$1 needs an argument"
            INSTALL_FIRST="$2"
            shift
            ;;
        -*)
            die_usage "unknown flag '$1'"
            ;;
        *)
            break
            ;;
    esac
    shift
done

[ $# = 0 ] || die_usage "takes no non-option argument"
[ -n "$SRCDIR" -a -n "$USERUID" -a -n "$AUTOREV" ] || die_usage "missing mandatory parameter(s)"

OUTPUTDIR=/home/builder/output
[ -d "$OUTPUTDIR" ] || die "Needs directory $OUTPUTDIR"

if [ -n "${INSTALL_FIRST}" ]; then
    dnf install -y \
        --setopt install_weak_deps=false \
        --nodocs \
        ${INSTALL_FIRST}
fi

# double the default stack size
ulimit -s 16384

# get the package arch used in the container (eg. "x86_64_v2")
# FIXME: pass as argument to be sure?
RPMARCH=$(rpm -q glibc --qf "%{arch}")

cd "$SRCDIR"

if specs=$(ls *.spec 2>/dev/null); then
    SPECFLAGS=(
        --define "_sourcedir $PWD"
        --define "_specdir $PWD"
    )
else
    specs=$(ls SPECS/*.spec 2>/dev/null)
    # SOURCES/ and SPECS/ are still the default in Alma10 but (sometimes?) sudo
    # causes source lookups in /root/rpmbuild/, naturally failing
    SPECFLAGS=(
        --define "_sourcedir $PWD/SOURCES"
    )
fi

test -r "$specs" || die "no specfile found"
specs=$(realpath "$specs")
echo "Found specfiles $specs"

dnf builddep "${SPECFLAGS[@]}" -y $specs

RPMBUILDFLAGS=(
    -ba $specs
    --target "$RPMARCH"
    "${SPECFLAGS[@]}"
    --define "autorev ${AUTOREV}"
)

# declare a user with proper UID so su can work
adduser --uid "$USERUID" builder
# fixup parent of bind-mount owned by root by default
chown "$USERUID" /home/builder

# use "su -" so anything added to /etc/profile.d/ by a build-dep gets enabled

# util-linux su trick to preserve proper word splitting: pass command
# words as arguments to 'exec "$@"'
ARGV0="rpmbuild $specs"
su - builder -c 'exec "$@"' -- "$ARGV0" rpmbuild "${RPMBUILDFLAGS[@]}"
if [ $? = 0 ]; then
    cp -a /home/builder/rpmbuild/RPMS /home/builder/rpmbuild/SRPMS "$OUTPUTDIR/"
fi
