# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
import os
import re
import subprocess
import tempfile

from seine.containers.arch import to_container_arch, to_debian_arch


def run_skopeo(args, hostBootstrap=None, volumes=None, workdir=None):
    if hostBootstrap is not None and getattr(hostBootstrap, "name", None):
        try:
            return hostBootstrap.output(["skopeo"] + args, volumes=volumes, workdir=workdir)
        except Exception:
            pass
    return subprocess.check_output(["skopeo"] + args)


def container_archive_filename(image, arch, digest):
    clean_image = re.sub(r"[/:]", "_", image)
    clean_digest = digest.split(":")[-1][:12]
    return f"{clean_image}_{arch}_{clean_digest}.tar"


def resolve_container(image, archs, hostBootstrap=None, auth=None):
    ref = image if image.startswith("docker://") else f"docker://{image}"
    cmd = ["inspect", "--raw", ref]
    raw = run_skopeo(cmd, hostBootstrap=hostBootstrap)
    if isinstance(raw, bytes):
        raw_bytes = raw
        raw_str = raw.decode("utf-8", errors="replace")
    else:
        raw_bytes = raw.encode("utf-8")
        raw_str = raw
    top_digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    try:
        data = json.loads(raw_str)
    except Exception:
        data = {}
    arch_digests = {}
    manifests = data.get("manifests", [])
    if manifests:
        for m in manifests:
            plat = m.get("platform", {})
            if plat.get("os") == "linux":
                oci_arch = plat.get("architecture")
                oci_variant = plat.get("variant")
                deb_arch = to_debian_arch(oci_arch, oci_variant)
                if deb_arch in archs:
                    arch_digests[deb_arch] = m.get("digest")
    else:
        for a in archs:
            arch_digests[a] = top_digest
    return top_digest, arch_digests


def fetch_container(image, arch, dest_path, manifest_digest=None, hostBootstrap=None, auth=None):
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    oci_arch, oci_variant = to_container_arch(arch)
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        digest_file = tmp.name
    try:
        ref = image if image.startswith("docker://") else f"docker://{image}"
        cmd = [
            "copy",
            "--override-arch", oci_arch,
            "--override-os", "linux",
        ]
        if oci_variant:
            cmd += ["--override-variant", oci_variant]
        cmd += [
            "--digestfile", digest_file,
            ref,
            f"docker-archive:{dest_path}:{image}",
        ]
        run_skopeo(cmd, hostBootstrap=hostBootstrap)
        with open(digest_file) as f:
            copied_digest = f.read().strip()
    finally:
        if os.path.exists(digest_file):
            os.unlink(digest_file)
    size = os.path.getsize(dest_path)
    h = hashlib.sha256()
    with open(dest_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    file_sha256 = h.hexdigest()
    return {
        "manifest_digest": copied_digest or manifest_digest,
        "size": size,
        "sha256": file_sha256,
    }
