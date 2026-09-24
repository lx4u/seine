# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile

from seine.tasks import Task
from seine.container import ContainerEngine

# Reports source packages, which CVEs are filed against, unlike generic scanners.
DEBSBOM_IMAGE = "ghcr.io/siemens/debsbom:latest"
SYFT_IMAGE = "ghcr.io/anchore/syft:latest"

# Files debsbom needs from a root filesystem: dpkg's package list and
# apt's caches. Extracting these avoids unpacking the whole tarball.
SBOM_INPUTS = ["var/lib/dpkg/status", "var/lib/apt/extended_states",
               "var/lib/apt/lists/"]

# Entries ending in '/' match everything under them, others match exactly.
def wanted(name):
    name = name.removeprefix("./")
    return any(name == path or (path.endswith("/") and name.startswith(path))
               for path in SBOM_INPUTS)

# Name/version/Installed-Size for each package, read from dpkg's status
# file in the tarball. Sorted largest first.
def installed_packages(tarball):
    try:
        with tarfile.open(tarball, "r") as tar:
            member = next((m for m in tar.getmembers()
                          if m.name.removeprefix("./") == "var/lib/dpkg/status"),
                         None)
            if member is None:
                return []
            status = tar.extractfile(member).read().decode("utf-8", "replace")
    except (OSError, tarfile.TarError):
        return []

    packages = []
    for stanza in status.split("\n\n"):
        name = version = kib = None
        for line in stanza.splitlines():
            if line.startswith("Package:"):
                name = line[len("Package:"):].strip()
            elif line.startswith("Version:"):
                version = line[len("Version:"):].strip()
            elif line.startswith("Installed-Size:"):
                try:
                    kib = int(line[len("Installed-Size:"):].strip())
                except ValueError:
                    kib = None
        if name and kib is not None:
            packages.append((name, version or "?", kib))
    return sorted(packages, key=lambda entry: entry[2], reverse=True)

# Path a prior 'seine build --sbom' would have written to, whether or not
# it exists. Same suffix rule as SBOM._output_file(), but without its
# gate on the current build options.
def output_path(image_output):
    path = os.path.realpath(image_output)
    if path.endswith(".img"):
        path = path[:-len(".img")]
    return path + "-sbom.spdx.json"

def scan_container(archive_path):
    archive_path = os.path.realpath(archive_path)
    archive_dir = os.path.dirname(archive_path)
    with tempfile.TemporaryDirectory(dir=ContainerEngine.scratch()) as scratch_dir:
        out_file = os.path.join(scratch_dir, "syft-sbom.json")
        if shutil.which("syft"):
            cmd = ["syft", "scan", f"docker-archive:{archive_path}", "-o", f"spdx-json={out_file}"]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        else:
            run_cmd = [
                "run", "--rm",
                "-v", f"{archive_dir}:{archive_dir}:ro,z",
                "-v", f"{scratch_dir}:{scratch_dir}:z",
                SYFT_IMAGE,
                "scan", f"docker-archive:{archive_path}",
                "-o", f"spdx-json={out_file}"
            ]
            ContainerEngine.run(run_cmd, check=True)
        with open(out_file, "r") as f:
            return json.load(f)

def splice_container_sbom(root_sbom_path, container_scans, distro_arch=None):
    if not os.path.exists(root_sbom_path):
        return
    with open(root_sbom_path, "r") as f:
        spdx = json.load(f)

    root_pkg_id = "SPDXRef-Debian"
    for rel in spdx.get("relationships", []):
        if rel.get("spdxElementId") == "SPDXRef-DOCUMENT" and rel.get("relationshipType") == "DESCRIBES":
            root_pkg_id = rel.get("relatedSpdxElement")
            break

    for container, syft_data in container_scans:
        label = container.image or (os.path.basename(container.file) if container.file else f"container-{container.index}")
        clean_label = re.sub(r"[^a-zA-Z0-9.-]", "-", label.removeprefix("docker.io/library/").removeprefix("docker.io/"))
        container_pkg_id = f"SPDXRef-Container-{clean_label}"

        container_pkg = {
            "SPDXID": container_pkg_id,
            "name": label,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
        }
        if distro_arch:
            digest = container.digest_for(distro_arch)
            if digest:
                container_pkg["versionInfo"] = digest

        syft_root_id = None
        for r in syft_data.get("relationships", []):
            if r.get("spdxElementId") == "SPDXRef-DOCUMENT" and r.get("relationshipType") == "DESCRIBES":
                syft_root_id = r.get("relatedSpdxElement")
                break

        id_map = {
            "SPDXRef-DOCUMENT": container_pkg_id,
        }
        if syft_root_id:
            id_map[syft_root_id] = container_pkg_id

        remapped_packages = []
        for p in syft_data.get("packages", []):
            p_copy = dict(p)
            orig_id = p_copy.get("SPDXID", "")
            suffix = orig_id.removeprefix("SPDXRef-")
            new_id = f"{container_pkg_id}-{suffix}"
            id_map[orig_id] = new_id
            p_copy["SPDXID"] = new_id
            remapped_packages.append(p_copy)

        remapped_files = []
        for fl in syft_data.get("files", []):
            fl_copy = dict(fl)
            orig_id = fl_copy.get("SPDXID", "")
            suffix = orig_id.removeprefix("SPDXRef-")
            new_id = f"{container_pkg_id}-{suffix}"
            id_map[orig_id] = new_id
            fl_copy["SPDXID"] = new_id
            remapped_files.append(fl_copy)

        remapped_relationships = []
        contains_targets = set()
        for r in syft_data.get("relationships", []):
            if r.get("spdxElementId") == "SPDXRef-DOCUMENT" and r.get("relationshipType") == "DESCRIBES":
                continue
            src_id = id_map.get(r.get("spdxElementId"), r.get("spdxElementId"))
            tgt_id = id_map.get(r.get("relatedSpdxElement"), r.get("relatedSpdxElement"))
            rel_type = r.get("relationshipType")
            if src_id == container_pkg_id and rel_type == "CONTAINS":
                contains_targets.add(tgt_id)
            rel_entry = {
                "spdxElementId": src_id,
                "relatedSpdxElement": tgt_id,
                "relationshipType": rel_type,
            }
            if "comment" in r:
                rel_entry["comment"] = r["comment"]
            remapped_relationships.append(rel_entry)

        for p in remapped_packages:
            if p["SPDXID"] not in contains_targets:
                remapped_relationships.append({
                    "spdxElementId": container_pkg_id,
                    "relatedSpdxElement": p["SPDXID"],
                    "relationshipType": "CONTAINS"
                })

        spdx.setdefault("packages", []).append(container_pkg)
        spdx["packages"].extend(remapped_packages)
        if remapped_files:
            spdx.setdefault("files", []).extend(remapped_files)
        spdx.setdefault("relationships", []).append({
            "spdxElementId": root_pkg_id,
            "relatedSpdxElement": container_pkg_id,
            "relationshipType": "CONTAINS"
        })
        spdx["relationships"].extend(remapped_relationships)

    with open(root_sbom_path, "w") as f:
        json.dump(spdx, f, indent=2)

class SBOM:
    def __init__(self, distro, options=None):
        self.distro = distro
        self.options = options if options is not None else {}

    # debsbom appends '.spdx.json' itself, giving '<image>-sbom.spdx.json'.
    def _output_file(self, image):
        output = None
        if 'sbom' in self.options and self.options['sbom'] is True:
            output = os.path.realpath(image)
            if output.endswith('.img'):
                output = output[:-len('.img')]
            output = output + '-sbom'
        return output

    def _extract(self, tarball, root):
        with tarfile.open(tarball, "r") as tar:
            for member in tar:
                if wanted(member.name):
                    tar.extract(member, path=root)

    # Needs the tarball, not the disk image, for dpkg's package list.
    def task(self, image):
        return Task("sbom",
                    lambda: self.generate(image._tarball, image._output, image_obj=image),
                    needs=["tarball"])

    def generate(self, tarball, output, image_obj=None, containers=None):
        if not isinstance(output, str) and hasattr(output, "_output"):
            image_obj = output
            output = output._output
        output_file = self._output_file(output)
        if output_file is not None:
            dir = os.path.dirname(output_file)
            with tempfile.TemporaryDirectory(dir=ContainerEngine.scratch()) as root:
                self._extract(tarball, root)
                run_cmd = ['run', '--rm',
                           '-v', '{}:/rootfs:ro,z'.format(root),
                           '-v', '{}:{}:z'.format(dir, dir),
                           DEBSBOM_IMAGE]
                # Passed explicitly: mmdebstrap rootfs lack arch-native.
                debsbom_cmd = ['debsbom', 'generate', '-r', '/rootfs', '-t', 'spdx',
                               '--distro-arch', self.distro["architecture"],
                               '-o', output_file]
                ContainerEngine.run([*run_cmd, *debsbom_cmd], check=True)

            container_list = containers
            if container_list is None and image_obj is not None:
                container_list = getattr(image_obj, "containers", [])

            if container_list:
                arch = self.distro["architecture"]
                main_files = self.options.get("files") or []
                spec_dir = os.path.dirname(main_files[0]) if len(main_files) > 0 else "."
                fetch_dir = os.path.join(self.options.get("build_dir") or "build", "containers")

                scans = []
                for c in container_list:
                    archive = c.archive_for(arch, spec_dir=spec_dir)
                    if not (archive and os.path.exists(archive)) and c.image:
                        archive = c.fetch_archive(arch, fetch_dir)
                    if archive and os.path.exists(archive):
                        syft_data = scan_container(archive)
                        scans.append((c, syft_data))
                if scans:
                    splice_container_sbom(output_file + ".spdx.json", scans, distro_arch=arch)
