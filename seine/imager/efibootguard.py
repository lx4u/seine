# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import struct
import tempfile
import zlib

from seine.imager.appliance import SCRATCH_DEVICE
from seine.imager.bootloader import SystemdBootBootloader

ENV_STRING_LENGTH = 255
ENV_MEM_USERVARS = 128 * 1024
BGENV_FILENAME = "BGENV.DAT"

# UEFI paths to chained systemd-boot binary on the ESP per architecture.
EBG_CHAINLOAD_KERNEL = {
    "amd64": r"\EFI\systemd\systemd-bootx64.efi",
    "arm64": r"\EFI\systemd\systemd-bootaa64.efi",
}

# Candidate source paths for the EFI Boot Guard binary inside the appliance.
EBG_EFI_BINARIES = {
    "amd64": [
        "/usr/lib/x86_64-linux-gnu/efibootguard/efibootguardx64.efi",
        "/usr/lib/x86_64-linux-gnu/efibootguard/efibootguard.efi",
        "/usr/lib/efibootguard/efibootguardx64.efi",
        "/usr/lib/efibootguard/efibootguard.efi",
    ],
    "arm64": [
        "/usr/lib/aarch64-linux-gnu/efibootguard/efibootguardaa64.efi",
        "/usr/lib/aarch64-linux-gnu/efibootguard/efibootguard.efi",
        "/usr/lib/efibootguard/efibootguardaa64.efi",
        "/usr/lib/efibootguard/efibootguard.efi",
    ],
}

EBG_ESP_REMOVABLE = {
    "amd64": "BOOTX64.EFI",
    "arm64": "BOOTAA64.EFI",
}

SYSTEMD_BOOT_FILENAME = {
    "amd64": "systemd-bootx64.efi",
    "arm64": "systemd-bootaa64.efi",
}


def encode_env_string(text, max_len=ENV_STRING_LENGTH):
    raw = text.encode("utf-16le")
    max_bytes = max_len * 2
    if len(raw) > max_bytes:
        raise ValueError(f"string exceeds maximum length of {max_len} characters")
    return raw.ljust(max_bytes, b"\x00")


def decode_env_string(raw_bytes):
    idx = 0
    while idx + 1 < len(raw_bytes):
        if raw_bytes[idx:idx + 2] == b"\x00\x00":
            break
        idx += 2
    return raw_bytes[:idx].decode("utf-16le")


def create_bgenv(kernel, revision, watchdog, ustate=0, args="", in_progress=0):
    kernel_bytes = encode_env_string(kernel)
    args_bytes = encode_env_string(args)
    header = struct.pack("<BBHI", in_progress, ustate, watchdog, revision)
    userdata = b"\x00" * ENV_MEM_USERVARS
    payload = kernel_bytes + args_bytes + header + userdata
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return payload + struct.pack("<I", crc)


def parse_bgenv(data):
    expected_size = ENV_STRING_LENGTH * 4 + 8 + ENV_MEM_USERVARS + 4
    if len(data) != expected_size:
        raise ValueError(f"invalid BGENV.DAT size: {len(data)} (expected {expected_size})")
    payload = data[:-4]
    stored_crc = struct.unpack("<I", data[-4:])[0]
    calc_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if stored_crc != calc_crc:
        raise ValueError(f"CRC32 mismatch: stored 0x{stored_crc:08x}, calculated 0x{calc_crc:08x}")

    k_end = ENV_STRING_LENGTH * 2
    p_end = k_end + ENV_STRING_LENGTH * 2
    kernel = decode_env_string(payload[:k_end])
    args = decode_env_string(payload[k_end:p_end])
    in_progress, ustate, watchdog, revision = struct.unpack("<BBHI", payload[p_end:p_end + 8])
    return {
        "kernel": kernel,
        "args": args,
        "in_progress": in_progress,
        "ustate": ustate,
        "watchdog": watchdog,
        "revision": revision,
        "crc32": stored_crc,
    }


class EfiBootGuard:
    def _initialize_bgenv(self, g, ph, part_devices):
        if not (ph.watchdog and ph.watchdog > 0):
            return
        bgenv_partitions = [p for p in ph.partitions if ph._is_bgenv(p)]
        if not bgenv_partitions:
            return
        target_arch = self.source.spec["distribution"]["architecture"]
        kernel_path = EBG_CHAINLOAD_KERNEL.get(target_arch)
        if not kernel_path:
            raise NotImplementedError(
                f"EFI Boot Guard watchdog support is not implemented for architecture '{target_arch}'")

        print("Initializing EFI Boot Guard environment blocks...")
        epoch = self.source._epoch()
        if self.reproducible and getattr(self, "_extra_tools_files", None):
            tools_dir = self._upload_tools(g, "/.imager-extra-tools", self._extra_tools_files)
            env = f"LD_LIBRARY_PATH={tools_dir} SOURCE_DATE_EPOCH={epoch}"
            for idx, part in enumerate(bgenv_partitions, start=1):
                dev = part_devices[id(part)]
                env_data = create_bgenv(
                    kernel=kernel_path,
                    revision=idx,
                    watchdog=ph.watchdog,
                    ustate=0,
                )
                label = part.get("label")
                serial = self._fat_serial(label, dev)
                hidden = 0 if part.get("_lvm") else part.get("_start_mib", 0) * 2048
                v_opt = f" -v {label.upper()[:11]}" if label else ""
                g.sh(f"{env} {tools_dir}/mformat -i {dev} -H {hidden} -N {serial}{v_opt} ::")
                with tempfile.NamedTemporaryFile(dir=self._output_dir, prefix="seine-") as f:
                    f.write(env_data)
                    f.flush()
                    g.upload(f.name, f"{tools_dir}/bgenv.dat")
                g.sh(f"{env} {tools_dir}/mcopy -Q -i {dev} {tools_dir}/bgenv.dat ::/{BGENV_FILENAME}")
                g.rm(f"{tools_dir}/bgenv.dat")
            g.rm_rf(tools_dir)
        else:
            mountpoints = g.mountpoints() if hasattr(g, "mountpoints") else {}
            mountpoint = "/.imager-bgenv-mnt" if "/" in mountpoints else "/"
            if mountpoint != "/":
                g.mkmountpoint(mountpoint)
            try:
                for idx, part in enumerate(bgenv_partitions, start=1):
                    dev = part_devices[id(part)]
                    env_data = create_bgenv(
                        kernel=kernel_path,
                        revision=idx,
                        watchdog=ph.watchdog,
                        ustate=0,
                    )
                    dest = f"{mountpoint.rstrip('/')}/{BGENV_FILENAME}"
                    g.mount(dev, mountpoint)
                    self._upload_bytes(g, env_data, dest)
                    if self.reproducible:
                        g.utimens(dest, epoch, 0, epoch, 0)
                    g.umount(mountpoint)
            finally:
                if mountpoint != "/":
                    g.rmdir(mountpoint)

    def _install_efibootguard(self, g, bootloader, esp_path):
        ph = self.source.partitionHandler
        if not (ph.watchdog and ph.watchdog > 0):
            return
        if not bootloader:
            return
        if not isinstance(bootloader, SystemdBootBootloader):
            raise NotImplementedError(
                "EFI Boot Guard watchdog support requires systemd-boot")

        target_arch = self.source.spec.get("distribution", {}).get("architecture")
        if target_arch not in EBG_EFI_BINARIES:
            raise NotImplementedError(
                f"EFI Boot Guard watchdog support is not implemented for architecture '{target_arch}'")

        esp_clean = esp_path.rstrip("/")
        systemd_boot_file = f"{esp_clean}/EFI/systemd/{SYSTEMD_BOOT_FILENAME[target_arch]}"
        if not g.is_file(systemd_boot_file):
            raise FileNotFoundError(
                f"systemd-boot binary not found at '{systemd_boot_file}'")

        src = None
        for candidate in EBG_EFI_BINARIES[target_arch]:
            if g.is_file(candidate):
                src = candidate
                break
        if src is None:
            raise FileNotFoundError(
                f"EFI Boot Guard binary not found for architecture '{target_arch}'")

        removable_dir = f"{esp_clean}/EFI/BOOT"
        removable_file = f"{removable_dir}/{EBG_ESP_REMOVABLE[target_arch]}"
        g.mkdir_p(removable_dir)
        if g.is_file(removable_file):
            g.rm(removable_file)
        print("Installing EFI Boot Guard as removable boot loader...")
        g.cp(src, removable_file)
        if self.reproducible:
            epoch = self.source._epoch()
            g.utimens(removable_file, epoch, 0, epoch, 0)


