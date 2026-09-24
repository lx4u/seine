# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

from typing import Optional, Tuple

DEBIAN_TO_OCI = {
    "amd64": ("amd64", None),
    "arm64": ("arm64", "v8"),
    "armhf": ("arm", "v7"),
    "armel": ("arm", "v6"),
    "i386": ("386", None),
    "ppc64el": ("ppc64le", None),
    "riscv64": ("riscv64", None),
    "s390x": ("s390x", None),
}

OCI_TO_DEBIAN = {
    ("amd64", None): "amd64",
    ("arm64", None): "arm64",
    ("arm64", "v8"): "arm64",
    ("arm", "v7"): "armhf",
    ("arm", "v6"): "armel",
    ("arm", None): "armhf",
    ("386", None): "i386",
    ("ppc64le", None): "ppc64el",
    ("riscv64", None): "riscv64",
    ("s390x", None): "s390x",
}


def to_container_arch(debian_arch: str) -> Tuple[str, Optional[str]]:
    """Map a Debian architecture name to (oci_architecture, optional_variant)."""
    return DEBIAN_TO_OCI.get(debian_arch, (debian_arch, None))


def to_debian_arch(oci_arch: str, variant: Optional[str] = None) -> str:
    """Map an OCI architecture and optional variant to a Debian architecture name."""
    if (oci_arch, variant) in OCI_TO_DEBIAN:
        return OCI_TO_DEBIAN[(oci_arch, variant)]
    if (oci_arch, None) in OCI_TO_DEBIAN:
        return OCI_TO_DEBIAN[(oci_arch, None)]
    return oci_arch
