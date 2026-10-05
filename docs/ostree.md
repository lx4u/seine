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

1. Unpack the tarball onto a throwaway stage disk. The target's own
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

With `--reproducible`, the partitions are then rebuilt like any other
ext4 partition, so two builds of the same specification give a
byte-identical image. The commit itself does not depend on the build
time. The rebuild keeps the hardlinks between the repository and the
deployments. It does not keep the immutable flag `deploy` puts on each
deployment root, which ostree sets again on the next deploy.

The commit carries an empty `machine-id` and no SSH host keys, so
every device creates its own. A root file system that ships host keys
is refused.

## Requirements

The root file system needs `dracut`, `ostree` and `ostree-boot`, and no
`initramfs-tools`. Install them from a playbook before the kernel, as
`examples/minimal-initrd/main.yaml` does for dracut. The build stops
before any disk work if one is missing.

## Disk size

Partitions without a `size:` get room for the repository plus one more
full deployment, so the first update does not fail on a full disk. Set
`size:` to trade that room for a smaller image.

## Not supported yet

Writing a boot loader, `composefs`, `containers:`, `bootlets:` and read-only
(`squashfs`/`erofs`) partitions.
