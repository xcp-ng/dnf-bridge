#!/usr/bin/env python3

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

import dnf # type: ignore
from dnf.package import Package # type: ignore
import hawkey # type: ignore

CONFS = ('src', 'bin')

class SrpmData:
    def __init__(self, bin_db: dnf.Base, srpm: Package):
        self.srpm = srpm
        self.srpmname = f"{srpm.name}-{srpm.version}-{srpm.release}.src.rpm" # no epoch here
        self.rpms: list[Package] = sorted(
            filter_dup_packages(bin_db.sack.query().filter(sourcerpm=f"{self.srpmname}")),
            key=lambda rpm: rpm.name)
        # binrpm's Requires as RDEPENDS
        self.rdepends: dict[Package, set[str]] = {}
        # list of lines to log RPM's unresolved reldep's
        self.unresolved: list[str] = []

# Mapping to turn RPM dependencies like "libcurl.so.4()(64bit)" and
# "mvn(org.apache.tomcat:tomcat-catalina)" into acceptable (virtual)
# package names
NAME_SANITIZER = str.maketrans(" ():", "____")

class BridgeData:
    def __init__(self, bin_db: dnf.Base):
        self.bin_db = bin_db
        self.providers_cache: dict[hawkey.Reldep, str | None] = {}
        self.packages: list[SrpmData] = []
        self.per_repo_rpms_src: list[list[Package]] = []
        self.virtual_providers: dict[str, list[Package]] = {}
        self.virtual_provides: dict[Package, list[str]] = {}
        # list of lines to log overall unresolved reldep's
        self.unresolved: list[hawkey.Reldep] = []

    def insert_pkg(self, pkg: Package) -> None:
        """Record a new `SrpmData` for `pkg`, resolving Requires into RDEPENDS.
        """
        srpm_data = SrpmData(self.bin_db, pkg)

        if pkg.name not in allowed_mismatched_checksums:
            handled = [p.srpm for p in self.packages if p.srpm.name == pkg.name]
            assert not handled, f"{pkg!r} previously handled as {handled}"
        self.packages.append(srpm_data)

    def resolve_pkgs(self) -> None:
        for srpm_data in self.packages:
            logging.debug("> %s %s", arch, srpm_data.srpmname)
            for binpkg in srpm_data.rpms:
                logging.debug(">> %s", binpkg)

                rel: hawkey.Reldep
                srpm_data.rdepends[binpkg] = set()
                for rel in binpkg.requires:
                    self.__resolve(rel)
                    provider = self.providers_cache[rel]
                    if provider:
                        srpm_data.rdepends[binpkg].add(provider)
                    else:
                        srpm_data.unresolved.append(f"{binpkg.name}: {rel}")

    def __resolve(self, rel: hawkey.Reldep) -> None:
        "Resolve `rel` and record result in `self.providers_cache`, if not already there"

        if rel in self.providers_cache:
            return

        providers = list(self.bin_db.sack.query().filter(provides=rel).filter(latest=1))

        logging.debug(">>> '%s' provided by: %s", rel, providers)

        # For now don't express unresolvable Requires.  We will want
        # to drop this and let this handled by the "!= 1" case below,
        # but Bitbake requires all RDEPENDS to be resolvable, and has
        # no way currently to understand which ones we don't need.
        if len(providers) == 0:
            self.providers_cache[rel] = None
            logging.debug(">>> Ignoring unresolvable dependency '%s'", rel)
            self.unresolved.append(rel)
            return

        # Some Provides resolution get a mix of obsolete packages when
        # things move and SRPMs from both before and after a move are
        # visible.  If that happens we would deal with virtual
        # packages that could resolve to RPMs not produced by any
        # recipe, so filter those out.
        newproviders = []
        flag = False
        for p in providers:
            if p.sourcerpm in (srpmdata.srpmname for srpmdata in self.packages):
                newproviders.append(p)
            else:
                logging.info(f">>> {p.sourcerpm!r} not in srpmdata") 
                flag = True
        if len(newproviders) == 0:
            self.providers_cache[rel] = None
            logging.debug("Ignoring unresolvable-after-dropping-obsolete-rpms dependency '%s'", rel)
            self.unresolved.append(rel)
            return
        providers = newproviders
        if flag:
            logging.debug("'%s' now provided by: %s", rel, providers)

        # filter out dups existing in different repos
        if len(providers) > 1:
            providers = filter_dup_packages(providers)

        # transform relations to multiple providers to use a virtual package
        if len(providers) != 1:
            vprovname = reldep_to_virtual(rel)
            # record as virtual provides
            self.virtual_providers[vprovname] = providers
            for p in providers:
                if p not in self.virtual_provides:
                    self.virtual_provides[p] = []
                self.virtual_provides[p].append(vprovname)

            self.providers_cache[rel] = vprovname
            return

        assert len(providers) == 1, f"too many providers for {rel}: {[str(p) for p in providers]} {tuple(p.name for p in providers)}"

        self.providers_cache[rel] = providers[0].name

def reldep_to_virtual(rel: hawkey.Reldep) -> str:
    relstr = str(rel)
    if relstr[0] == '(' and relstr[-1] == ')':
        relstr = relstr[1:-1]
    return "virtual/" + (relstr.replace(">=", "ge").replace(">", "gt")
                         .replace("<=", "le").replace("<", "lt")
                         .replace("=", "eq")
                         .translate(NAME_SANITIZER))

def write_recipe(bridge_data: BridgeData, recipesdir: Path, config: dict, srpm_data: SrpmData
                 ) -> None:
    """Write a .bb file from dnf Package object.
    """
    pkg = srpm_data.srpm
    fname = f"{pkg.name}_{f'{pkg.epoch}:' if pkg.epoch else ''}{pkg.version}-{pkg.release}.bb"
    with open(recipesdir / fname, "w") as r:
        maybeepochline = f'PE = "{pkg.epoch}"\n' if pkg.epoch else ""
        print(f'''# File generated by {os.path.basename(sys.argv[0])}, do not modify

inherit dnf-bridge

PN = "{pkg.name}"
{maybeepochline}PV = "{pkg.version}"
PR = "{pkg.release}"
PACKAGES = " \\
 {' \\\n '.join(binpkg.name for binpkg in srpm_data.rpms)} \\
"
''', end='', file=r)

        if srpm_data.unresolved:
            print(f'''
## Requires that were seen as not satisfiable in original repo:
{'\n'.join(f"# - {line}" for line in sorted(srpm_data.unresolved))}
''', end='', file=r)
        url = (pkg.remote_location(schemes=["https", "http", "file"])
               .replace(config["basesrcurl"], f"${{{config["basesrcurl_bbvar"]}}}"))
        print(f'\nURI_src = "{url};name=src;unpack=0"',
              file=r)
        print(f'SRC_URI = "${{URI_src}}"', file=r)
        assert pkg.chksum[0] == hawkey.chksum_type("sha256")
        print(f'SRC_URI[src.sha256sum] = "{pkg.chksum[1].hex()}"', file=r)

        all_virtual_provides: dict[str, set[str]] = set()
        for binpkg in srpm_data.rpms:
            url = (binpkg.remote_location(schemes=["https", "http", "file"])
                   .replace(config["baseurl"], f"${{{config["baseurl_bbvar"]}}}"))
            print(f'\nURI_{binpkg.name} = "{url};name={binpkg.name};unpack=0"',
                  file=r)
            print(f'SRC_URI += "${{URI_{binpkg.name}}}"', file=r)
            assert binpkg.chksum[0] == hawkey.chksum_type("sha256")
            print(f'SRC_URI[{binpkg.name}.sha256sum] = "{binpkg.chksum[1].hex()}"', file=r)

            if binpkg in bridge_data.virtual_provides:
                print(f'RPROVIDES:{binpkg.name} = "{" ".join(sorted(bridge_data.virtual_provides[binpkg]))}"', file=r)
                all_virtual_provides.update(bridge_data.virtual_provides[binpkg])

            if srpm_data.rdepends[binpkg]:
                rdeps = f" \\\n {' \\\n '.join(sorted(srpm_data.rdepends[binpkg]))} \\\n"
            else:
                # avoid useless newlines in otherwise-empty string
                rdeps = ""
            print(f'RDEPENDS:{binpkg.name} = "{rdeps}"', file=r)

        if all_virtual_provides:
            print(f'\nPROVIDES += "{" ".join(f"rpm/{p}" for p in sorted(all_virtual_provides))}"', file=r)

def compute_bridge_data(repo_configs: list[dict]) -> BridgeData:
    dbs = {}
    cachedirs = {}

    with (tempfile.TemporaryDirectory() as persistdir,
          tempfile.TemporaryDirectory() as reposdir,
          tempfile.TemporaryDirectory() as cachedirs['src'],
          tempfile.TemporaryDirectory() as cachedirs['bin'],
          tempfile.TemporaryDirectory() as varsdir):
        ## setup dnf config

        # avoid any system dnf config files
        confs: dict[str, dnf.conf.Conf] = {}
        for i in CONFS:
            confs[i] = dnf.conf.Conf()

            conf_config_file_path = confs[i]._config.config_file_path()
            conf_config_file_path.set(value='/dev/null', priority=conf_config_file_path.getPriority())
            conf_reposdir = confs[i]._config.reposdir()
            conf_reposdir.set(conf_reposdir.getPriority(), reposdir)
            conf_persistdir = confs[i]._config.persistdir()
            conf_persistdir.set(conf_persistdir.getPriority(), persistdir)
            conf_system_cachedir = confs[i]._config.system_cachedir()
            conf_system_cachedir.set(conf_system_cachedir.getPriority(), cachedirs[i])
            conf_varsdir = confs[i]._config.varsdir()
            conf_varsdir.set(conf_varsdir.getPriority(), varsdir)

            # necessary to solve deps against concrete files
            confs[i]._config.optional_metadata_types().getValue().push_back('load_filelists')

            dbs[i] = dnf.Base(conf=confs[i])

        srcrepo_sections: list[list[str]] = []
        for config in repo_configs:
            srcrepo_sections.append([])
            if "sections" in config:
                for section in config["sections"]:
                    srcurl = config["srcurl"].format(basesrcurl=config["basesrcurl"], section=section)
                    binurl = config["binurl"].format(baseurl=config["baseurl"], section=section,
                                                     arch=config["arch"])
                    baserepoid = f"{config['name']}-{section}"
                    dbs['bin'].repos.add_new_repo(baserepoid, confs['bin'], [binurl])
                    dbs['src'].repos.add_new_repo(f"{baserepoid}-src", confs['src'], [srcurl])
                    srcrepo_sections[-1].append(f"{baserepoid}-src")
            else:
                srcurl = config["srcurl"].format(basesrcurl=config["basesrcurl"])
                binurl = config["binurl"].format(baseurl=config["baseurl"],
                                                 arch=config["arch"])
                baserepoid = config['name']
                dbs['bin'].repos.add_new_repo(baserepoid, confs['bin'], [binurl])
                dbs['src'].repos.add_new_repo(f"{baserepoid}-src", confs['src'], [srcurl])
                srcrepo_sections[-1].append(f"{baserepoid}-src")

        dbs['bin'].fill_sack(load_system_repo=False)
        dbs['src'].fill_sack(load_system_repo=False)

        # get repo metadata for all packages
        per_repo_rpms_src: list[list[Package]] = [
            sum((list(dbs['src'].sack.query().filter(latest=1, reponame=repoid).available())
                 for repoid in srcrepos), [])
            for srcrepos in srcrepo_sections
        ]
        logging.info(f"packages per repo: {[len(l) for l in per_repo_rpms_src]}")

        # incrementally build bridge data
        bridge_data = BridgeData(dbs['bin'])

        for config, rpms_src in zip(repo_configs, per_repo_rpms_src):
            exclude_packages = config.get("exclude_packages", [])
            bridge_data.per_repo_rpms_src.append(rpms_src)
            for pkg in filter_dup_packages(rpms_src):
                if pkg.name in exclude_packages:
                    logging.info("Ignoring excluded package: %s", pkg)
                    continue
                bridge_data.insert_pkg(pkg)
        # this makes rpm-against-srpms check against all repos, but it
        # should not be a problem?
        bridge_data.resolve_pkgs()

        return bridge_data

def filter_dup_packages(l: list[Package]) -> list[Package]:
    # prepare to index package by (rpmname, chksum)
    pkg_info = list(zip(((os.path.basename(p.remote_location()), p.chksum) for p in l),
                        l))
    # unicity of rpmname, and of (rpmname, chksum)
    unique_packages_contents = set(info for (info, _pkg) in pkg_info)
    unique_packages_by_rpmname = dict((rpmname, pkg)
                                      for ((rpmname, _chksum), pkg) in pkg_info)

    # sanity check: dups must have identical contents
    if len(unique_packages_by_rpmname) and next(iter(unique_packages_by_rpmname.values())).name in allowed_mismatched_checksums:
        # this package is available in Alma and EPEL with same version
        # and different checksums, at least in 10.1
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

def write_bridge_layer(outlayer: Path, config: dict, packages: list[Package],
                       bridge_data: BridgeData) -> None:
    logging.debug("write_bridge_layer('%s') ...", repo_config['name'])
    recipesdir = outlayer / config["recipes"]
    if os.path.exists(recipesdir):
        shutil.rmtree(recipesdir)
    os.makedirs(recipesdir)
    for srpm_data in bridge_data.packages:
        if srpm_data.srpm not in packages:
            continue
        write_recipe(bridge_data, recipesdir, config, srpm_data)

    conffile = outlayer / "conf" / config["default_bbvar_conf"]
    with open(conffile, "w") as f:
        print(f'''# File generated by {os.path.basename(sys.argv[0])}, do not modify
{config["basesrcurl_bbvar"]} = "{config["basesrcurl"]}"
{config["baseurl_bbvar"]} = "{config["baseurl"]}"
''', end='', file=f)

def cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a proxy BitBake layer for a DNF repo")
    parser.add_argument('-v', '--verbose', action="count", default=0,
                        help="increase verbosity level")
    parser.add_argument('output_layer',
                        help="directory in which to write the resulting layer data")
    return parser

if __name__ == '__main__':
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

    layer = Path(args.output_layer)
    with open(layer / "conf/dnf-bridge.toml", "rb") as fp:
        config = tomllib.load(fp)

    logging.info("config: %s", config)

    assert "repo" in config
    assert isinstance(config["repo"], list)
    for repo in config["repo"]:
        for repokey in ("baseurl basesrcurl arch srcurl binurl recipes "
                        "default_bbvar_conf baseurl_bbvar basesrcurl_bbvar").split():
            assert repokey in repo, f"{repokey!r} not in config['repo']"

    allowed_mismatched_checksums = config.get("allowed_mismatched_checksums", [])
    bridge_data = compute_bridge_data(config["repo"])
    for repo, packages in zip(config["repo"], bridge_data.per_repo_rpms_src):
        write_bridge_layer(layer, repo, packages, bridge_data)

    # PREFERRED_RPROVIDER for all virtual packages

    with open(layer / "conf" / "default-providers.conf", "w") as f:
        for virtual, vproviders in sorted(bridge_data.virtual_providers.items(),
                                          key=lambda kv: kv[0]):
            print(f'''
# providers for {virtual}:
{"\n".join(f"# - {vprovider.name}" for vprovider in vproviders)}
PREFERRED_RPROVIDER_{virtual} ??= "{vproviders[0].name if vproviders else ''}"
''', end='', file=f)

    with open(layer / "conf" / "unresolved.log", "w") as f:
        print(f'''Requires that were seen as not satisfiable in original repo:

{'\n'.join(sorted(str(reldep) for reldep in bridge_data.unresolved))}
''', file=f)
