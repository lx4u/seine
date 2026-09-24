# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import re
from typing import Any, Dict, List, Optional


class ContainerImage:
    def __init__(
        self,
        spec: Dict[str, Any],
        index: int,
        spec_dir: str = ".",
        defaults: Optional[Dict[str, Any]] = None,
    ):
        self.index = index
        self.spec = spec
        defaults = defaults or {}
        if not isinstance(self.spec, dict):
            raise ValueError(f"container #{self.index} is not a dictionary!")
        self.image: Optional[str] = spec.get("image")
        self.digest: Optional[str] = spec.get("digest")
        self.digests: Optional[Dict[str, str]] = spec.get("digests")
        self.file: Optional[str] = spec.get("file")
        self.auth: Optional[Dict[str, Any]] = spec.get("auth")
        self.architectures: Optional[Dict[str, Any]] = spec.get("architectures")
        self.target: str = spec.get("target") or defaults.get("target") or "docker"
        self.root: Optional[str] = spec.get("root") or defaults.get("root")
        self.namespace: Optional[str] = spec.get("namespace") or defaults.get("namespace")
        self.resolved_file: Optional[str] = None
        self._validate(spec_dir)

    def _validate(self, spec_dir: str):
        if not self.image and not self.file:
            if "image" in self.spec:
                raise ValueError(f"container #{self.index}: 'image' must be a non-empty string")
            if "file" in self.spec:
                raise ValueError(f"container #{self.index}: 'file' must be a non-empty string")
            raise ValueError(
                f"container #{self.index} must declare either 'image:' or 'file:'"
            )

        if self.target not in ("docker", "containerd"):
            raise ValueError(
                f"container #{self.index}: invalid target '{self.target}'. Expected 'docker' or 'containerd'"
            )

        if self.root is None:
            if self.target == "docker":
                self.root = "/var/lib/docker"
            else:
                self.root = "/var/lib/containerd"
        elif not isinstance(self.root, str) or not self.root.startswith("/"):
            raise ValueError(f"container #{self.index}: 'root' must be an absolute path string")

        if self.namespace is None:
            if self.target == "containerd":
                if "k3s" in self.root or "rancher" in self.root:
                    self.namespace = "k8s.io"
                else:
                    self.namespace = "default"
        elif not isinstance(self.namespace, str):
            raise ValueError(f"container #{self.index}: 'namespace' must be a string")

        if self.image is not None:
            if not isinstance(self.image, str) or not self.image.strip():
                raise ValueError(f"container #{self.index}: 'image' must be a non-empty string")
            self.image = self.image.removeprefix("docker://")

        if self.digest is not None:
            if not isinstance(self.digest, str):
                raise ValueError(f"container #{self.index}: 'digest' must be a string")
            if not re.match(r"^sha256:[a-f0-9]{64}$", self.digest):
                raise ValueError(
                    f"container #{self.index}: invalid digest format '{self.digest}'. "
                    f"Expected 'sha256:<64-hex>'"
                )

        if self.digests is not None:
            if not isinstance(self.digests, dict):
                raise ValueError(f"container #{self.index}: 'digests' must be a dictionary")
            for arch, d in self.digests.items():
                if not isinstance(d, str) or not re.match(r"^sha256:[a-f0-9]{64}$", d):
                    raise ValueError(
                        f"container #{self.index}: invalid digest format for architecture '{arch}': '{d}'. "
                        f"Expected 'sha256:<64-hex>'"
                    )

        if self.architectures is not None:
            if not isinstance(self.architectures, dict):
                raise ValueError(f"container #{self.index}: 'architectures' must be a dictionary")
            for arch, info in self.architectures.items():
                if not isinstance(info, dict):
                    raise ValueError(f"container #{self.index}: architectures['{arch}'] must be a dictionary")
                arch_digest = info.get("digest") or info.get("manifest_digest")
                if arch_digest is not None and (not isinstance(arch_digest, str) or not re.match(r"^sha256:[a-f0-9]{64}$", arch_digest)):
                    raise ValueError(
                        f"container #{self.index}: invalid digest format for architecture '{arch}': '{arch_digest}'"
                    )

        if self.file is not None:
            if not isinstance(self.file, str):
                raise ValueError(f"container #{self.index}: 'file' must be a string path")
            self.resolved_file = os.path.normpath(
                os.path.join(spec_dir, self.file) if not os.path.isabs(self.file) else self.file
            )

        if self.auth is not None:
            if not isinstance(self.auth, dict):
                raise ValueError(f"container #{self.index}: 'auth' must be a dictionary")
            if "vault" in self.auth and not isinstance(self.auth["vault"], str):
                raise ValueError(f"container #{self.index}: 'auth: vault:' must be a string path")

    def digest_for(self, arch: Optional[str] = None) -> Optional[str]:
        """Return the digest applicable to the given architecture, if any."""
        if arch and self.digests and arch in self.digests:
            return self.digests[arch]
        if arch and self.architectures and arch in self.architectures:
            info = self.architectures[arch]
            if isinstance(info, dict):
                d = info.get("digest") or info.get("manifest_digest")
                if d:
                    return d
        if self.digest:
            return self.digest
        if arch is None:
            if self.digests:
                return next(iter(self.digests.values()), None)
            if self.architectures:
                for info in self.architectures.values():
                    if isinstance(info, dict):
                        d = info.get("digest") or info.get("manifest_digest")
                        if d:
                            return d
        return None

    def has_digest(self, arch: Optional[str] = None) -> bool:
        return self.digest_for(arch) is not None

    @property
    def identity(self) -> str:
        if self.digest:
            return f"{self.image}@{self.digest}" if self.image else self.digest
        if self.image:
            return self.image
        return self.file or f"container-#{self.index}"

    def archive_for(self, arch: str, spec_dir: str = ".") -> Optional[str]:
        if self.resolved_file:
            return self.resolved_file
        if self.file:
            return os.path.normpath(
                os.path.join(spec_dir, self.file) if not os.path.isabs(self.file) else self.file
            )
        if self.architectures and arch in self.architectures:
            arch_info = self.architectures[arch]
            if isinstance(arch_info, dict) and "file" in arch_info:
                path = arch_info["file"]
                return os.path.normpath(
                    os.path.join(spec_dir, path) if not os.path.isabs(path) else path
                )
        if self.image:
            clean_image = re.sub(r"[/:]", "_", self.image)
            container_dir = os.path.join(spec_dir, "vendor", "containers")
            if os.path.isdir(container_dir):
                pinned = self.digest_for(arch)
                if pinned:
                    clean_d = pinned.split(":")[-1][:12]
                    expected = f"{clean_image}_{arch}_{clean_d}.tar"
                    candidate_path = os.path.join(container_dir, expected)
                    if os.path.isfile(candidate_path):
                        return os.path.normpath(candidate_path)
                prefix = f"{clean_image}_{arch}_"
                for candidate in os.listdir(container_dir):
                    if candidate.startswith(prefix) and candidate.endswith(".tar"):
                        return os.path.normpath(os.path.join(container_dir, candidate))
        return None

    def fetch_archive(self, arch: str, dest_dir: str) -> str:
        """Fetch this image from the registry via skopeo and return the local path.

        Uses a content-addressed filename so repeated calls skip re-downloading.
        Accepts a pinned digest that matches either the multi-arch index or the
        per-arch manifest, consistent with how 'seine vendor' resolves digests.
        """
        if not self.image:
            raise ValueError(f"container #{self.index}: cannot fetch — no 'image:' declared")
        from seine.containers.fetch import fetch_container, resolve_container
        os.makedirs(dest_dir, exist_ok=True)
        top_digest, arch_digests = resolve_container(self.image, [arch])
        arch_digest = arch_digests.get(arch) or top_digest
        pinned = self.digest_for(arch)
        if pinned and pinned != top_digest and pinned != arch_digest:
            raise ValueError(
                f"container #{self.index}: resolved digest {arch_digest} "
                f"does not match pinned digest {pinned}"
            )
        recorded = pinned or top_digest
        clean_image = re.sub(r"[/:]", "_", self.image)
        clean_digest = recorded.split(":")[-1][:12]
        final_path = os.path.join(dest_dir, f"{clean_image}_{arch}_{clean_digest}.tar")
        if not os.path.isfile(final_path):
            tmp_path = f"{final_path}.fetching.{os.getpid()}"
            try:
                fetch_container(self.image, arch, tmp_path, manifest_digest=arch_digest)
                if not os.path.isfile(final_path):
                    os.replace(tmp_path, final_path)
                else:
                    os.unlink(tmp_path)
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                raise
        return final_path


def parse(spec: Dict[str, Any], spec_dir: str = ".") -> List[ContainerImage]:
    raw = spec.get("containers")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [ContainerImage(entry, i + 1, spec_dir=spec_dir) for i, entry in enumerate(raw)]
    if isinstance(raw, dict):
        defaults = {
            "target": raw.get("target"),
            "root": raw.get("root"),
            "namespace": raw.get("namespace"),
        }
        if defaults["target"] is not None and defaults["target"] not in ("docker", "containerd"):
            raise ValueError(
                f"invalid target '{defaults['target']}'. Expected 'docker' or 'containerd'"
            )
        images = raw.get("images", [])
        if images is None:
            return []
        if not isinstance(images, list):
            raise ValueError("'containers: images:' shall be a list of container definitions!")
        return [
            ContainerImage(entry, i + 1, spec_dir=spec_dir, defaults=defaults)
            for i, entry in enumerate(images)
        ]
    raise ValueError("'containers:' shall be a list or dictionary of container definitions!")


parse_containers_spec = parse


def validate_hashes(containers: List[ContainerImage], arch: Optional[str] = None):
    missing = [c for c in containers if c.image and not c.has_digest(arch) and not c.file]
    if missing:
        report = [
            f"--require-hashes was given and {len(missing)} container(s) missing digest:"
        ]
        for c in missing:
            report.append(f"  container #{c.index}: image '{c.image}'")
            report.append("    add 'digest: sha256:...' or 'digests: ...' to the specification")
        raise ValueError("\n".join(report))


def validate_offline(containers: List[ContainerImage], arch: str, spec_dir: str = "."):
    missing = []
    for c in containers:
        archive = c.archive_for(arch, spec_dir)
        if not archive or not os.path.isfile(archive):
            label = f"image '{c.image}'" if c.image else f"file '{c.file}'"
            missing.append(f"  container #{c.index}: {label} has no local archive for '{arch}'")
    if missing:
        raise ValueError(
            "offline build requires all containers to be vendored locally:\n" + "\n".join(missing)
        )


def _merge_container_lists(
    base_list: List[Dict[str, Any]], overlay_list: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged = [dict(e) for e in base_list if isinstance(e, dict)]
    image_map = {e.get("image"): e for e in merged if e.get("image")}
    file_map = {e.get("file"): e for e in merged if e.get("file")}
    for entry in overlay_list:
        if not isinstance(entry, dict):
            continue
        target = None
        if entry.get("image") and entry.get("image") in image_map:
            target = image_map[entry["image"]]
        elif entry.get("file") and entry.get("file") in file_map:
            target = file_map[entry["file"]]
        if target is not None:
            for k, v in entry.items():
                if k not in target or target[k] is None:
                    target[k] = v
                elif k == "digests" and isinstance(v, dict) and isinstance(target.get("digests"), dict):
                    target["digests"].update(v)
                elif k == "architectures" and isinstance(v, dict) and isinstance(target.get("architectures"), dict):
                    target["architectures"].update(v)
        else:
            new_e = dict(entry)
            merged.append(new_e)
            if new_e.get("image"):
                image_map[new_e["image"]] = new_e
            if new_e.get("file"):
                file_map[new_e["file"]] = new_e
    return merged


def merge_containers(base_raw: Any, overlay_raw: Any) -> Any:
    if base_raw is None:
        return overlay_raw
    if overlay_raw is None:
        return base_raw

    base_is_dict = isinstance(base_raw, dict)
    overlay_is_dict = isinstance(overlay_raw, dict)

    if not base_is_dict and not overlay_is_dict:
        return _merge_container_lists(base_raw, overlay_raw)

    base_dict = base_raw if base_is_dict else {"images": base_raw}
    overlay_dict = overlay_raw if overlay_is_dict else {"images": overlay_raw}

    merged_dict = dict(base_dict)
    for k in ("target", "root", "namespace"):
        if k in overlay_dict and overlay_dict[k] is not None:
            merged_dict[k] = overlay_dict[k]

    base_images = base_dict.get("images") or []
    overlay_images = overlay_dict.get("images") or []
    merged_dict["images"] = _merge_container_lists(base_images, overlay_images)

    return merged_dict
