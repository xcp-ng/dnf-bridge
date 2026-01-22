#!/usr/bin/env python3

# /// script
# requires-python = ">=3.12"
# dependencies = [
# ]
# ///
# Note: the above *cannot* specify `dnf`, which is not available in `pypi`
# Further more, `uv run` and friends will not work today becuse of this, so
# we cannot add arbitrary dependencies here, we really need to use just stock
# python libs.

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

import dnf # type: ignore
from dnf.package import Package # type: ignore
import hawkey # type: ignore

class ArchRpmData:
    "An index of RPMs of a given arch produced by a given SRPM"
    def __init__(self, srpm_data: SrpmData, bin_db: dnf.Base, *,
                 bridge_data: BridgeData, srpm_sourcerpm_name: str):
        self.srpm_data = srpm_data # ref back to srpm
        self.rpms: list[Package] = sorted(
            filter_dup_packages(bin_db.sack.query().filter(sourcerpm=srpm_sourcerpm_name),
                                bridge_data=bridge_data),
            key=lambda rpm: rpm.name)
        # binrpm's Requires as RDEPENDS
        self.rdepends: dict[Package, set[str]] = {}
        # list of lines to log RPM's unresolved reldep's
        self.unresolved: list[str] = []

def srpm_sourcerpm_name_of(srpm: Package) -> str:
    "name of a srpm package suitable for a filter(sourcerpm=...) query"
    return f"{srpm.name}-{srpm.version}-{srpm.release}.src.rpm" # no epoch here

class SrpmData:
    "Data about a given SRPM and the RPMs it produced"
    def __init__(self, srpm: Package, bin_dbs: dict[str, dnf.Base], bridge_data: BridgeData,
                 altarch_data: dict[str, dict[str, str | dnf.Base]]):
        self.srpm = srpm
        self.srpm_sourcerpm_name = srpm_sourcerpm_name_of(srpm) # FIXME can drop?

        self.arch_rpmdata = {}
        for arch, bin_db in bin_dbs.items():
            this_altarch_data = altarch_data.get(arch, {})
            if this_altarch_data:
                suffix: str = this_altarch_data["suffix"]
                db: dnf.Base = this_altarch_data["srcdb"]
                srpms = list(db.sack.query()
                             .filter(name=srpm.name, version=srpm.version,
                                    release__glob=srpm.release + suffix + "*")
                             .latest())
                if not srpms:
                    other_versions = [f"{p.version}-{p.release}"
                                      for p in list(db.sack.query()
                                                    .filter(name=srpm.name)
                                                    .latest())]
                    # epel-release is named epel-release-almalinux-altarch :/
                    logging.debug('package not in altarch-src: %r %r %r - other versions: %s',
                                  srpm.name, srpm.version, srpm.release + suffix + "*",
                                  ' '.join(other_versions))
                    bridge_data.version_inconsistencies[srpm.name] = (
                        f"main: {srpm.version}-{srpm.release} - altarch: {' '.join(other_versions)}")
                    continue
                altarch_srpm = srpms[0]
                srpm_sourcerpm_name = srpm_sourcerpm_name_of(altarch_srpm)
            else:
                srpm_sourcerpm_name = self.srpm_sourcerpm_name

            self.arch_rpmdata[arch] = ArchRpmData(self, bin_db, bridge_data=bridge_data,
                                                  srpm_sourcerpm_name=srpm_sourcerpm_name)

        self.rpms_by_arch: dict[str, dict[str, Package]] = {} # rpm.name -> arch -> rpm view
        for arch, arch_rpmdata in self.arch_rpmdata.items():
            for rpm in arch_rpmdata.rpms:
                if rpm.name not in self.rpms_by_arch:
                    self.rpms_by_arch[rpm.name] = {}
                assert arch not in self.rpms_by_arch[rpm.name]
                self.rpms_by_arch[rpm.name][arch] = rpm

# Mapping to turn RPM dependencies like "libcurl.so.4()(64bit)" and
# "mvn(org.apache.tomcat:tomcat-catalina)" into acceptable (virtual)
# package names
RPM_INVALID_CHARS = " ()[]:"
NAME_SANITIZER = str.maketrans(RPM_INVALID_CHARS, "_" * len(RPM_INVALID_CHARS))

class BridgeData:
    def __init__(self, src_db: dnf.Base, bin_dbs: dict[str, dnf.Base]):
        self.src_db = src_db
        self.bin_dbs = bin_dbs
        self.per_repo_altarch_src_dbs: list[dict[str, dnf.Base]] = [] # used only for reporting
        self.arch_providers_cache: dict[str, dict[hawkey.Reldep, str | None]] = {}
        self.packages: list[SrpmData] = []
        self.per_repo_rpms_src: list[list[Package]] = []
        # FIXME: separate by arch
        self.virtual_providers: dict[str, list[Package]] = {}
        self.virtual_provides: dict[Package, list[str]] = {}
        # list of lines to log overall unresolved reldep's
        self.arch_unresolved: dict[str, list[hawkey.Reldep]] = {}
        # for each package, detailed string to log
        self.version_inconsistencies: dict[str, str] = {}
        self.allowed_mismatched_checksums: list[str] = []
        self.exclude_packages: set(str) = set() # packages excluded from any repo
        self.config: dict | None = None
        self.layer: Path | None = None

    def __repr__(self) -> str:
        return (f'<BridgeData for "{self.layer}", {len(self.packages)} packages,'
                f' {sum(len(unresolved) for unresolved in self.arch_unresolved.values())} unresolved>')

    def packages_named(self, name: str) -> list[Package]:
        return [p for p in self.packages if p.srpm.name == name]

    def insert_pkg(self, pkg: Package, *, altarch_data: dict[str, dict[str, str | dnf.Base]]) -> None:
        """Record a new `SrpmData` for `pkg`, resolving Requires into RDEPENDS.
        """
        srpm_data = SrpmData(pkg, self.bin_dbs, self, altarch_data=altarch_data)

        if pkg.name not in self.allowed_mismatched_checksums:
            handled = [p.srpm for p in self.packages_named(pkg.name)]
            assert not handled, f"{pkg!r} previously handled as {handled}"
        self.packages.append(srpm_data)

    def resolve_pkgs(self, altarch_suffixes: dict[str, list[str]]) -> None:
        for arch, bin_db in self.bin_dbs.items():
            if arch not in self.arch_unresolved:
                self.arch_unresolved[arch] = []
            assert arch not in self.arch_providers_cache
            self.arch_providers_cache[arch] = {}
            for srpm_data in self.packages:
                logging.debug("> %s %s", arch, srpm_data.srpm_sourcerpm_name)
                if arch not in srpm_data.arch_rpmdata:
                    continue    # already logged in SrpmData.__init__
                rpmdata = srpm_data.arch_rpmdata[arch]
                for binpkg in rpmdata.rpms:
                    logging.debug(">> %s %s", arch, binpkg)

                    rel: hawkey.Reldep
                    rpmdata.rdepends[binpkg] = set()
                    for rel in binpkg.requires:
                        self.__resolve(arch, bin_db, rel,
                                       altarch_suffixes=altarch_suffixes.get(arch, []))
                        provider = self.arch_providers_cache[arch][rel]
                        if provider:
                            rpmdata.rdepends[binpkg].add(provider)
                        else:
                            rpmdata.unresolved.append(f"{binpkg.name}: {rel}")

    def __resolve(self, arch: str, bin_db: dnf.Base, rel: hawkey.Reldep, *,
                  altarch_suffixes: list[str]) -> None:
        "Resolve `rel` and record result in `self.arch_providers_cache`, if not already there"

        if rel in self.arch_providers_cache[arch]:
            return

        providers = list(bin_db.sack.query().filter(provides=rel).filter(latest=1))
        logging.debug(">>> '%s' provided by: %s", rel, providers)

        # For now don't express unresolvable Requires.  We will want
        # to drop this and let this handled by the "!= 1" case below,
        # but Bitbake requires all RDEPENDS to be resolvable, and has
        # no way currently to understand which ones we don't need.
        if len(providers) == 0:
            self.arch_providers_cache[arch][rel] = None
            logging.debug(">>>> Ignoring unresolvable dependency '%s'", rel)
            self.arch_unresolved[arch].append(rel)
            return

        # Some Provides resolution get a mix of obsolete packages when
        # things move and SRPMs from both before and after a move are
        # visible.  If that happens we would deal with virtual
        # packages that could resolve to RPMs not produced by any
        # recipe, so filter those out.
        newproviders = []
        flag = False
        for p in providers:
            srpm_sourcerpm_name = p.sourcerpm
            if altarch_suffixes:
                # derive original SRPM name (without any rebuild suffix)
                for suffix in altarch_suffixes:
                    srpm_sourcerpm_name = re.sub(re.escape(suffix) + r'(\.[0-9.]*[0-9])?', '',
                                                 srpm_sourcerpm_name)
            if srpm_sourcerpm_name in (srpmdata.srpm_sourcerpm_name for srpmdata in self.packages):
                newproviders.append(p)
            else:
                srpm_name = '-'.join(srpm_sourcerpm_name.split('-')[:-2]) # strip [e]vr
                srpm_version, srpm_release = srpm_sourcerpm_name.split('-')[-2:]
                other_versions = [srpmdata.srpm_sourcerpm_name for srpmdata in self.packages
                                  if srpmdata.srpm.name == srpm_name]
                if srpm_name in self.version_inconsistencies:
                    logging.debug(">>>> %r (%s) not in srpmdata (known inconsistency), other versions: %s",
                                  p.sourcerpm, srpm_sourcerpm_name, ' '.join(other_versions))
                elif srpm_name in self.exclude_packages and not other_versions:
                    # note: that check likely has false negatives in case of repo overlap
                    logging.debug(">>>> %r (%s) not in srpmdata (excluded)",
                                  p.sourcerpm, srpm_sourcerpm_name)
                else:
                    logging.debug(">>>> %r (%s) not in srpmdata (should be investigated), other versions: %s",
                                  p.sourcerpm, srpm_sourcerpm_name, ' '.join(other_versions))
                flag = True
        if len(newproviders) == 0:
            self.arch_providers_cache[arch][rel] = None
            logging.debug("Ignoring unresolvable-after-dropping-obsolete-rpms dependency '%s'", rel)
            self.arch_unresolved[arch].append(rel)
            return
        providers = newproviders
        if flag:
            logging.debug("'%s' selected provider: %s", rel, providers)

        # filter out dups existing in different repos
        if len(providers) > 1:
            providers = filter_dup_packages(providers, bridge_data=self)

        # transform relations to multiple providers to use a virtual package
        if len(providers) != 1:
            vprovname = reldep_to_virtual(rel)
            # record as virtual provides
            self.virtual_providers[vprovname] = providers
            for p in providers:
                if p not in self.virtual_provides:
                    self.virtual_provides[p] = []
                self.virtual_provides[p].append(vprovname)

            self.arch_providers_cache[arch][rel] = vprovname
            return

        assert len(providers) == 1, f"too many providers for {rel}: {[str(p) for p in providers]} {tuple(p.name for p in providers)}"

        self.arch_providers_cache[arch][rel] = providers[0].name

def reldep_to_virtual(rel: hawkey.Reldep) -> str:
    relstr = str(rel)
    if relstr[0] == '(' and relstr[-1] == ')':
        relstr = relstr[1:-1]
    return "virtual/" + (relstr.replace(">=", "ge").replace(">", "gt")
                         .replace("<=", "le").replace("<", "lt")
                         .replace("=", "eq")
                         .translate(NAME_SANITIZER))

def format_arch_dependent_list(varname: str, arch_items: dict[str, list[str]]) -> str:
    """Format a variable with potentially-different values per arch.

    Format as a single variable definition if possible, use arch overrides if not.
    """
    unique_items_sets = []
    for items in arch_items.values():
        if items not in unique_items_sets:
            unique_items_sets.append(items)

    if len(unique_items_sets) == 1:
        items = unique_items_sets[0]
        return ' \\\n '.join([f'{varname} = "'] + items + ['"'])

    arch_items_content_lines = {
        arch: " \\\n " + ' \\\n '.join(items) + " \\\n"
        for arch, items in arch_items.items()
    }
    return '\n'.join(f'{varname}:{arch} = "{lines}"'
                     for arch, lines in arch_items_content_lines.items())

def altarch_aware_variable(varname: str, repo_config: dict, arch: str,
                           only_altarch: bool = False) -> str | None:
    "Deal with variables being possibly overridden in altarch case"
    if "altarch" in repo_config and arch in repo_config["altarch"]:
        try:
            url = repo_config["altarch"][arch][varname]
        except KeyError as ex:
            raise KeyError(f"expecting '{varname}' in repo_config['altarch'][{arch}]") from ex
        assert isinstance(url, str)
        return url
    if only_altarch:
        return None
    assert isinstance(repo_config[varname], str), f"expected a str: repo_config[{varname}] = {repo_config[varname]!r}"
    return repo_config[varname]

def write_recipe(srpm_data: SrpmData, recipesdir: Path, repo_config: dict, bridge_data: BridgeData
                 ) -> None:
    """Write a .bb file from info collected in SrpmData object.
    """
    pkg = srpm_data.srpm
    fname = f"{pkg.name}_{f'{pkg.epoch}:' if pkg.epoch else ''}{pkg.version}-{pkg.release}.bb"
    with open(recipesdir / fname, "w") as r:
        maybeepochline = f'PE = "{pkg.epoch}"\n' if pkg.epoch else ""
        arch_packages = {arch: [rpm.name for rpm in arch_rpmdata.rpms]
                         for arch, arch_rpmdata in srpm_data.arch_rpmdata.items()}
        arch_packages_lines = format_arch_dependent_list('PACKAGES', arch_packages)

        print(f"""# File generated by {os.path.basename(sys.argv[0])}, do not modify

inherit dnf-bridge

PN = "{pkg.name}"
{maybeepochline}PV = "{pkg.version}"
PR = "{pkg.release}"
{arch_packages_lines}
""", end='', file=r)

        url = (pkg.remote_location(schemes=["https", "http", "file"])
               .replace(repo_config["basesrcurl"], f"${{{repo_config['basesrcurl_bbvar']}}}"))
        print(f'\nURI_src = "{url};name=src;unpack=0"',
              file=r)
        print(f'SRC_URI = "${{URI_src}}"', file=r)
        assert pkg.chksum[0] == hawkey.chksum_type("sha256")
        print(f'SRC_URI[src.sha256sum] = "{pkg.chksum[1].hex()}"', file=r)

        for arch, arch_rpmdata in srpm_data.arch_rpmdata.items():
            if arch_rpmdata.unresolved:
                print(f'''
## Requires ({arch}) that were seen as not satisfiable in original repo:
{'\n'.join(f"# - {line}" for line in sorted(arch_rpmdata.unresolved))}
''', end='', file=r)

        all_virtual_provides: dict[str, set[str]] = {}
        for arch, arch_rpmdata in srpm_data.arch_rpmdata.items():
            all_virtual_provides[arch] = set()
            arch_baseurl = altarch_aware_variable("baseurl", repo_config, arch)
            arch_baseurl_bbvar = altarch_aware_variable("baseurl_bbvar", repo_config, arch).format(arch=arch)
            for binpkg in arch_rpmdata.rpms:
                url = (binpkg.remote_location(schemes=["https", "http", "file"])
                       .replace(arch_baseurl, "${%s}" % arch_baseurl_bbvar))
                print(f'\nURI_{arch}_{binpkg.name} = "{url};name={arch}_{binpkg.name};unpack=0"',
                      file=r)
                print(f'SRC_URI:append = " ${{URI_{arch}_{binpkg.name}}}"', file=r)
                assert binpkg.chksum[0] == hawkey.chksum_type("sha256")
                print(f'SRC_URI[{arch}_{binpkg.name}.sha256sum] = "{binpkg.chksum[1].hex()}"', file=r)

                if binpkg in bridge_data.virtual_provides:
                    print(f'RPROVIDES:{binpkg.name}:append:{arch} = " {" ".join(sorted(bridge_data.virtual_provides[binpkg]))}"', file=r)
                    all_virtual_provides[arch].update(bridge_data.virtual_provides[binpkg])

        print("", file=r)
        for rpmname, arch_rpm in srpm_data.rpms_by_arch.items():
            arch_rdeps = {
                arch: sorted(arch_rpmdata.rdepends[arch_rpm[arch]])
                for arch, arch_rpmdata in srpm_data.arch_rpmdata.items()
                if arch in arch_rpm
            }
            print(format_arch_dependent_list(f'RDEPENDS:{rpmname}', arch_rdeps), file=r)

def new_dnf_db(arch: str, dnftmpdir: str) -> dnf.Base:
    "Build a new DNF config isolated from any system dnf config files"
    conf = dnf.conf.Conf()

    conf_config_file_path = conf._config.config_file_path()
    conf_config_file_path.set(value='/dev/null', priority=conf_config_file_path.getPriority())
    conf_reposdir = conf._config.reposdir()
    conf_reposdir.set(conf_reposdir.getPriority(), os.path.join(dnftmpdir, "repos"))
    conf_persistdir = conf._config.persistdir()
    conf_persistdir.set(conf_persistdir.getPriority(), os.path.join(dnftmpdir, "persist"))
    conf_system_cachedir = conf._config.system_cachedir()
    conf_system_cachedir.set(conf_system_cachedir.getPriority(), os.path.join(dnftmpdir, "cache"))
    conf_varsdir = conf._config.varsdir()
    conf_varsdir.set(conf_varsdir.getPriority(), os.path.join(dnftmpdir, "vars"))

    # necessary to solve deps against concrete files
    conf._config.optional_metadata_types().getValue().push_back('load_filelists')

    db = dnf.Base(conf=conf)
    if arch not in ('src', db.conf.substitutions['arch']):
        db.conf.substitutions['arch'] = arch
        db.conf.substitutions['basearch'] = dnf.rpm.basearch(arch)

    return conf, db

def compute_bridge_data(archs: list[str], repo_configs: list[dict]) -> BridgeData:
    """Fetches data from DNF repo and compute the bits needed for bitbake.
    """
    confs: dict[str, dnf.conf.Conf] = {}
    dbs = {}

    with tempfile.TemporaryDirectory() as dnftmpdir:
        ## setup dnf config

        logging.info("STAGE importing package information")
        for i in ['src'] + archs:
            confs[i], dbs[i] = new_dnf_db(i, dnftmpdir=os.path.join(dnftmpdir, i))

        srcrepo_sections: list[list[str]] = [] # one list of section repoids per repo_config
        for config in repo_configs:
            srcrepo_sections.append([])
            if "sections" in config:
                for section in config["sections"]:
                    baserepoid = f"{config['name']}-{section}"
                    srcurl = config["srcurl"].format(basesrcurl=config["basesrcurl"], section=section)
                    dbs['src'].repos.add_new_repo(f"{baserepoid}-src", confs['src'], [srcurl])
                    for arch in archs:
                        binurl = config["binurl"].format(
                            baseurl=altarch_aware_variable("baseurl", config, arch),
                            section=section, arch=arch)
                        dbs[arch].repos.add_new_repo(f"{baserepoid}-{arch}", confs[arch], [binurl])
                    srcrepo_sections[-1].append(f"{baserepoid}-src")
            else:
                baserepoid = config['name']
                srcurl = config["srcurl"].format(basesrcurl=config["basesrcurl"])
                dbs['src'].repos.add_new_repo(f"{baserepoid}-src", confs['src'], [srcurl])
                for arch in archs:
                    binurl = config["binurl"].format(
                        baseurl=altarch_aware_variable("baseurl", config, arch),
                        arch=arch)
                    dbs[arch].repos.add_new_repo(f"{baserepoid}-{arch}", confs[arch], [binurl])
                srcrepo_sections[-1].append(f"{baserepoid}-src")

        for arch in archs:
            dbs[arch].fill_sack(load_system_repo=False)
        dbs['src'].fill_sack(load_system_repo=False)

        # get repo metadata for all packages, per repo_config
        per_repo_rpms_src: list[list[Package]] = [
            sum((list(dbs['src'].sack.query().filter(latest=1, reponame=repoid).available())
                 for repoid in srcrepos), [])
            for srcrepos in srcrepo_sections
        ]
        logging.info(f"packages per repo: {[len(l) for l in per_repo_rpms_src]}")

        # altarch handling needs for each arch all suffix patterns from all repos
        altarch_suffixes = {}
        for repo_config in repo_configs:
            for arch, altarch_data in repo_config.get("altarch", {}).items():
                if arch not in altarch_suffixes:
                    altarch_suffixes[arch] = []
                altarch_suffixes[arch].append(altarch_data["suffix"])

        # incrementally build bridge data
        bridge_data = BridgeData(dbs['src'], {arch: dbs[arch] for arch in archs})

        for repo_config, rpms_src in zip(repo_configs, per_repo_rpms_src):
            exclude_packages = repo_config.get("exclude_packages", [])
            bridge_data.exclude_packages.update(exclude_packages)
            altarch_data = repo_config.get("altarch", {})

            # add repo to dnf config
            bridge_data.per_repo_altarch_src_dbs.append({})
            for arch, this_altarch_data in altarch_data.items():
                altarch_conf, this_altarch_data["srcdb"] = new_dnf_db(
                    arch, os.path.join(dnftmpdir, "altarchsrc", repo_config["name"], arch))
                srcurl = repo_config["srcurl"].format(basesrcurl=this_altarch_data["basesrcurl"])
                this_altarch_data["srcdb"].repos.add_new_repo(f"{repo_config["name"]}-altarch-src", altarch_conf, [srcurl])
                this_altarch_data["srcdb"].fill_sack(load_system_repo=False)
                bridge_data.per_repo_altarch_src_dbs[-1][arch] = this_altarch_data["srcdb"]

            bridge_data.per_repo_rpms_src.append(rpms_src)
            for pkg in filter_dup_packages(rpms_src, bridge_data=bridge_data):
                if pkg.name in exclude_packages:
                    logging.info("Ignoring excluded package: %s", pkg)
                    continue
                bridge_data.insert_pkg(pkg, altarch_data=altarch_data)
        # this makes rpm-against-srpms check against all repos, but it
        # should not be a problem?
        logging.info("STAGE dependency resolution")
        bridge_data.resolve_pkgs(altarch_suffixes)

        return bridge_data

def filter_dup_packages(l: list[Package], *, bridge_data: BridgeData) -> list[Package]:
    """Filter out packages appearing in more than one repo/section.

    This deals with a number of Alma binary RPMs appearing in more
    than one section of the distro, making sure they have same
    checksum and select one.

    An exception mechanism is provided to deal with bugs, like in Alma
    10.1 where epel-release-almalinux-altarch appears both in Alma and
    EPEL, and have different checksums.
    """
    # prepare to index package by (rpmname, chksum)
    pkg_info = list(zip(((os.path.basename(p.remote_location()), p.chksum) for p in l),
                        l))
    # unicity of rpmname, and of (rpmname, chksum)
    unique_packages_contents = set(info for (info, _pkg) in pkg_info)
    unique_packages_by_rpmname = dict((rpmname, pkg)
                                      for ((rpmname, _chksum), pkg) in pkg_info)

    # sanity check: dups must have identical contents
    if len(unique_packages_by_rpmname) and next(iter(unique_packages_by_rpmname.values())).name in bridge_data.allowed_mismatched_checksums:
        logging.info("ignoring potential cksum mismatch for %s", next(iter(unique_packages_by_rpmname.values())).name)
    else:
        assert len(unique_packages_by_rpmname) == len(unique_packages_contents), (
            "identical rpmname with different chksum"
            f" (len({unique_packages_by_rpmname})==len({unique_packages_contents})) in {l}:"
            f" {unique_packages_contents}")

    if len(unique_packages_by_rpmname) == len(l):
        return l

    assert unique_packages_by_rpmname
    assert len(unique_packages_by_rpmname) < len(l)
    logging.debug("filter_dup_packages: filtered out %s duplicate packages",
                  len(l) - len(unique_packages_by_rpmname))
    unique_packages = dict(unique_packages_by_rpmname)
    return list(unique_packages.values())

def write_bridge_layer(outlayer: Path, repo_config: dict, packages: list[Package],
                       bridge_data: BridgeData) -> None:
    """Create recipe files, and config files for tunable defaults.
    """
    logging.debug("write_bridge_layer('%s') ...", repo_config['name'])
    recipesdir = outlayer / repo_config["recipes"]
    if os.path.exists(recipesdir):
        shutil.rmtree(recipesdir)
    os.makedirs(recipesdir)
    for srpm_data in bridge_data.packages:
        if srpm_data.srpm not in packages:
            continue
        write_recipe(srpm_data, recipesdir, repo_config, bridge_data)

    conffile = outlayer / "conf" / repo_config["default_bbvar_conf"]
    with open(conffile, "w") as f:
        print(f'''# File generated by {os.path.basename(sys.argv[0])}, do not modify
{repo_config["basesrcurl_bbvar"]} = "{repo_config["basesrcurl"]}"
{repo_config["baseurl_bbvar"]} = "{repo_config["baseurl"]}"
''', end='', file=f)
        for altarch_data in repo_config.get("altarch", {}).values():
            print(f'{altarch_data["baseurl_bbvar"]} = "{altarch_data["baseurl"]}"', file=f)

def cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a proxy BitBake layer for a DNF repo")
    parser.add_argument('-v', '--verbose', action="count", default=0,
                        help="increase verbosity level")
    parser.add_argument('output_layer',
                        help="directory in which to write the resulting layer data")
    return parser

def do_setup() -> Path:
    args = cli_parser().parse_args()
    match args.verbose:
        case 0:
            LOGLEVEL = logging.WARNING
        case 1:
            LOGLEVEL = logging.INFO
        case _:
            LOGLEVEL = logging.DEBUG

    logging.basicConfig(
        level=LOGLEVEL,
        format='{asctime}|{levelname}: {message}', style='{')

    return Path(args.output_layer)

def do_read(layer: Path) -> BridgeData:
    with open(layer / "conf/dnf-bridge.toml", "rb") as fp:
        config = tomllib.load(fp)

    logging.info("config: %s", config)

    # poor man's schema checking
    assert "archs" in config
    assert isinstance(config["archs"], list)
    assert all(isinstance(arch, str) for arch in config["archs"])
    assert "repo" in config
    assert isinstance(config["repo"], list)
    for repo in config["repo"]:
        for repokey in ("baseurl basesrcurl srcurl binurl recipes "
                        "default_bbvar_conf baseurl_bbvar basesrcurl_bbvar").split():
            assert repokey in repo, f"{repokey!r} not in config['repo']"

    bridge_data = compute_bridge_data(config["archs"], config["repo"])
    bridge_data.allowed_mismatched_checksums = config.get("allowed_mismatched_checksums", [])
    bridge_data.layer = layer
    bridge_data.config = config
    return bridge_data

def format_repo_info(repo: dnf.repo.Repo) -> str:
    return (f"revision: {repo._repo.getRevision()},"
            f" updated-utc: {time.asctime(time.gmtime(repo._repo.getMaxTimestamp()))}")

def do_write(bridge_data: BridgeData) -> None:
    logging.info("STAGE writing out layer")
    assert bridge_data.layer
    assert bridge_data.config
    for repo_config, packages in zip(bridge_data.config["repo"], bridge_data.per_repo_rpms_src):
        write_bridge_layer(bridge_data.layer, repo_config, packages, bridge_data)

    # metadata to relate to repo changes (mimics "dnf repolist -v" implementation)
    repo_info: dict[str, str] = {}
    for repoid, repo in bridge_data.src_db.repos.items():
        assert isinstance(repo, dnf.repo.Repo)
        repo_info[repoid] = format_repo_info(repo)
    for arch, bin_db in bridge_data.bin_dbs.items():
        for repoid, repo in bin_db.repos.items():
            repo_info[f"{repoid}"] = format_repo_info(repo)
    for altarch_dbs in bridge_data.per_repo_altarch_src_dbs:
        for arch, src_db in altarch_dbs.items():
            for repoid, repo in src_db.repos.items():
                key = f"{repoid}-{arch}"
                assert key not in repo_info
                repo_info[key] = format_repo_info(repo)
    with open(bridge_data.layer / "conf" / "repo_info", "w") as f:
        # identification of this script: last commit changing it
        # 0. update index to avoid spurious "dirty", e.g. in containers
        script_dir = os.path.dirname(sys.argv[0])
        this_script = subprocess.run(['git', '-C', script_dir,
                                      'update-index', '--refresh', '-q'],
                                     check=True)
        # 1. path relative to toplevel
        this_script = subprocess.run(['git', '-C', script_dir,
                                      'ls-files', '--full-name', '--', sys.argv[0]],
                                     capture_output=True, text=True, check=True).stdout.strip()
        is_dirty = subprocess.run(['git', '-C', script_dir,
                                   'diff-index', '--quiet', 'HEAD',
                                   '--', os.path.basename(sys.argv[0])]
                                  ).returncode != 0
        dnf_bridge_version = subprocess.run(['git', '-C', script_dir,
                                             'describe', '--always', f'HEAD:{this_script}'],
                                            capture_output=True, text=True, check=True).stdout
        print(f"generated-by: {'untracked changes on ' if is_dirty else ''}{dnf_bridge_version}", file=f)
        # 2. serial number and modification date of all repos
        for repoid, info in sorted(repo_info.items(), key=lambda kv: kv[0]):
            print(f"{repoid}: {info}", file=f)

    # PREFERRED_RPROVIDER for all virtual packages
    with open(bridge_data.layer / "conf" / "default-providers.conf", "w") as f:
        for virtual, vproviders in sorted(bridge_data.virtual_providers.items(),
                                          key=lambda kv: kv[0]):
            print(f'''
# providers for {virtual}:
{"\n".join(f"# - {vprovider.name}" for vprovider in vproviders)}
PREFERRED_RPROVIDER_{virtual} ??= "{vproviders[0].name if vproviders else ''}"
''', end='', file=f)

    # diagnostics helpers

    with open(bridge_data.layer / "conf" / "unresolved.log", "w") as f:
        print("Requires that were seen as not satisfiable in original repo:", file=f)

        for arch in bridge_data.bin_dbs.keys():
            print(f'''
For arch {arch}:
{'\n'.join(sorted(str(reldep) for reldep in bridge_data.arch_unresolved[arch]))}
''', file=f)

    with open(bridge_data.layer / "conf" / "version_inconsistencies.log", "w") as f:
        print("Version inconsistencies leading to some archs not exposing all packages:\n", file=f)
        for srpmname, details in sorted(bridge_data.version_inconsistencies.items(),
                                        key=lambda kv: kv[0]):
            print(f"{srpmname}: {details}", file=f)

if __name__ == '__main__':
    # we'll use git at the very last step, don't wait to fail
    assert shutil.which("git"), "'git' executable not found in PATH"

    layer = do_setup()
    bridge_data = do_read(layer)
    do_write(bridge_data)
    logging.info("STAGE finished")

# recipe for debugging:
#
# $ podman run --rm --platform linux/amd64/v2 -it -v $PWD:/xcpng ghcr.io/almalinux/10-base:10 python3
# >>> import importlib.util
# >>> import sys
# >>> from pathlib import Path
# >>> spec = importlib.util.spec_from_file_location("gdp", "/xcpng/dnf-bridge/scripts/gen-dnf-proxy.py")
# >>> gdp = importlib.util.module_from_spec(spec)
# >>> # this is where to start again after a source modification
# >>> spec.loader.exec_module(gdp)
# >>> bridge_data = gdp.do_read(Path("/xcpng/meta-almalinux"))
# >>> # sample:
# >>> srpm_data, = bridge_data.packages_named("glibc")
