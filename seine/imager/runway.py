# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import subprocess

from seine.imager.bootloader import runway_path

# The slot the first UKI is staged in.
SLOT = "CONFIG1"
STORE_QUOTA = 80
BOOT_TRIES = 3

# Where the tools container sees the work directory.
WORK = "/work"


# Formats the Runway store with one confirmed slot holding 'uki' and
# writes it to 'device', a partition of the appliance. 'run' runs a
# command of the imager tools container in 'workdir'.
def provision_store(g, run, arch, workdir, device, size_mib, store_uuid,
                    uki, entry, watchdog=None, seed=None):
    loader = runway_path(g, arch, "rwy-loader.efi")
    wdt_dir = runway_path(g, arch, "wdt")
    if loader is None or wdt_dir is None:
        raise RuntimeError(
            "Runway boot loader files (rwy-loader.efi or wdt drivers) "
            "not found in the root file-system")

    try:
        run(["sh", "-c", "command -v mkstore rwyctl"])
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "mkstore and rwyctl are not in the imager tools container: add "
            "examples/common/runway.yaml, which sets 'imager: packages: "
            "[runway-tools]'") from e

    g.download(loader, os.path.join(workdir, "rwy-loader.efi"))
    os.makedirs(os.path.join(workdir, "wdt"))
    for name in g.ls(wdt_dir):
        if name.lower().endswith(".efi"):
            g.download(f"{wdt_dir}/{name}", os.path.join(workdir, "wdt", name))
    shutil.copy(uki, os.path.join(workdir, "uki.efi"))

    cmd = ["mkstore", "--size", str(size_mib), f"--quota={STORE_QUOTA}",
           "--uuid", store_uuid, "--loader", "rwy-loader.efi", "--wdt", "wdt"]
    if seed:
        cmd += ["--random-seed", seed]
    run(cmd + ["RUNWAY.DAT"])

    wdt_files = [f"wdt/{name}" for name in sorted(os.listdir(os.path.join(workdir, "wdt")))
                 if name.lower().endswith(".efi")]
    if wdt_files:
        run(["rwyctl", "--store", "RUNWAY.DAT", "wdt", "set", "--unlock"] + wdt_files)

    if watchdog:
        run(["rwyctl", "--store", "RUNWAY.DAT", "set", "WATCHDOG", str(watchdog)])

    cmd = ["rwyctl", "--store", "RUNWAY.DAT", "install", "--name", SLOT,
           "--kernel", "uki.efi", "--entry", entry, "--tries", str(BOOT_TRIES)]
    if watchdog:
        cmd += ["--watchdog", str(watchdog)]
    run(cmd)
    run(["rwyctl", "--store", "RUNWAY.DAT", "confirm", SLOT])

    print(f"Provisioning Runway store partition on {device} ({size_mib} MiB)...")
    g.upload(os.path.join(workdir, "RUNWAY.DAT"), device)
