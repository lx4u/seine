# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Shadows ansible.builtin.apt: runs apt-get on the build host, not
# inside the (maybe foreign-arch) target. Only 'name', 'state:
# present|absent' and 'install_recommends' are supported.

import os
import subprocess

from ansible.plugins.action import ActionBase
from ansible.errors import AnsibleActionFail
from ansible.module_utils.parsing.convert_bool import boolean

ENV_CID = "SEINE_ROOTFS_CID"
ENV_ROOT = "SEINE_CONTAINER_ROOT"
ENV_RUNROOT = "SEINE_CONTAINER_RUNROOT"
ENV_ARCH = "SEINE_APT_ARCH"
ENV_HOST_IMAGE = "SEINE_APT_HOST_IMAGE"
ENV_PACKAGES_DIR = "SEINE_APT_PACKAGES_DIR"
# Set by ansible_runner.py only when a feed is authenticated -- the host
# path of the netrc seine.utils.netrc_for() wrote for this build.
ENV_NETRC = "SEINE_APT_NETRC"

MERGED = "/rootfs"
# Where a spec's rebuilt packages live, if any -- must match
# seine.packages.REPOSITORY, the path the target's own sources.list
# points 'file:' at.
PACKAGES = "/packages"
# Matches seine.utils.NETRC_MOUNT -- duplicated as a plain string rather
# than importing seine here, since this plugin runs as a standalone
# ansible process.
NETRC_MOUNT = "/run/seine/netrc"

def _env(name):
    value = os.environ.get(name)
    if not value:
        raise AnsibleActionFail(f"{name} is not set -- the 'apt' action "
                                f"plugin only works inside a seine build")
    return value

def _podman(args):
    cmd = ["podman", "--root", _env(ENV_ROOT), "--runroot", _env(ENV_RUNROOT)]
    return subprocess.run(cmd + args, capture_output=True, text=True)

def _merged_dir(cid):
    proc = _podman(["inspect", cid, "--format", "{{.GraphDriver.Data.MergedDir}}"])
    if proc.returncode != 0:
        raise AnsibleActionFail(f"could not inspect container {cid}: {proc.stderr}")
    return proc.stdout.strip()

def _names(value):
    if isinstance(value, str):
        return [n.strip() for n in value.replace(",", " ").split()]
    return list(value)

# A maintainer script (e.g. a kernel package's postinst) chroots into
# MERGED and expects a live /proc, /sys and /dev there, not empty dirs.
MOUNT_LIVE_FS = (f"mount -t proc proc {MERGED}/proc && "
                f"mount -t sysfs sys {MERGED}/sys && "
                f"mount --rbind /dev {MERGED}/dev && ")

CHANGED_MARKER = "SEINE_APT_CHANGED"

# One podman run does both the change check and, unless simulating, the
# real install/remove: apt-get's own '-s' already answers "would this
# change anything", so a separate dpkg-query pass (and container) buys
# nothing a second apt-get invocation doesn't already tell us.
def _apt_get(merged_dir, action, names, simulate, recommends=True):
    arch = _env(ENV_ARCH)
    netrc = os.environ.get(ENV_NETRC)
    recommends_opt = "" if recommends else "-o APT::Install-Recommends=false "
    netrc_opt = f'-o Dir::Etc::netrc="{NETRC_MOUNT}" ' if netrc else ""
    apt_get = (f"apt-get "
              f"-o Dir::State={MERGED}/var/lib/apt "
              f"-o Dir::State::status={MERGED}/var/lib/dpkg/status "
              f"-o Dir::Cache={MERGED}/var/cache/apt "
              f"-o Dir::Etc={MERGED}/etc/apt "
              f"-o DPkg::Chroot-Directory={MERGED} "
              # This root is a throwaway podman overlay: durability from
              # fsync buys nothing and dpkg's unpack is faster without it.
              f"-o DPkg::Options::=--force-unsafe-io "
              f"-o APT::Architecture={arch} "
              f"-o APT::Architectures::={arch} "
              f"{recommends_opt}"
              f"{netrc_opt}"
              f"-qqy {action} {' '.join(names)}")
    script = (
        f"if {apt_get} -s | grep -Eq '^(Inst|Remv) '; then "
        f"echo {CHANGED_MARKER}; "
        + ("true; " if simulate else MOUNT_LIVE_FS + apt_get + "; ") +
        "fi")
    # Simulating never mounts anything, so it needs no extra capability.
    caps = [] if simulate else ["--cap-add=sys_admin"]
    volumes = ["-v", f"{merged_dir}:{MERGED}"]
    packages_dir = os.environ.get(ENV_PACKAGES_DIR)
    if packages_dir:
        # apt's own acquire step reads this unchrooted, but dpkg then
        # chroots into MERGED to unpack each .deb and needs the same
        # path to still resolve from there -- so both, not just one.
        volumes += ["-v", f"{packages_dir}:{PACKAGES}",
                   "-v", f"{packages_dir}:{MERGED}{PACKAGES}"]
    if netrc:
        volumes += ["-v", f"{os.path.dirname(netrc)}:{os.path.dirname(NETRC_MOUNT)}:ro"]
    return _podman(["run", "--rm"] + caps + volumes +
                   [_env(ENV_HOST_IMAGE), "sh", "-c", script])

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        args = self._task.args

        state = args.get("state", "present")
        if state not in ("present", "absent"):
            raise AnsibleActionFail(f"apt: state={state} is not supported "
                                    f"here (only present/absent)")
        if "name" not in args:
            raise AnsibleActionFail("apt: 'name' is required")
        names = _names(args["name"])
        recommends = boolean(args.get("install_recommends", True))

        merged_dir = _merged_dir(_env(ENV_CID))
        action = "install" if state == "present" else "remove"
        proc = _apt_get(merged_dir, action, names, self._task.check_mode,
                         recommends)
        if proc.returncode != 0:
            result["failed"] = True
            result["msg"] = f"apt-get {action} failed: {proc.stderr.strip()}"
            return result

        result["changed"] = CHANGED_MARKER in proc.stdout
        return result
