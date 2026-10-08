# Standard specification library (stdlib)

The standard specification library (`stdlib`) provides a curated collection of
reusable specification fragments shipped directly with `seine`. These fragments
encapsulate common distribution releases, target architectures, system
services, and build environments, allowing specifications across different
repositories to share configurations without duplicating YAML files.

---

## Referencing standard specifications

Specifications reference standard library components using the `stdlib:` URI
scheme in their `requires:` list or as command-line arguments:

```yaml
requires:
    - stdlib:debian/amd64.yml
    - stdlib:debian/bookworm.yml
    - stdlib:services/networkd.yml
```

The resolver supports both explicit file extensions (`.yml`, `.yaml`) and
extension-less paths:

```yaml
requires:
    - stdlib:debian/amd64
    - stdlib:debian/bookworm
    - stdlib:services/networkd
```

When omitting the file extension, `seine` looks for `<name>.yml` first, followed
by `<name>.yaml`.

### Resolution hierarchy

When resolving a `stdlib:<path>` reference, `seine` searches locations in the
following order:

1. **Remote build staging / job worktree**:
   `.seine-stdlib/` located in the project root or job directory. This ensures
   remote worker agents always execute against the exact standard specifications
   packaged and transmitted by the build client.
2. **Environment variable override**:
   The path pointed to by `SEINE_STDLIB_DIR`, if set. Useful for unit testing,
   CI runs, or pointing to a custom standard library directory.
3. **Development checkout**:
   The top-level `stdlib/` directory at the repository root when running `seine`
   directly from a source checkout.
4. **Debian system package installation**:
   `/usr/share/seine/stdlib/` on systems where the `seine` Debian package is
   installed.
5. **Python package resource fallback**:
   Packaged data accessed via `importlib.resources.files("seine").joinpath("stdlib")`
   when `seine` is installed as a Python wheel without system data paths.

If a requested specification cannot be found across these locations, `seine`
raises a `FileNotFoundError` listing the missing specification and all searched
directories.

---

## Standard library catalog

### Debian architectures and base

#### `stdlib:debian/amd64.yml`

* **Specification**: `stdlib:debian/amd64.yml` (or `stdlib:debian/amd64`)
* **Purpose**: Configures the target architecture as `amd64`. Provides kernel
  rebuild defaults (`flavour: amd64`), module build defaults for `nvidia-open`
  and `bcachefs`, sets the imager appliance kernel to `linux-image-amd64`, and
  adds playbook tasks (priority 800) installing GRUB EFI boot packages
  (`grub-efi-amd64`, `grub-efi-amd64-signed`) and the standard Debian kernel
  (`linux-image-amd64`, `firmware-linux-free`).
* **When to Use It**: Foundational specification for any 64-bit x86 target image
  or multi-architecture vendor configuration.
* **Extension Points**: The `defaults.packages` definitions can be overridden or
  amended by concrete `packages:` entries in top-level specifications. The imager
  kernel can be replaced under `imager.kernel`.
* **Relevant Examples**: `examples/pc-image/main.yaml`,
  `examples/minimal-uki-image/main.yaml`, `examples/vendor/amd64.yaml`.
* **Limitations**: Targets `amd64` architecture only.
* **Vault / Credentials Entries**: None.

#### `stdlib:debian/arm64.yml`

* **Specification**: `stdlib:debian/arm64.yml` (or `stdlib:debian/arm64`)
* **Purpose**: Configures the target architecture as `arm64`. Provides kernel
  rebuild defaults (`flavour: arm64`), module build defaults for `nvidia-open`
  and `bcachefs`, and sets the imager appliance kernel to `linux-image-arm64`.
* **When to Use It**: Foundational specification for any 64-bit ARM (AArch64)
  target image or multi-architecture vendor configuration.
* **Extension Points**: Board-specific bootloader and firmware packages
  (such as U-Boot, Raspberry Pi firmware, or device trees) are layered on top
  by board-specific specifications.
* **Relevant Examples**: `examples/rpi4-image/main.yaml`,
  `examples/vendor/arm64.yaml`.
* **Limitations**: Targets `arm64` architecture only. Unlike `amd64.yml`, it does
  not include a default GRUB playbook because ARM boards typically require
  board-specific firmware and boot configurations.
* **Vault / Credentials Entries**: None.

#### `stdlib:debian/base.yml`

* **Specification**: `stdlib:debian/base.yml` (or `stdlib:debian/base`)
* **Purpose**: Establishes minimal Debian packaging hygiene by setting
  `defaults.apt.install_recommends: false`. Installs the minimal Debian `init`
  metapackage via an early playbook task (priority 100).
* **When to Use It**: Universal base for Debian systems. Required automatically
  by suite specifications such as `stdlib:debian/bookworm.yml` and
  `stdlib:debian/trixie.yml`.
* **Extension Points**: Recommended packages can be re-enabled per-specification
  or per-package by setting `apt.install_recommends: true`.
* **Relevant Examples**: `stdlib:debian/bookworm.yml`,
  `stdlib:debian/trixie.yml`.
* **Limitations**: Intended for Debian-derived systems using APT.
* **Vault / Credentials Entries**: None.

#### `stdlib:debian/feeds.yml`

* **Specification**: `stdlib:debian/feeds.yml` (or `stdlib:debian/feeds`)
* **Purpose**: Declares official Debian repository feeds (main suites, updates,
  and security suites) for supported releases (`bookworm` and `trixie`).
* **When to Use It**: Required by suite specifications (`bookworm.yml`,
  `trixie.yml`) or directly by `seine vendor` when vendoring packages without
  loading base distribution tasks.
* **Extension Points**: Additional custom feeds, local repositories, or
  alternative mirrors can be added in `distribution.feeds`.
* **Relevant Examples**: `examples/vendor/main.yaml`,
  `stdlib:debian/bookworm.yml`, `stdlib:debian/trixie.yml`.
* **Limitations**: Declares official Debian upstream mirrors.
* **Vault / Credentials Entries**: None.

#### `stdlib:debian/bookworm.yml`

* **Specification**: `stdlib:debian/bookworm.yml` (or `stdlib:debian/bookworm`)
* **Purpose**: Sets `distribution.release: bookworm`, requires
  `stdlib:debian/base.yml` and `stdlib:debian/feeds.yml`, and declares the
  `bookworm-backports` repository feed.
* **When to Use It**: Standard suite fragment for Debian 12 (Bookworm) images.
* **Extension Points**: Backports feed is configured with `NotAutomatic`, so
  packages from backports are only pulled when explicitly targeted by version or
  target release.
* **Relevant Examples**: `examples/pc-image/main.yaml`,
  `examples/rpi4-image/main.yaml`, `examples/minimal-uki-image/main.yaml`.
* **Limitations**: Locked to Debian 12 (Bookworm).
* **Vault / Credentials Entries**: None.

#### `stdlib:debian/trixie.yml`

* **Specification**: `stdlib:debian/trixie.yml` (or `stdlib:debian/trixie`)
* **Purpose**: Sets `distribution.release: trixie`, and requires
  `stdlib:debian/base.yml` and `stdlib:debian/feeds.yml`.
* **When to Use It**: Standard suite fragment for Debian 13 (Trixie) images.
* **Extension Points**: Inherits repository and package configuration from
  `base.yml` and `feeds.yml`.
* **Relevant Examples**: Target specifications building Trixie-based systems.
* **Limitations**: Locked to Debian 13 (Trixie).
* **Vault / Credentials Entries**: None.

---

### System configuration

#### `stdlib:debian/locales.yml`

* **Specification**: `stdlib:debian/locales.yml` (or `stdlib:debian/locales`)
* **Purpose**: Restricts generated locales to English via `overrides.locales: [en]`,
  stripping unused locale data to minimize disk image size.
* **When to Use It**: Embedded systems and appliances that only require English
  system messages and standard locale databases.
* **Extension Points**: Additional language codes (such as `de`, `fr`, `ja`) can
  be appended under `overrides.locales`.
* **Relevant Examples**: Minimal and embedded disk image specifications.
* **Limitations**: Debian locale generation.
* **Vault / Credentials Entries**: None.

#### `stdlib:debian/timezone.yml`

* **Specification**: `stdlib:debian/timezone.yml` (or `stdlib:debian/timezone`)
* **Purpose**: Configures the system timezone to UTC (`Etc/UTC`) using
  `community.general.timezone`. On Debian Trixie (where `hwclock` was split out
  of `util-linux`), it automatically installs `util-linux-extra`.
* **When to Use It**: Universal default for embedded devices, appliances, and
  servers running in UTC.
* **Extension Points**: Can be overridden by specifying a different timezone in
  a subsequent playbook task.
* **Relevant Examples**: `examples/pc-image/main.yaml`.
* **Limitations**: Runs at playbook priority 900.
* **Vault / Credentials Entries**: None.

#### `stdlib:debian/sysctl.yml`

* **Specification**: `stdlib:debian/sysctl.yml` (or `stdlib:debian/sysctl`)
* **Purpose**: Injects kernel sysctl parameters tuned for appliance workloads
  (lowers `vm.swappiness` to `10`). Configures parameters using
  `ansible.posix.sysctl` with `sysctl_set: false` and `reload: false` so that
  configuration files under `/etc/sysctl.d/` are written cleanly without
  attempting to write to `/proc/sys` inside unprivileged container build roots.
* **When to Use It**: Systems with flash memory or solid-state storage where
  reduced swap aggressiveness is preferred.
* **Extension Points**: Additional sysctl keys can be declared using
  `ansible.posix.sysctl` tasks in application playbooks.
* **Relevant Examples**: Appliance images.
* **Limitations**: Playbook priority 900. Applied on first target boot.
* **Vault / Credentials Entries**: None.

---

### Services

#### `stdlib:services/networkd.yml`

* **Specification**: `stdlib:services/networkd.yml` (or `stdlib:services/networkd`)
* **Purpose**: Installs `systemd-resolved` and `libnss-resolve` (priority 500),
  enables `systemd-networkd` and `systemd-resolved`, creates the `/etc/resolv.conf`
  symlink to the resolved stub, and deploys `80-wired.network` to enable DHCP,
  IPv6 router advertisements, and link-local addressing on wired interfaces
  (`en*`, `eth*`) at priority 800.
* **When to Use It**: Standard dynamic wired networking setup for systemd-based
  appliances and embedded systems.
* **Extension Points**: Custom static IP configurations or wireless network units
  can be added by placing additional `.network` files in `/etc/systemd/network/`.
* **Relevant Examples**: `examples/pc-image/main.yaml`,
  `examples/rpi4-image/main.yaml`.
* **Limitations**: Requires systemd init.
* **Vault / Credentials Entries**: None.

#### `stdlib:services/sshd.yml`

* **Specification**: `stdlib:services/sshd.yml` (or `stdlib:services/sshd`)
* **Purpose**: Installs `openssh-server` (priority 300) and hardens its configuration
  using the companion `sshd_config` Ansible module in `stdlib/services/library/`.
  Disables root login, disables password authentication, restricts ciphers, and
  removes postinst-generated host keys so unique keys are generated on first boot.
* **When to Use It**: Deploying an SSH daemon configured for key-based authentication.
* **Extension Points**: Additional SSH options can be configured using the
  `sshd_config` Ansible module provided by this specification.
* **Relevant Examples**: `examples/pc-image/main.yaml`.
* **Limitations**: Disables password authentication; requires SSH authorized keys
  to be deployed or active console access.
* **Vault / Credentials Entries**: None.

---

### Language and toolchain extensions

#### `stdlib:extends/golang.yml`

* **Specification**: `stdlib:extends/golang.yml` (or `stdlib:extends/golang`)
* **Purpose**: Configures standard Go toolchain version (`1.26.7`) and
  architecture-specific SHA-256 checksums for `amd64` and `arm64`. Enables
  DEP-5 license scanning for vendored Go modules (`license-scan: true`).
* **When to Use It**: When building custom Go packages from source using
  `extends: go`.
* **Extension Points**: `defaults.extends.go.toolchain`, `toolchain-sha256`, and
  `license-scan` can be overridden per package or per specification.
* **Relevant Examples**: Specifications compiling Go daemons or utilities.
* **Limitations**: Preconfigured hashes provided for `amd64` and `arm64`.
* **Vault / Credentials Entries**: None.

---

## Remote builds and reproducibility

When building remotely (`seine build --remote <server> ...`), the client computes
the full specification dependency closure before staging the worktree:

1. Every standard specification loaded via `stdlib:` (or located inside the
   active standard library directory) is recognized by `is_stdlib_path()`.
2. The referenced standard library YAML files and any co-located assets (such
   as custom Ansible modules in `library/`) are packaged into the `.tar.zst`
   worktree archive under `.seine-stdlib/`.
3. When the worker agent unpacks the archive into its execution directory, it
   sets `SEINE_STDLIB_DIR = os.path.join(job_dir, ".seine-stdlib")` in the child
   environment.

Because the child `seine build` process on the worker resolves `.seine-stdlib/`
first, remote builds always execute against the exact standard specifications
sent by the client. This guarantees reproducibility across workers, even if a
worker has an older, newer, or differing system `seine` package installed.

---

## Contribution guidelines

When adding or updating specifications in `stdlib/`:

* **General applicability**: Standard specifications must solve universal
  problems applicable to many projects. Project-specific or board-specific
  definitions belong in project repositories or `examples/`.
* **No credentials or secrets**: Standard specifications must never contain
  hardcoded passwords, test credentials, hashes, private keys, or certificates.
* **Self-contained assets**: Any companion files (such as Ansible action plugins
  or helper modules) must live in a co-located `library/` folder alongside the
  specification.
* **Documentation required**: Every specification added to `stdlib/` must be
  documented in this catalog following the standard headings.
* **Automated verification**: Add test coverage under `tests/build/` verifying
  that the specification resolves and merges without error.
