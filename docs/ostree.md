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
   after `deploy`: the kernel arguments of the deployment, including
   `ostree=` with the checksum of the kernel and initramfs, are added to
   its command line, and it is signed if `image: secure-boot:` is set.
   It lands on the ESP as `EFI/Linux/<stateroot>-<name>.efi`. The
   sysroot then gets no boot entry of its own and no kernel files on the
   ESP: systemd-boot finds the UKI by itself, and GRUB gets a menu entry
   that chainloads it. A root file system with a UKI needs a boot loader.

   The UKI names the deployment of the first boot. After an update, the
   new deployment is not covered by it: keeping the UKI in step with the
   commits is the updater's job.

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
