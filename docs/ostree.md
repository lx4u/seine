# OSTree sysroots

By default the imager unpacks the root file system onto `/`. With
`image: ostree:` it builds an [OSTree](https://ostreedev.github.io/ostree/)
sysroot instead: the root file system becomes a commit in a repository
on the root partition, and a deployment of that commit is what boots.
See [ostree](specification.md#ostree) for the attributes.

Only `mode: standard` builds a disk today.

## Partitions

OSTree only versions `/usr` and the defaults in `/usr/etc`. Everything
else lives in real partitions that survive updates:

| Mount    | Role                                                   |
| -------- |--------------------------------------------------------|
| `/`      | Physical sysroot: the repository and the deployments   |
| `/var`   | Persistent state, seeded once from the commit          |
| `/efi`   | ESP, as usual                                          |
| `/boot`  | Optional, `ext4` only: OSTree cannot deploy to `vfat`  |

`/home`, `/opt`, `/srv`, `/root`, `/mnt` and `/usr/local` are links
into `/var` in the commit, so they cannot be mounts. Put a data
partition under `/var` instead, for instance `where: /var/home`.

## What the imager does

For each root file system, in the imager appliance:

1. Unpack the tarball onto the imager's scratch disk. The target's own
   `ostree` runs from there, so the commit is made by the version the device
   will run later.
2. Reshape the tree: usr-merge links, an empty `/sysroot`, links into
   `/var`, `/etc` moved to `/usr/etc`, the kernel and initramfs next to
   their modules. Content already in `/root` or `/usr/local` seeds the
   new `/var` on first boot.
3. Write `/etc/fstab` with the mounts other than `/` (`/var`, `/efi`,
   and so on): without those lines the `/var` partition and the ESP are
   never mounted. Each mount point outside `/var` is created in the
   commit, and nested ones under `/var` by `tmpfiles.d`.
4. Mount the partitions under `/sysroot`, then `ostree admin init-fs`,
   `os-init`, `commit` and `deploy`. The deployment gets the kernel
   arguments `root=PARTUUID=<root partition> rw`, plus those of
   `GRUB_CMDLINE_LINUX` and `GRUB_CMDLINE_LINUX_DEFAULT` in
   `/etc/default/grub` if the root file system has one. They stay in the
   boot entry, and later deployments inherit them.
   The repository is set to `sysroot.bootloader none`: the imager writes
   the boot configuration, not ostree, which would otherwise try to run
   `grub-mkconfig` on a deploy and fail.
5. Install the boot loader into the ESP when the root file system has
   one: GRUB (`grub-efi-amd64`) or systemd-boot (`systemd-boot`), both
   without an NVRAM entry. The boot loader runs from the root file
   system's own tools, so install it like for any image, from a
   playbook.

   GRUB (`grub-install --removable`): write `grub/grub.cfg` there from
   the boot entries `deploy` wrote. Each entry names its root file
   system by UUID, so one `grub.cfg` boots every sysroot of a
   multiconfig disk; the group named by `imager: boot` is the first
   entry, and the others are prefixed with their stateroot. Debian's `15_ostree` is not used: it leaves out the
   `/boot` prefix, and `grub-mkconfig` cannot run from the staged tree.

   systemd-boot (`bootctl install`): it cannot read ext4, so the ESP
   also gets the kernel and initramfs of every sysroot, and the boot
   entries `deploy` wrote. Entry file names and, with several sysroots,
   titles are prefixed with the stateroot. `loader.conf` makes the
   newest entry of the group named by `imager: boot` the default. The
   build stops if those files do not fit: give the ESP 256 MiB, enough
   for a few kernels. A deployment that an update adds later needs its
   files copied to the ESP as well, which is the updater's job.

6. A UKI in the root file system (`/boot/EFI/Linux/*.efi`, from
   `extends: uki:` or a playbook) is taken out of the commit and rebuilt
   after `deploy`: the kernel arguments of the deployment are added to
   its command line, and it is signed if `image: secure-boot:` is set.
   Its `ostree=` names a link, `/ostree/<stateroot>-<commit>`, that the
   imager creates to the deployment, instead of the `boot.<N>` path of
   the boot entry.
   It lands on the ESP as `EFI/Linux/<stateroot>-<name>.efi`. The
   sysroot then gets no boot entry of its own and no kernel files on the
   ESP: systemd-boot finds the UKI by itself, and GRUB gets a menu entry
   that chainloads it. A root file system with a UKI needs a boot loader.

   With `image: ostree: version:` set, the imager first writes
   `IMAGE_VERSION=<version>` into `/usr/lib/os-release` of the tree, so
   the commit and the booted system carry it, and the commit gets a
   `version` metadata key. The UKI takes that os-release (systemd-boot
   sorts by it) and lands on the ESP as
   `EFI/Linux/<stateroot>-<version>.efi`. The root file system must ship
   exactly one UKI then. Its digest does not change.

   The link stays valid when an update flips `boot.<N>`, so the UKI
   keeps booting its deployment, and a rollback to it works. The name
   does not pin the commit: `ostree-prepare-root` does not check it. A
   new deployment is not covered by this UKI: the updater builds a UKI
   and creates a link for each new commit.

With `--reproducible`, the partitions are then rebuilt like any other
ext4 partition, so two builds of the same specification give a
byte-identical image. The commit itself does not depend on the build
time. The rebuild keeps the hardlinks between the repository and the
deployments. It does not keep the immutable flag `deploy` puts on each
deployment root, which ostree sets again on the next deploy.

The commit carries an empty `machine-id` and no SSH host keys, so
every device creates its own. A root file system that ships host keys
is refused.

## Disk identifiers

A device is updated, not flashed again, so a later build must keep the
GUIDs and UUIDs of the disk, its partitions and its file systems (the
serial of the ESP too): `root=PARTUUID=` in the UKI has to find the
device, and the fstab of an update has to find the ESP. They come from the
layout and the product: the `image:` section without `filename`,
`version`, `payload`, `manifest-key` and `gpg-key`, and the
architecture. A new release, another file name or other packages keep
them. A change of the partitions, the table, `mode`, `stateroot` or
`ref` gives new ones, and the devices must be flashed again. Without
ostree the identifiers follow the whole specification.

## Signing the commit

Set `gpg-key: vault:<name>` under `image: ostree` (or in a `sources:`
entry) to sign the commit with a pgp key of the vault. The imager signs
the commit object with the build time and stores the signature in the
`.commitmeta` file next to it, as `ostree gpg-sign` does. Use a key of
its own, not the one that signs an apt repository. Two builds with the
same key stay byte-identical. `examples/common/dev-ostree-key.yaml`
holds a fixed key for development only (`ostree-commits`). The
[vault guide](vault-openbao.md) shows how to create a real one.

The device checks it with a remote that has `gpg-verify` (see the update
fragment below).

## The update payload

With `version:` set under `image: ostree`, the build also writes what a
device updates from, into a directory next to the disk image (see
`docs/specification.md` for its name and `payload:`):

* `repo/`: an `archive` ostree repo with the commit of the build. The
  moving ref (`debian/amd64`) goes to the new commit, and the ref
  `debian/amd64.v<version>` keeps it. The summary and its signature
  (`summary.sig`) are signed with `gpg-key`, so a remote with
  `gpg-verify-summary` accepts them. The summary holds a time, so it
  differs between builds of the same image; commits and objects do not.
* `uki/`: the UKI of each version, named `<stateroot>-<version>.efi`, and
  the list `SHA256SUMS` of all of them with `SHA256SUMS.gpg`, signed with
  `manifest-key`. This is what `systemd-sysupdate` reads. The list is
  written last.

A later build with a higher version adds to the same directory: devices
keep the objects they have, and the older UKIs stay available. The build
is refused for a lower version, and for the same version with another
commit. A rebuild of the same version and commit is accepted.

The UKI is the one of the image, with the command line of the
deployment. A device can boot it because `root=PARTUUID=` does not change
between builds of the same layout. A build whose command line differs
(outside `ostree=`) from the newest UKI of the directory is refused: that
needs a reflash.

`deltas-from` lists versions that devices run in the field. The build
makes a static delta from each to the new version, so a device on one of
them downloads less. A device on another version still updates, from
single objects.

The commit is exported to a small archive repo by the target's own
`ostree`, on the scratch disk, and only that repo leaves the imager
appliance. The bare repo of the sysroot is never copied out: ownership is
lost on the way and the copy is corrupt.

## The update script

`examples/common/ostree-update/seine-update` is a shell script that a
device runs to apply an update: it pulls the new commit, deploys it, and
adds the UKI of the new version to the ESP last, because the UKI is what
the boot loader picks. A crash before that step leaves the old system
untouched. It needs `systemd-sysupdate` (for the UKI), `ostree` and
`bootctl`.

```
seine-update             one update run
seine-update reconcile   repair what a cut run left behind, then check
seine-update check       only check the invariants
```

It reads `/etc/seine-update.conf` (shell syntax): `STATEROOT`, `REF`,
`REMOTE` and `ESP` are required. `KEEP_FAILED=1` keeps the files of a
failed update for diagnosis.

Exit code 0 means done, nothing new, waiting or busy. Exit code 1 means
a failure, which also shows in the unit that runs the script. Only one
run happens at a time (`/run/seine-update.lock`). A run waits while a
UKI still has boot tries counted: the last update has not had its good
boot yet.

`/var/lib/seine-update/status` is rewritten after every run, whole, with
four lines: `time` (epoch), `result`, `version` and `message`. The
`result` is `ok`, `nothing`, `waiting`, `busy`, `failed` or
`failed-update` (see below).

The script only removes files and deployments through two functions that
keep the last-known-good, the booted and the pinned deployment. The
last-known-good is the blessed UKI with the highest version whose
deployment still exists, together with that deployment. The script pins
that deployment in ostree and never removes that UKI. Before a deploy it
removes the other UKIs, because the deploy drops the old rollback
deployment. After a good update the device keeps the new last-known-good
and the previous one as the rollback.

A UKI that ran out of boot tries (`+0-N`) is a failed update. At the next
run, or at boot with `reconcile`, its version goes to
`/var/lib/seine-update/failed`, the UKI, its deployment and its link are
removed, and the status says `failed-update`. A version on that list is
not fetched again: ship a fix with a higher version. The list is the
durable record, the status line is replaced by the next run.

If `bootctl` or `ostree admin status` print nothing, the script deletes
nothing and stops: what it cannot read is not treated as gone.

The script never reboots the device: that policy belongs to the image.

Tests can set `SEINE_UPDATE_CONF`, `SEINE_UPDATE_ESP`,
`SEINE_UPDATE_SYSROOT`, `SEINE_UPDATE_STATE`, `SEINE_UPDATE_LOCK` and
`SEINE_UPDATE_SYSUPDATE`, `SEINE_UPDATE_OSTREE`, `SEINE_UPDATE_BOOTCTL`
to run it on a temporary tree with stand-in commands. With
`SEINE_UPDATE_FAILPOINT=<step>` the script stops dead after that step
(`kill`, the default) or sleeps (`SEINE_UPDATE_FAILPOINT_ACTION=hold`),
so a test can cut the power. The steps are `after-elect`, `after-pull`,
`after-prune-ukis`, `after-deploy`, `after-link`, `after-prune`,
`after-uki`, `after-bind`, and, when a failed update is cleaned up,
`after-denylist` and `after-undeploy`. Leave the variable unset on a
device.

## Updating a device

`examples/common/ostree-update.yaml` is a fragment that makes an image
update itself. It installs the update script, its units and the public
keys, and it enables the timer. The including specification sets where
the updates are served and which stateroot and ref the image uses:

```
requires:
    - ../common/ostree-update
update:
    url: http://updates.example.com/pc
image:
    ostree:
        stateroot: debian
        ref: debian/amd64
```

The URL is baked into the image, so changing it changes the root file
system. The build stops when it is empty.

What the fragment puts in the image:

* `/usr/libexec/seine-update/seine-update`, and `seine-update.service`
  with its timer. The timer starts a run 15 minutes after boot and then
  every hour. A failed run is tried again after 10 minutes, three times
  in six hours.
* `seine-update-reconcile.service`, which runs `seine-update reconcile`
  at boot, before the first update run. It does not wait for the good
  boot mark: the next run moves the last-known-good pin.
* the remote `seine` in `/etc/ostree/remotes.d/`, which checks the
  signature of every commit and of the summary, and the key it checks
  them with in `/usr/share/ostree/trusted.gpg.d/`.
* `/etc/sysupdate.d/50-uki.transfer`, which names the UKI directory of
  the server, and `/etc/systemd/import-pubring.gpg`, the key that checks
  the signed list of files (`SHA256SUMS`).
* `/etc/seine-update.conf`, read by the script.
* a drop-in that resets a boot that never completes after five minutes,
  and a watchdog setting that resets a hung system. Both count as a
  failed try of the new UKI.
* `systemd-boot-check-no-failures.service`, enabled, so a boot with a
  failed unit is not marked good. `systemd-sysupdate.timer` and
  `systemd-sysupdate-reboot.timer` are masked: the script is what runs
  `systemd-sysupdate`, and it never reboots.

The two public keys are the halves of the development keys
`dev-ostree-key.yaml` and `dev-update-manifest-key.yaml`, which sign
the commit and the list of files. They are for development only. A
project exports its own public keys and replaces both files in the
fragment's directory, or copies the fragment.

## Requirements

The root file system needs `dracut`, `ostree` and `ostree-boot`, and no
`initramfs-tools`. Install them from a playbook before the kernel:
`examples/pc-ostree-image/ostree.yaml` does it, and `main.yaml` next to
it is a bootable PC disk built on it. The build stops before any disk
work if one is missing.

With a boot loader in the root file system, the disk also needs an ESP
mounted at `/efi`, in the group named by `imager: boot` for a multiconfig
disk.

## Disk size

Partitions without a `size:` get room for the repository plus one more
full deployment, so the first update does not fail on a full disk. Set
`size:` to trade that room for a smaller image.

## Not supported yet

Booting on a non-amd64 architecture (the build stops with a message),
mixing ostree and plain root file systems on one disk, a separate
`/boot` partition (its entries are written, but it has not been
boot-tested), `composefs`, `containers:`, `bootlets:` and read-only
(`squashfs`/`erofs`) partitions.
