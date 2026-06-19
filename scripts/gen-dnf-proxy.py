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

class ArchRpmData:
    "An index of RPMs of a given arch produced by a given SRPM"
    def __init__(self, srpm_data: SrpmData, bin_db: dnf.Base, *, bridge_data: BridgeData):
        self.srpm_data = srpm_data # ref back to srpm
        self.rpms: list[Package] = sorted(
            filter_dup_packages(bin_db.sack.query().filter(sourcerpm=f"{self.srpm_data.srpmname}"),
                                bridge_data=bridge_data),
            key=lambda rpm: rpm.name)
        # binrpm's Requires as RDEPENDS
        self.rdepends: dict[Package, set[str]] = {}
        # list of lines to log RPM's unresolved reldep's
        self.unresolved: list[str] = []

class SrpmData:
    "Data about a given SRPM and the RPMs it produced"
    def __init__(self, srpm: Package, bin_dbs: dict[str, dnf.Base], bridge_data: BridgeData):
        self.srpm = srpm
        self.srpmname = f"{srpm.name}-{srpm.version}-{srpm.release}.src.rpm" # no epoch here
        self.arch_rpmdata = {
            arch: ArchRpmData(self, bin_db, bridge_data=bridge_data)
            for arch, bin_db in bin_dbs.items()
        }

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
NAME_SANITIZER = str.maketrans(" ():", "____")

class BridgeData:
    def __init__(self, bin_dbs: dict[str, dnf.Base]):
        self.bin_dbs = bin_dbs
        self.arch_providers_cache: dict[str, dict[hawkey.Reldep, str | None]] = {}
        self.packages: list[SrpmData] = []
        self.per_repo_rpms_src: list[list[Package]] = []
        self.virtual_providers: dict[str, list[Package]] = {}
        self.virtual_provides: dict[Package, list[str]] = {}
        # list of lines to log overall unresolved reldep's
        self.unresolved: list[hawkey.Reldep] = []
        self.allowed_mismatched_checksums: list[str] = []
        self.config: dict | None = None
        self.layer: Path | None = None

    def __repr__(self) -> str:
        return (f'<BridgeData for "{self.layer}", {len(self.packages)} packages,'
                f' {len(self.unresolved)} unresolved>')

    def packages_named(self, name: str) -> list[Package]:
        return [p for p in self.packages if p.srpm.name == name]

    def insert_pkg(self, pkg: Package) -> None:
        """Record a new `SrpmData` for `pkg`, resolving Requires into RDEPENDS.
        """
        srpm_data = SrpmData(pkg, self.bin_dbs, self)

        if pkg.name not in self.allowed_mismatched_checksums:
            handled = [p.srpm for p in self.packages_named(pkg.name)]
            assert not handled, f"{pkg!r} previously handled as {handled}"
        self.packages.append(srpm_data)

    def resolve_pkgs(self) -> None:
        for arch, bin_db in self.bin_dbs.items():
            assert arch not in self.arch_providers_cache
            self.arch_providers_cache[arch] = {}
            for srpm_data in self.packages:
                logging.debug("> %s %s", arch, srpm_data.srpmname)
                rpmdata = srpm_data.arch_rpmdata[arch]
                for binpkg in rpmdata.rpms:
                    logging.debug(">> %s %s", arch, binpkg)

                    rel: hawkey.Reldep
                    rpmdata.rdepends[binpkg] = set()
                    for rel in binpkg.requires:
                        self.__resolve(arch, bin_db, rel)
                        provider = self.arch_providers_cache[arch][rel]
                        if provider:
                            rpmdata.rdepends[binpkg].add(provider)
                        else:
                            rpmdata.unresolved.append(f"{binpkg.name}: {rel}")

    def __resolve(self, arch: str, bin_db: dnf.Base, rel: hawkey.Reldep) -> None:
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
            self.arch_providers_cache[arch][rel] = None
            logging.debug("Ignoring unresolvable-after-dropping-obsolete-rpms dependency '%s'", rel)
            self.unresolved.append(rel)
            return
        providers = newproviders
        if flag:
            logging.debug("'%s' now provided by: %s", rel, providers)

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

def recipe_supports_arch(arch: str, repo_config: dict) -> bool:
    return "archs" not in repo_config or arch in repo_config["archs"]

def write_recipe(srpm_data: SrpmData, recipesdir: Path, repo_config: dict, bridge_data: BridgeData
                 ) -> None:
    """Write a .bb file from info collected in SrpmData object.
    """
    pkg = srpm_data.srpm
    fname = f"{pkg.name}_{f'{pkg.epoch}:' if pkg.epoch else ''}{pkg.version}-{pkg.release}.bb"
    with open(recipesdir / fname, "w") as r:
        maybeepochline = f'PE = "{pkg.epoch}"\n' if pkg.epoch else ""
        arch_packages = {arch: [rpm.name for rpm in arch_rpmdata.rpms]
                         for arch, arch_rpmdata in srpm_data.arch_rpmdata.items()
                         if recipe_supports_arch(arch, repo_config)}
        arch_packages_lines = format_arch_dependent_list('PACKAGES', arch_packages)

        print(f"""# File generated by {os.path.basename(sys.argv[0])}, do not modify

inherit dnf-bridge

PN = "{pkg.name}"
{maybeepochline}PV = "{pkg.version}"
PR = "{pkg.release}"
{arch_packages_lines}
""", end='', file=r)

        if "archs" in repo_config:
            print(f'COMPATIBLE_MACHINE = "{" ".join(repo_config["archs"])}"', file=r)

        url = (pkg.remote_location(schemes=["https", "http", "file"])
               .replace(repo_config["basesrcurl"], f"${{{repo_config["basesrcurl_bbvar"]}}}"))
        print(f'\nURI_src = "{url};name=src;unpack=0"',
              file=r)
        print(f'SRC_URI = "${{URI_src}}"', file=r)
        assert pkg.chksum[0] == hawkey.chksum_type("sha256")
        print(f'SRC_URI[src.sha256sum] = "{pkg.chksum[1].hex()}"', file=r)

        for arch, arch_rpmdata in srpm_data.arch_rpmdata.items():
            if not recipe_supports_arch(arch, repo_config):
                continue

            if arch_rpmdata.unresolved:
                print(f'''
## Requires ({arch}) that were seen as not satisfiable in original repo:
{'\n'.join(f"# - {line}" for line in sorted(arch_rpmdata.unresolved))}
''', end='', file=r)

        all_virtual_provides: dict[str, set[str]] = {}
        for arch, arch_rpmdata in srpm_data.arch_rpmdata.items():
            if not recipe_supports_arch(arch, repo_config):
                continue

            all_virtual_provides[arch] = set()
            for binpkg in arch_rpmdata.rpms:
                url = (binpkg.remote_location(schemes=["https", "http", "file"])
                       .replace(repo_config["baseurl"], f"${{{repo_config["baseurl_bbvar"]}}}"))
                print(f'\nURI_{arch}_{binpkg.name} = "{url};name={arch}_{binpkg.name};unpack=0"',
                      file=r)
                print(f'SRC_URI:append = " ${{URI_{arch}_{binpkg.name}}}"', file=r)
                assert binpkg.chksum[0] == hawkey.chksum_type("sha256")
                print(f'SRC_URI[{arch}_{binpkg.name}.sha256sum] = "{binpkg.chksum[1].hex()}"', file=r)

                if binpkg in bridge_data.virtual_provides:
                    print(f'RPROVIDES:{binpkg.name}:{arch} = "{" ".join(sorted(bridge_data.virtual_provides[binpkg]))}"', file=r)
                    all_virtual_provides[arch].update(bridge_data.virtual_provides[binpkg])

        print("", file=r)
        for rpmname, arch_rpm in srpm_data.rpms_by_arch.items():
            arch_rdeps = {
                arch: sorted(arch_rpmdata.rdepends[arch_rpm[arch]])
                for arch, arch_rpmdata in srpm_data.arch_rpmdata.items()
                if arch in arch_rpm
            }
            print(format_arch_dependent_list(f'RDEPENDS:{rpmname}', arch_rdeps), file=r)

        for arch, arch_virtual_provides in all_virtual_provides.items():
            if arch_virtual_provides:
                print(f'\nPROVIDES:append:{arch} = " {" ".join(f"rpm/{p}" for p in sorted(arch_virtual_provides))}"', file=r)

def compute_bridge_data(archs: list[str], repo_configs: list[dict]) -> BridgeData:
    dbs = {}

    with (tempfile.TemporaryDirectory() as persistdir,
          tempfile.TemporaryDirectory() as reposdir,
          tempfile.TemporaryDirectory() as cachedirs,
          tempfile.TemporaryDirectory() as varsdir):
        ## setup dnf config

        # avoid any system dnf config files
        confs: dict[str, dnf.conf.Conf] = {}
        for i in ['src'] + archs:
            confs[i] = dnf.conf.Conf()
            cachedir = os.path.join(cachedirs, i)

            conf_config_file_path = confs[i]._config.config_file_path()
            conf_config_file_path.set(value='/dev/null', priority=conf_config_file_path.getPriority())
            conf_reposdir = confs[i]._config.reposdir()
            conf_reposdir.set(conf_reposdir.getPriority(), reposdir)
            conf_persistdir = confs[i]._config.persistdir()
            conf_persistdir.set(conf_persistdir.getPriority(), persistdir)
            conf_system_cachedir = confs[i]._config.system_cachedir()
            conf_system_cachedir.set(conf_system_cachedir.getPriority(), cachedir)
            conf_varsdir = confs[i]._config.varsdir()
            conf_varsdir.set(conf_varsdir.getPriority(), varsdir)

            # necessary to solve deps against concrete files
            confs[i]._config.optional_metadata_types().getValue().push_back('load_filelists')

            dbs[i] = dnf.Base(conf=confs[i])
            if i not in ('src', dbs[i].conf.substitutions['arch']):
                dbs[i].conf.substitutions['arch'] = i
                dbs[i].conf.substitutions['basearch'] = dnf.rpm.basearch(i)

        srcrepo_sections: list[list[str]] = []
        for config in repo_configs:
            srcrepo_sections.append([])
            if "sections" in config:
                for section in config["sections"]:
                    baserepoid = f"{config['name']}-{section}"
                    srcurl = config["srcurl"].format(basesrcurl=config["basesrcurl"], section=section)
                    dbs['src'].repos.add_new_repo(f"{baserepoid}-src", confs['src'], [srcurl])
                    for arch in archs:
                        if not recipe_supports_arch(arch, config):
                            logging.info("Skipping repo %s for arch %s", baserepoid, arch)
                            continue
                        binurl = config["binurl"].format(baseurl=config["baseurl"], section=section,
                                                         arch=arch)
                        dbs[arch].repos.add_new_repo(f"{baserepoid}-{arch}", confs[arch], [binurl])
                    srcrepo_sections[-1].append(f"{baserepoid}-src")
            else:
                baserepoid = config['name']
                srcurl = config["srcurl"].format(basesrcurl=config["basesrcurl"])
                dbs['src'].repos.add_new_repo(f"{baserepoid}-src", confs['src'], [srcurl])
                for arch in archs:
                    if not recipe_supports_arch(arch, config):
                        logging.info("Skipping repo %s for arch %s", baserepoid, arch)
                        continue
                    binurl = config["binurl"].format(baseurl=config["baseurl"],
                                                     arch=arch)
                    dbs[arch].repos.add_new_repo(f"{baserepoid}-{arch}", confs[arch], [binurl])
                srcrepo_sections[-1].append(f"{baserepoid}-src")

        for arch in archs:
            dbs[arch].fill_sack(load_system_repo=False)
        dbs['src'].fill_sack(load_system_repo=False)

        # get repo metadata for all packages
        per_repo_rpms_src: list[list[Package]] = [
            sum((list(dbs['src'].sack.query().filter(latest=1, reponame=repoid).available())
                 for repoid in srcrepos), [])
            for srcrepos in srcrepo_sections
        ]
        logging.info(f"packages per repo: {[len(l) for l in per_repo_rpms_src]}")

        # incrementally build bridge data
        bridge_data = BridgeData({arch: dbs[arch] for arch in archs})

        for config, rpms_src in zip(repo_configs, per_repo_rpms_src):
            exclude_packages = config.get("exclude_packages", [])
            bridge_data.per_repo_rpms_src.append(rpms_src)
            for pkg in filter_dup_packages(rpms_src, bridge_data=bridge_data):
                if pkg.name in exclude_packages:
                    logging.info("Ignoring excluded package: %s", pkg)
                    continue
                bridge_data.insert_pkg(pkg)
        # this makes rpm-against-srpms check against all repos, but it
        # should not be a problem?
        bridge_data.resolve_pkgs()

        return bridge_data

def filter_dup_packages(l: list[Package], *, bridge_data: BridgeData) -> list[Package]:
    # prepare to index package by (rpmname, chksum)
    pkg_info = list(zip(((os.path.basename(p.remote_location()), p.chksum) for p in l),
                        l))
    # unicity of rpmname, and of (rpmname, chksum)
    unique_packages_contents = set(info for (info, _pkg) in pkg_info)
    unique_packages_by_rpmname = dict((rpmname, pkg)
                                      for ((rpmname, _chksum), pkg) in pkg_info)

    # sanity check: dups must have identical contents
    if len(unique_packages_by_rpmname) and next(iter(unique_packages_by_rpmname.values())).name in bridge_data.allowed_mismatched_checksums:
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

def write_bridge_layer(outlayer: Path, repo_config: dict, packages: list[Package],
                       bridge_data: BridgeData) -> None:
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

def do_write(bridge_data: BridgeData) -> None:
    assert bridge_data.layer
    assert bridge_data.config
    for repo_config, packages in zip(bridge_data.config["repo"], bridge_data.per_repo_rpms_src):
        write_bridge_layer(bridge_data.layer, repo_config, packages, bridge_data)

    # PREFERRED_RPROVIDER for all virtual packages

    with open(bridge_data.layer / "conf" / "default-providers.conf", "w") as f:
        for virtual, vproviders in sorted(bridge_data.virtual_providers.items(),
                                          key=lambda kv: kv[0]):
            print(f'''
# providers for {virtual}:
{"\n".join(f"# - {vprovider.name}" for vprovider in vproviders)}
PREFERRED_RPROVIDER_{virtual} ??= "{vproviders[0].name if vproviders else ''}"
''', end='', file=f)

    with open(bridge_data.layer / "conf" / "unresolved.log", "w") as f:
        print(f'''Requires that were seen as not satisfiable in original repo:

{'\n'.join(sorted(str(reldep) for reldep in bridge_data.unresolved))}
''', file=f)

if __name__ == '__main__':
    layer = do_setup()
    bridge_data = do_read(layer)
    do_write(bridge_data)

# recipe for debugging:
#
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
