# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

# 'vms:' converts the finished disk image into one or more hypervisor
# formats. 'type: ova' packages a stream-optimized VMDK plus an OVF
# descriptor into one '.ova' VMware and VirtualBox both import directly;
# 'type: vhdx' pairs with a Hyper-V PowerShell script, since Hyper-V has
# no OVA import of its own. Plain 'vmdk'/'vdi'/'qcow2'/'raw' are just the
# converted disk, for attaching to a VM by hand. Defaults the VM to
# UEFI + TPM + serial where the target format can express it.

import collections
import hashlib
import os
import subprocess
import tarfile
import tempfile

from seine import utils
from seine.tasks import Task

# Bump when a change here alters what bytes a converted artifact or a
# descriptor ends up holding, so a prior one is not reused.
CONVERT_REVISION = 1
DESCRIPTOR_REVISION = 1

# 'type:' -> file extension. 'ova' is not a qemu-img '-O' target -- see
# _build_ova() -- every other key here is passed to qemu-img as-is.
VM_FORMATS = {
    "vmdk":  ".vmdk",
    "vhdx":  ".vhdx",
    "vdi":   ".vdi",
    "qcow2": ".qcow2",
    "raw":   ".raw",
    "ova":   ".ova",
}

# qemu-img 'convert -o' key/value implementing 'thin-provisioning' per
# type. A type with no entry has no thin/thick switch to give qemu-img.
THIN_OPTIONS = {
    "vmdk": {True: ("subformat", "streamOptimized"), False: ("subformat", "monolithicFlat")},
    "vhdx": {True: ("subformat", "dynamic"), False: ("subformat", "fixed")},
    "vdi":  {True: ("static", "off"), False: ("static", "on")},
}

DEFAULTS = {
    "firmware": "uefi",
    "tpm": True,
    "serial": True,
    "cpus": 2,
    "memory": 2048,
    "thin-provisioning": True,
}

VmFormat = collections.namedtuple(
    "VmFormat", ["name", "type", "cpus", "memory", "firmware", "tpm",
                 "serial", "thin_provisioning", "options"])

Generator = collections.namedtuple("Generator", ["extension", "executable", "render"])

# OVF envelope for one VM, one disk. 'vmw:Config'/'firmware' is the key
# vCenter/ESXi itself writes for "UEFI" -- written here too, for tools
# that do honor it, but verified (VBoxManage import, VirtualBox 7.2) that
# VirtualBox silently imports as BIOS regardless: a real, documented gap,
# not a hypervisor-specific descriptor seine can paper over from here.
# TPM/serial have no OVF key at all; the launch descriptor path
# (Hyper-V's .ps1) is what carries those instead.
OVF_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
          xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
          xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData"
          xmlns:vmw="http://www.vmware.com/schema/ovf">
  <References>
    <File ovf:href="{disk_name}" ovf:id="disk1" ovf:size="{disk_size}"/>
  </References>
  <DiskSection>
    <Info>Virtual disk information</Info>
    <Disk ovf:capacity="{capacity}" ovf:capacityAllocationUnits="byte"
          ovf:diskId="vmdisk1" ovf:fileRef="disk1"
          ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
  </DiskSection>
  <VirtualSystem ovf:id="{name}">
    <Info>{name}</Info>
    <Name>{name}</Name>
    <OperatingSystemSection ovf:id="100">
      <Info>Guest OS</Info>
      <Description>Other Linux (64-bit)</Description>
    </OperatingSystemSection>
    <VirtualHardwareSection>
      <Info>Virtual hardware</Info>
      <System>
        <vssd:VirtualSystemType>vmx-19</vssd:VirtualSystemType>
      </System>
      <Item>
        <rasd:Description>Number of virtual CPUs</rasd:Description>
        <rasd:ElementName>{cpus} virtual CPU(s)</rasd:ElementName>
        <rasd:InstanceID>1</rasd:InstanceID>
        <rasd:ResourceType>3</rasd:ResourceType>
        <rasd:VirtualQuantity>{cpus}</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
        <rasd:Description>Memory Size</rasd:Description>
        <rasd:ElementName>{memory}MB of memory</rasd:ElementName>
        <rasd:InstanceID>2</rasd:InstanceID>
        <rasd:ResourceType>4</rasd:ResourceType>
        <rasd:VirtualQuantity>{memory}</rasd:VirtualQuantity>
      </Item>
      <Item>
        <rasd:Address>0</rasd:Address>
        <rasd:ElementName>SCSI Controller</rasd:ElementName>
        <rasd:InstanceID>3</rasd:InstanceID>
        <rasd:ResourceSubType>lsilogic</rasd:ResourceSubType>
        <rasd:ResourceType>6</rasd:ResourceType>
      </Item>
      <Item>
        <rasd:AddressOnParent>0</rasd:AddressOnParent>
        <rasd:ElementName>{disk_name}</rasd:ElementName>
        <rasd:HostResource>ovf:/disk/vmdisk1</rasd:HostResource>
        <rasd:InstanceID>4</rasd:InstanceID>
        <rasd:Parent>3</rasd:Parent>
        <rasd:ResourceType>17</rasd:ResourceType>
      </Item>
      <vmw:Config ovf:required="false" vmw:key="firmware" vmw:value="{firmware}"/>
    </VirtualHardwareSection>
  </VirtualSystem>
</Envelope>
"""


def _render_hyperv(fmt, disk_name):
    name = fmt.name
    lines = [
        f'$vmName = "{name}"',
        f'New-VM -Name $vmName -MemoryStartupBytes {fmt.memory}MB '
        f'-Generation 2 -VHDPath "{disk_name}"',
        f'Set-VMProcessor -VMName $vmName -Count {fmt.cpus}',
        'Set-VMFirmware -VMName $vmName -EnableSecureBoot '
        + ("On" if fmt.firmware == "uefi" else "Off"),
    ]
    if fmt.tpm:
        lines += [
            'Set-VMKeyProtector -VMName $vmName -NewLocalKeyProtector',
            'Enable-VMTPM -VMName $vmName',
        ]
    if fmt.serial:
        lines.append(
            f'Set-VMComPort -VMName $vmName -Number 1 -Path "\\\\.\\pipe\\{name}-serial"')
    return "\n".join(lines) + "\n"


# type -> descriptor generator, for a type whose own converted disk is
# not already a launchable VM by itself. 'ova' needs none -- the package
# it builds already is one. 'vmdk'/'vdi'/'qcow2'/'raw' get none either:
# a bare disk file for attaching to a VM by hand.
DESCRIPTOR_BY_TYPE = {
    "vhdx": Generator(".ps1", False, _render_hyperv),
}


def _check_settings(path, settings, allowed):
    if type(settings) != type({}):
        raise ValueError(f"'{path}' shall be a dictionary")
    for key in settings:
        if key not in allowed:
            raise ValueError(
                f"'{path}' has no '{key}' setting, expected one of "
                + ", ".join(sorted(allowed)))


def _check_scalar_types(path, settings):
    if settings["firmware"] not in ("uefi", "bios"):
        raise ValueError(f"'{path}: firmware' shall be 'uefi' or 'bios'")
    for key in ("tpm", "serial", "thin-provisioning"):
        if type(settings[key]) != type(True):
            raise ValueError(f"'{path}: {key}' shall be true or false")
    for key in ("cpus", "memory"):
        if type(settings[key]) != type(1) or settings[key] < 1:
            raise ValueError(f"'{path}: {key}' shall be a positive integer")


def _parse_scalars(path, settings, base):
    _check_settings(path, settings, DEFAULTS)
    merged = dict(base)
    merged.update(settings)
    _check_scalar_types(path, merged)
    return merged


class VmImages:
    def __init__(self, image):
        self.image = image
        self.formats = []

    # No 'vms:' section: nothing to do, tasks() then returns [].
    def parse(self, spec):
        vms = spec.get("vms")
        if vms is None:
            return
        if "image" not in spec:
            raise ValueError("'vms' needs an 'image' section to convert")
        _check_settings("vms", vms, ("defaults", "formats"))
        defaults = _parse_scalars("vms: defaults", vms.get("defaults", {}), DEFAULTS)

        formats = vms.get("formats")
        if type(formats) != type({}) or len(formats) == 0:
            raise ValueError("'vms: formats' shall be a non-empty dictionary")
        for name, settings in formats.items():
            self.formats.append(self._parse_format(name, settings, defaults))

    def _parse_format(self, name, settings, defaults):
        path = f"vms: formats: {name}"
        if type(settings) != type({}):
            raise ValueError(f"'{path}' shall be a dictionary")
        vtype = settings.get("type")
        if vtype not in VM_FORMATS:
            raise ValueError(
                f"'{path}: type' shall be one of " + ", ".join(sorted(VM_FORMATS)))
        options = settings.get("options", {})
        if type(options) != type({}) or any(
                type(k) != type("") or type(v) != type("") for k, v in options.items()):
            raise ValueError(f"'{path}: options' shall be a dictionary of strings")

        own = {k: v for k, v in settings.items() if k not in ("type", "options")}
        merged = _parse_scalars(path, own, defaults)
        return VmFormat(
            name=name, type=vtype, options=dict(options),
            cpus=merged["cpus"], memory=merged["memory"],
            firmware=merged["firmware"], tpm=merged["tpm"],
            serial=merged["serial"], thin_provisioning=merged["thin-provisioning"])

    # '<image-stem>-<format-name><ext>', beside the main image -- always
    # suffixed by name so two formats sharing the same 'type:' (e.g. two
    # vmdk variants) never collide.
    def _disk_output(self, fmt):
        stem = os.path.splitext(self.image._output)[0]
        return f"{stem}-{fmt.name}{VM_FORMATS[fmt.type]}"

    def _descriptor_output(self, fmt):
        generator = DESCRIPTOR_BY_TYPE.get(fmt.type)
        if generator is None:
            return None
        stem = os.path.splitext(self.image._output)[0]
        return f"{stem}-{fmt.name}{generator.extension}"

    def tasks(self):
        return [Task(f"vm-image-{fmt.name}", lambda fmt=fmt: self._build(fmt),
                     needs=["image"], resource="io")
                for fmt in self.formats]

    def _convert_options(self, fmt):
        options = dict(fmt.options)
        thin = THIN_OPTIONS.get(fmt.type, {}).get(fmt.thin_provisioning)
        if thin is not None:
            key, value = thin
            options.setdefault(key, value)
        return options

    def _convert_digest(self, fmt):
        parts = [CONVERT_REVISION, self.image._image_digest(), fmt.type,
                 sorted(self._convert_options(fmt).items())]
        return hashlib.sha256(repr(parts).encode()).hexdigest()

    def _convert(self, fmt):
        if fmt.type == "ova":
            self._build_ova(fmt)
            return
        output = self._disk_output(fmt)
        digest_file = f"{output}.digest"
        digest = self._convert_digest(fmt)
        if utils.digest_file_current(digest_file, digest, output):
            print(f"VM image up to date ({utils.display_path(output)})")
            return
        utils.invalidate_digest_file(digest_file)
        options = self._convert_options(fmt)
        cmd = ["qemu-img", "convert", "-O", fmt.type]
        if options:
            cmd += ["-o", ",".join(f"{k}={v}" for k, v in sorted(options.items()))]
        cmd += [self.image._output, output]
        subprocess.run(cmd, check=True)
        utils.write_digest_file(digest_file, digest)

    def _ova_digest(self, fmt):
        # Ignores 'options'/'thin-provisioning': the OVA's own disk is
        # always stream-optimized VMDK, the packaging convention every
        # OVF-importing tool expects, regardless of what a bare 'vmdk'
        # entry was asked for.
        parts = [CONVERT_REVISION, self.image._image_digest(), "ova",
                 fmt.cpus, fmt.memory, fmt.firmware]
        return hashlib.sha256(repr(parts).encode()).hexdigest()

    # An OVA is an uncompressed tar of an OVF XML descriptor, a manifest
    # of its checksums, and the disk -- in that order, the layout every
    # OVF-importing tool (VMware, VirtualBox) expects.
    def _build_ova(self, fmt):
        output = self._disk_output(fmt)
        digest_file = f"{output}.digest"
        digest = self._ova_digest(fmt)
        if utils.digest_file_current(digest_file, digest, output):
            print(f"VM image up to date ({utils.display_path(output)})")
            return
        utils.invalidate_digest_file(digest_file)

        with tempfile.TemporaryDirectory(dir=os.path.dirname(output)) as tmp:
            disk_name = f"{fmt.name}-disk1.vmdk"
            disk_path = os.path.join(tmp, disk_name)
            subprocess.run(
                ["qemu-img", "convert", "-O", "vmdk", "-o", "subformat=streamOptimized",
                 self.image._output, disk_path], check=True)

            ovf_name = f"{fmt.name}.ovf"
            ovf_path = os.path.join(tmp, ovf_name)
            with open(ovf_path, "w") as f:
                f.write(OVF_TEMPLATE.format(
                    name=fmt.name, disk_name=disk_name,
                    disk_size=os.path.getsize(disk_path),
                    capacity=os.path.getsize(self.image._output),
                    cpus=fmt.cpus, memory=fmt.memory,
                    firmware="efi" if fmt.firmware == "uefi" else "bios"))

            mf_name = f"{fmt.name}.mf"
            mf_path = os.path.join(tmp, mf_name)
            with open(mf_path, "w") as f:
                f.write(f"SHA256({ovf_name})= {utils.file_digest(ovf_path)}\n")
                f.write(f"SHA256({disk_name})= {utils.file_digest(disk_path)}\n")

            # GNU format, not tarfile's own PAX default: VirtualBox's OVA
            # reader rejects a PAX extended header outright
            # (VERR_TAR_UNSUPPORTED_PAX_TYPE), caught by actually
            # importing a built OVA rather than just reading it back.
            partial = f"{output}.partial"
            with tarfile.open(partial, "w", format=tarfile.GNU_FORMAT) as tar:
                for name, path in ((ovf_name, ovf_path), (mf_name, mf_path),
                                   (disk_name, disk_path)):
                    tar.add(path, arcname=name)
            os.replace(partial, output)

        utils.write_digest_file(digest_file, digest)

    def _descriptor_digest(self, fmt, disk_output):
        parts = [DESCRIPTOR_REVISION, fmt.type, fmt.cpus, fmt.memory,
                 fmt.firmware, fmt.tpm, fmt.serial, disk_output]
        return hashlib.sha256(repr(parts).encode()).hexdigest()

    # Kept separate from the digest that guards _convert(): a launch
    # setting like 'cpus' should not force a whole disk to be re-converted.
    def _write_descriptor(self, fmt):
        generator = DESCRIPTOR_BY_TYPE.get(fmt.type)
        if generator is None:
            return
        disk_output = self._disk_output(fmt)
        output = self._descriptor_output(fmt)
        digest_file = f"{output}.digest"
        digest = self._descriptor_digest(fmt, disk_output)
        if utils.digest_file_current(digest_file, digest, output):
            print(f"VM descriptor up to date ({utils.display_path(output)})")
            return
        utils.invalidate_digest_file(digest_file)
        with open(output, "w") as f:
            f.write(generator.render(fmt, os.path.basename(disk_output)))
        if generator.executable:
            os.chmod(output, os.stat(output).st_mode | 0o111)
        utils.write_digest_file(digest_file, digest)

    def _build(self, fmt):
        self._convert(fmt)
        self._write_descriptor(fmt)
