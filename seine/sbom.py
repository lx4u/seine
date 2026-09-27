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

# A package's own embedded SBOM fragment, e.g. from 'extends: go:
# license-scan: true'. The captured group is the package name.
GO_SBOM_RE = re.compile(r"^usr/share/doc/([^/]+)/sbom\.spdx\.json$")

# Entries ending in '/' match everything under them, others match exactly.
def wanted(name):
    name = name.removeprefix("./")
    if any(name == path or (path.endswith("/") and name.startswith(path))
           for path in SBOM_INPUTS):
        return True
    return GO_SBOM_RE.match(name) is not None

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

# 'ro_dir' is mounted read-only for the container-engine path; unused
# when a local 'syft' binary is run directly.
def _run_syft(scan_target, ro_dir, out_file, extra_args=None):
    extra_args = extra_args or []
    scratch_dir = os.path.dirname(out_file)
    if shutil.which("syft"):
        cmd = ["syft", "scan", scan_target, *extra_args, "-o", f"spdx-json={out_file}"]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        run_cmd = [
            "run", "--rm",
            "-v", f"{ro_dir}:{ro_dir}:ro,z",
            "-v", f"{scratch_dir}:{scratch_dir}:z",
            SYFT_IMAGE,
            "scan", scan_target, *extra_args,
            "-o", f"spdx-json={out_file}"
        ]
        ContainerEngine.run(run_cmd, check=True)
    with open(out_file, "r") as f:
        return json.load(f)

def scan_container(archive_path):
    archive_path = os.path.realpath(archive_path)
    with tempfile.TemporaryDirectory(dir=ContainerEngine.scratch()) as scratch_dir:
        out_file = os.path.join(scratch_dir, "syft-sbom.json")
        return _run_syft(f"docker-archive:{archive_path}",
                          os.path.dirname(archive_path), out_file)

def scan_directory(path, extra_args=None):
    path = os.path.realpath(path)
    with tempfile.TemporaryDirectory(dir=ContainerEngine.scratch()) as scratch_dir:
        out_file = os.path.join(scratch_dir, "syft-sbom.json")
        return _run_syft(f"dir:{path}", path, out_file, extra_args=extra_args)

# Merges a syft SPDX doc's packages/files/relationships into 'spdx', with
# every id prefixed to avoid collisions, and CONTAINS edges from
# 'anchor_pkg_id' to each of the syft doc's top-level packages.
def _splice_packages(spdx, anchor_pkg_id, syft_data, id_prefix):
    syft_root_id = None
    for r in syft_data.get("relationships", []):
        if r.get("spdxElementId") == "SPDXRef-DOCUMENT" and r.get("relationshipType") == "DESCRIBES":
            syft_root_id = r.get("relatedSpdxElement")
            break

    id_map = {"SPDXRef-DOCUMENT": anchor_pkg_id}
    if syft_root_id:
        id_map[syft_root_id] = anchor_pkg_id

    remapped_packages = []
    for p in syft_data.get("packages", []):
        p_copy = dict(p)
        orig_id = p_copy.get("SPDXID", "")
        suffix = orig_id.removeprefix("SPDXRef-")
        new_id = f"{id_prefix}-{suffix}"
        id_map[orig_id] = new_id
        p_copy["SPDXID"] = new_id
        remapped_packages.append(p_copy)

    remapped_files = []
    for fl in syft_data.get("files", []):
        fl_copy = dict(fl)
        orig_id = fl_copy.get("SPDXID", "")
        suffix = orig_id.removeprefix("SPDXRef-")
        new_id = f"{id_prefix}-{suffix}"
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
        if src_id == anchor_pkg_id and rel_type == "CONTAINS":
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
                "spdxElementId": anchor_pkg_id,
                "relatedSpdxElement": p["SPDXID"],
                "relationshipType": "CONTAINS"
            })

    spdx.setdefault("packages", []).extend(remapped_packages)
    if remapped_files:
        spdx.setdefault("files", []).extend(remapped_files)
    spdx.setdefault("relationships", []).extend(remapped_relationships)

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

        spdx.setdefault("packages", []).append(container_pkg)
        spdx.setdefault("relationships", []).append({
            "spdxElementId": root_pkg_id,
            "relatedSpdxElement": container_pkg_id,
            "relationshipType": "CONTAINS"
        })
        _splice_packages(spdx, container_pkg_id, syft_data, container_pkg_id)

    with open(root_sbom_path, "w") as f:
        json.dump(spdx, f, indent=2)

# Nests a Go package's own vendored-module scan under its existing SPDX
# package entry (found by name), rather than under a new wrapper package.
def splice_go_sboms(root_sbom_path, fragments):
    if not os.path.exists(root_sbom_path):
        return
    with open(root_sbom_path, "r") as f:
        spdx = json.load(f)

    for pkg_name, syft_data in fragments:
        # debsbom emits both a source-package node and one binary-package
        # node per architecture, sharing the same name: attach to the
        # binary (what actually ships these modules), not the source.
        candidates = [p for p in spdx.get("packages", [])
                     if p.get("name") == pkg_name]
        anchor = next((p for p in candidates
                      if p.get("primaryPackagePurpose") != "SOURCE"),
                     candidates[0] if candidates else None)
        if anchor is None:
            print(f"SBOM: no package named '{pkg_name}' found for its "
                  "embedded Go module list, skipping")
            continue
        _splice_packages(spdx, anchor["SPDXID"], syft_data, anchor["SPDXID"])

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

    def current(self, tarball, output, image_obj=None):
        if self.options.get("rebuild"):
            return False
        if not isinstance(output, str) and hasattr(output, "_output"):
            image_obj = output
            output = output._output
        output_file = self._output_file(output)
        if output_file is None:
            return True
        sbom_path = output_file + ".spdx.json"
        if not os.path.isfile(sbom_path):
            return False
        if not tarball or not os.path.isfile(tarball):
            return False
        digest_file = f"{tarball}.digest"
        if not os.path.isfile(digest_file):
            return False
        if image_obj is not None and hasattr(image_obj, "_rootfs_digest"):
            try:
                from seine import vendor
                distro = image_obj.spec["distribution"]
                vendor_digest = vendor.offline_dockerfile_digest(image_obj.spec, distro)
                digest = image_obj._rootfs_digest(vendor_digest)
                return image_obj._rootfs_current(digest)
            except Exception:
                return False
        return True

    def _extract(self, tarball, root):
        with tarfile.open(tarball, "r") as tar:
            for member in tar:
                if wanted(member.name):
                    tar.extract(member, path=root)

    # Any package's own embedded Go-module SBOM, found among what
    # '_extract()' pulled out of the rootfs tarball.
    def _collect_go_sboms(self, root):
        fragments = []
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                relpath = os.path.relpath(
                    os.path.join(dirpath, filename), root)
                match = GO_SBOM_RE.match(relpath)
                if match:
                    with open(os.path.join(dirpath, filename)) as f:
                        fragments.append((match.group(1), json.load(f)))
        return fragments

    # Needs the tarball, not the disk image, for dpkg's package list.
    # 'image._output' is None for an 'initrd:' build, which deploys
    # '_initrd_output' instead -- fall back to that for the SBOM's name.
    def task(self, image):
        return Task("sbom",
                    lambda: self.generate(image._tarball,
                                          image._output or image._initrd_output,
                                          image_obj=image),
                    needs=["rootfs"])

    def generate(self, tarball, output, image_obj=None, containers=None):
        if not isinstance(output, str) and hasattr(output, "_output"):
            image_obj = output
            output = output._output
        output_file = self._output_file(output)
        if output_file is not None:
            target_sbom = output_file + ".spdx.json"
            if self.current(tarball, output, image_obj=image_obj):
                print(f"SBOM up to date ({target_sbom})")
                return

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
                go_fragments = self._collect_go_sboms(root)

            if go_fragments:
                splice_go_sboms(output_file + ".spdx.json", go_fragments)

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
