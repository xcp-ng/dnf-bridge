# dnf-bridge

This is a BitBake layer providing tools to maintain a layer acting as
a bridge to a DNF repository, and classes to support that bridge
layer.

It has been initially written to maintain a bridge to AlmaLinux 10 DNF
repositories, for the build of XCP-ng 9.0.

The expected usage is to automatically and periodically update the
bridge layer, and track its evolution in a Git repository, so you can
easily select which version of it you use in a given project, and
easily switch back to a previous version when needed.

## what gets bridged

- one recipe per SRPM name (currently only the latest version of a
  given SRPM gets bridged) gets translated in to a `.bb` file
- RPMs built from that SRPM get listed as packages built by the
  recipe (in `PACKAGES`)
- the URLs for SRPM and its RPMs get into `SRC_URI` (to be downloaded
  by `do_fetch`)
- `Requires` in a RPM gets mapped into `RDEPENDS` for that RPM,
  using `PROVIDES` when needed (see details below)

### mapping Requires to RDEPENDS

Since the RPM dependency relationships can be quite complex, and has
their own semantics, we cannot just copy the values of a `Requires`
into `RDEPENDS` and `Provides` into `PROVIDES`.

Here are a few examples of complex dependencies from Almalinux 10.0 packages:
```
(weston or cage or kwin-wayland or mutter or gnome-kiosk)
(crate(nix) >= 0.24.2 with crate(nix) < 0.30.0~)
((pulseaudio-module-gsettings and sound-theme-freedesktop) if pulseaudio)
```

While some of them could be translated into bitbake relationships, it
would be a non-trivial and error-prone work.  So instead they get
resolved by DNF itself against packages available in the specificied
respositories (the result depends on the set of repositories selected
for the bridge).

If they match a single package, that package's name gets added to the
RDEPENDS.

If they match more than one, a ad-hoc "virtual package" is created,
which each matching packages explicitly RPROVIDES, and each package
requiring it RDEPENDS on.

If match zero package, the dependency will be dropped, but every such
drop will be listed both in the recipe and in `conf/unresolved.log` in
the output layer.  Some reasons for them could be (non-exhaustive list):

- an actual bug/limitation in `gen-dnf-proxy` that can fail to deal
  with some specific cases we're not aware of yet
- a conditionnal dependency dealing with older package versions,
  useless but not dropped yet (`if package < OLDVERSION`), or specific
  to another explicit distro variation (`if fedora-release-common`),
  or pertaining to optional packages not in this particular
  distro/components (`if sdubby`).  We could one day just avoid
  reporting them.
- an dependency genuinely missing, causing a package to be
  uninstallable from the repo being bridged
- a binary package without a matching source package (seen as a bug,
  not in `gen-dnf-proxy`, but rather in the repo being bridged)

## how to use

### layout recommendations

The examples below assume that your top-level project (e.g. XCP-ng 9)
has its own repository, containing its main layer as a subdirectory,
`dnf-bridge` as a submodule, and the generated bridge layer as a
submodule.  `Bitbake` is provided by a submodule in `dnf-bridge`, but
could be taken from your own submodule if you want better control on
which version to use.

Your top-level project should contain a `.templateconf` file, and a
`<yourproject>-init-build-env` script with contents like:

```sh
. .templateconf
. dnf-bridge/oe-init-build-env 
```

A sample `.templateconf` file would look like:

```sh
# Template settings
TEMPLATECONF=${TEMPLATECONF:-$OEROOT/../meta-xcpng/conf/templates/default}
```

### creation of a bridge layer

* create a Bitbake layer in a standard way, depending on the
  `dnf-bridge` layer.  It is suggested to use a dedicated Git
  repository for this layer.  FIXME: should define what
  LAYERSERIES_COMPAT is supposed to be used.

* create a `conf/dnf-bridge.toml` in that layer, to describe which DNF
  repositories you want to bridge to.  FIXME: describe this, for now
  see meta-almalinux for an example.

  To help while tuning your initial configuration, it is suggested you
  create an initial commit with just the configuration, and commit the
  generated files separately.

* run `gen-dnf-proxy` on your layer (see "syncing" section below)

### syncing the bridge layer to the DNF repo

To workaround [a bug when running dnf on non-RPM
distros](https://github.com/rpm-software-management/libdnf/issues/1757),
you will need to use a container to run the bridge-builder script there.

Here is an example for a project bridging Almalinux 10 into
`meta-almalinux`:

```sh
podman run --rm --platform linux/amd64/v2 -it \
    -v /path/to/project:/project \
    ghcr.io/almalinux/10-base:10 \
    /project/dnf-bridge/scripts/gen-dnf-proxy.py /project/meta-almalinux'
```

FIXME: is the `--platform` file actually pertinent, other than to
reuse a container image we already have?
