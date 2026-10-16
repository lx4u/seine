# PC OSTree Update Image

This example demonstrates a self-updating PC disk image with an OSTree
root file system, a signed Unified Kernel Image (UKI) booted by
systemd-boot, and reproducible update payloads with static deltas.

## Architecture

The example demonstrates multi-version lifecycle updates across several
revisions:

1. **Version 1 (Factory Baseline)** (`main.yaml`):
   The initial installation image. It writes the bootable disk image
   (`pc-ostree-update.img`) and initializes the update payload repository
   (`pc-ostree-update-payload/`) with UKI `debian-1.efi` and commit `1`.

2. **Version 2 (Feature Update with Static Delta)** (`v2.yaml`):
   An overlay on top of `main.yaml`. It bumps `image.ostree.version` to `"2"`,
   adds packages (`htop`, `strace`), and generates a static delta against
   version 1 via `deltas-from: ["1"]`.

3. **Version 3 (Subsequent Update with Multi-Hop Deltas)** (`v3.yaml`):
   An overlay bumping `image.ostree.version` to `"3"`, adding `chrony`, and
   generating static deltas against both previous releases via
   `deltas-from: ["1", "2"]`. This allows devices running version 1 to jump
   directly to version 3 (long jump) and devices running version 2 to apply
   a smaller incremental update (stepwise).

4. **Version 4 (Daemon Addition)** (`v4.yaml`):
   An overlay bumping version to `"4"`, installing `htop`, `strace`, and
   `chrony`, and generating a static delta from version 3.

5. **Version 5 (Poisoned Release & Watchdog Rollback)** (`v5.yaml`):
   An overlay bumping version to `"5"`, introducing a failing systemd
   service that triggers hardware watchdog reboot exhaustion and automated
   rollback to the last-known-good release (version 4).

6. **Version 5.1 (Hotfix Release)** (`v5.1.yaml`):
   An overlay bumping version to `"5.1"`, pruning `htop`, removing the
   failing service, and generating a static delta from version 4.

7. **Version 9 (Final Application Release)** (`v9.yaml`):
   An overlay bumping version to `"9"`, installing `iperf3`, custom app
   binary `/usr/bin/myapp`, and a `timesyncd` drop-in. Generates static deltas
   against baseline version 1 (long jump) and version 5.1 (stepwise).

### Payload Directory Layout

Each build exports update artifacts into `build/deploy/<release>/pc-ostree-update-payload/`:

* `uki/`: contains the unified kernel images (`debian-1.efi`, `debian-2.efi`,
  `debian-3.efi`), `SHA256SUMS`, and detached signature `SHA256SUMS.gpg`.
* `repo/`: the OSTree archive-z2 repository containing commits, refs,
  signed summary index (`summary`, `summary.sig`), and compressed static
  deltas under `repo/deltas/`.

---

## Step-by-Step CLI Walkthrough

### 1. Build the Baseline Image (Version 1)

```bash
seine build examples/pc-ostree-update-image/main.yaml
```

This creates:
* `build/deploy/trixie/pc-ostree-update.img`: the bootable raw GPT disk image.
* `build/deploy/trixie/pc-ostree-update-payload/`: the update payload repository.

### 2. Boot the Baseline Image in QEMU

Launch the image with UEFI firmware:

```bash
OVMF=/usr/share/OVMF/OVMF_CODE.fd
qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 \
    -drive if=pflash,format=raw,readonly=on,file=$OVMF \
    -drive file=build/deploy/trixie/pc-ostree-update.img,format=raw \
    -net nic -net user \
    -nographic
```

Log in with user `root` and password `welcome123`. Check the active version:

```bash
grep IMAGE_VERSION /etc/os-release
```

### 3. Start the Payload Server on the Host

In another host terminal, serve the payload directory on port 8000 (accessible
as `http://10.0.2.2:8000` from the QEMU guest):

```bash
python3 -m http.server 8000 --directory build/deploy/trixie/pc-ostree-update-payload
```

### 4. Build Version 2 with Static Delta

Apply the `v2.yaml` overlay:

```bash
seine build examples/pc-ostree-update-image/main.yaml examples/pc-ostree-update-image/v2.yaml
```

The build compiles UKI `debian-2.efi`, commits the updated tree to the
payload repository, and computes the static delta `1 -> 2`.

### 5. Apply the Update on the Target

Inside the running QEMU guest:

```bash
/usr/libexec/seine-update/seine-update
systemctl reboot
```

After rebooting, log in and verify version 2 and the new tools:

```bash
grep IMAGE_VERSION /etc/os-release
which htop strace
```

### 6. Build Version 3 with Stepwise and Long-Jump Deltas

Apply the `v3.yaml` overlay:

```bash
seine build examples/pc-ostree-update-image/main.yaml examples/pc-ostree-update-image/v3.yaml
```

Static deltas `1 -> 3` and `2 -> 3` are generated. A device running
either version 1 or version 2 will pull the smallest delta when updating.

---

## Interactive Walkthrough in `seine tui`

You can also inspect and drive this workflow using the terminal interface:

1. **Launch the TUI**:
   ```bash
   seine tui
   ```

2. **Load the Baseline Specification**:
   Enter `/use examples/pc-ostree-update-image/main.yaml` in the command prompt.
   The `/overview` screen displays the partition layout and OSTree settings.

3. **Build the Disk and Payload**:
   Press `Ctrl+B` or run `/build`. The task runner compiles the rootfs, builds
   the UKI, and creates the baseline payload.

4. **Inspect Generated Artifacts**:
   Navigate to `/artifacts` (`▣` icon) to inspect the disk image and explore
   the contents of `pc-ostree-update-payload/` (UKIs, commits, and manifests).

5. **Switch to Version 2 Overlay**:
   Enter `/use examples/pc-ostree-update-image/main.yaml @examples/pc-ostree-update-image/v2.yaml`.
   Notice the version changes to `2` and the delta dependency appears.

6. **Build Update**:
   Press `Ctrl+B` (or `Ctrl+U` for payload-only mode) to generate the v2 update.

7. **Track Lineage and Deltas**:
   Switch to `/updates` (`↑` icon) to view the release timeline, commit hashes,
   and the delta matrix showing wire savings.
