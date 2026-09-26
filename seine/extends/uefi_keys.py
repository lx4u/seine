# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: uefi-keys:' means: a Debian package, generated
# entirely by seine, that installs PK/KEK/db/dbx signature lists and
# enrolls them into NVRAM (via a systemd service, seine/data/uefi-keys/
# service) while firmware is still in Setup Mode. No upstream to fetch.

import os
import uuid

from seine.extends import parsing
from seine.extends import templates

SETTINGS = ["db", "dbx", "include-microsoft-keys", "include-standard-dbx",
            "kek", "pk", "reboot", "signing-key"]

def is_uefi_keys_package(package):
    return getattr(package, "uefi_keys", False)

def _vault_list(package, path, value, fallback):
    if value is None:
        return list(fallback)
    if type(value) == type(""):
        value = [value]
    if type(value) != type([]):
        raise package._error(
            "'%s' shall be a string or a list of strings" % path)
    return [parsing.vault_name(package, path, item) for item in value]

def parse(package, extends):
    settings = extends.get("uefi-keys", {})
    package.uefi_keys = "uefi-keys" in extends
    if package.uefi_keys == False:
        package.uefi_keys_pk = None
        package.uefi_keys_kek = []
        package.uefi_keys_db = []
        package.uefi_keys_dbx = []
        package.uefi_keys_reboot = False
        return

    parsing.require_generated_source(package, "uefi-keys")

    # No fallback to 'image: secure-boot: private-key': package
    # preparation (where this runs) never sees the image spec, only
    # this package's own settings.
    signing_key = parsing.parse_vault_key(
        package, "uefi-keys", settings, "signing-key")
    fallback = [signing_key] if signing_key else []

    pk = parsing.parse_vault_key(package, "uefi-keys", settings, "pk")
    if pk is None:
        if signing_key is None:
            raise package._error(
                "'extends: uefi-keys' needs 'pk' or a fallback "
                "'signing-key' to name the Platform Key")
        package.uefi_keys_pk = signing_key
    else:
        package.uefi_keys_pk = pk

    package.uefi_keys_kek = _vault_list(
        package, "extends: uefi-keys: kek", settings.get("kek"), fallback)
    if len(package.uefi_keys_kek) == 0:
        raise package._error(
            "'extends: uefi-keys' needs 'kek' or a fallback 'signing-key'")

    package.uefi_keys_db = _vault_list(
        package, "extends: uefi-keys: db", settings.get("db"), fallback)
    if len(package.uefi_keys_db) == 0:
        raise package._error(
            "'extends: uefi-keys' needs 'db' or a fallback 'signing-key'")

    package.uefi_keys_dbx = _vault_list(
        package, "extends: uefi-keys: dbx", settings.get("dbx"), [])

    # No bundled trust anchors ship in this checkout: refusing beats
    # silently enrolling nothing, or fabricating certificates nobody
    # can audit. Name the certs individually under 'dbx:'/'kek:' instead.
    if parsing.parse_bool(package, "uefi-keys", settings,
                          "include-standard-dbx"):
        raise package._error(
            "'extends: uefi-keys: include-standard-dbx' has no bundled "
            "UEFI Forum revocation list in this checkout -- list its "
            "certificates individually under 'dbx:' instead")
    if parsing.parse_bool(package, "uefi-keys", settings,
                          "include-microsoft-keys"):
        raise package._error(
            "'extends: uefi-keys: include-microsoft-keys' has no bundled "
            "Microsoft certificates in this checkout -- list them "
            "individually under 'kek:'/'db:' instead")

    package.uefi_keys_reboot = parsing.parse_bool(
        package, "uefi-keys", settings, "reboot")

UEFI_KEYS_FILES = ("changelog", "control", "rules", "service",
                   "check-setup-mode", "provision-keys")

def uefi_keys_packaging():
    return templates.load_templates("uefi-keys", UEFI_KEYS_FILES)

# One '.pem' per referenced key, host side: sbuild's chroot has no
# network to reach a vault from, so every cert this package needs is
# fetched and written before the chroot ever starts (module.py's
# kernel/apply.py does the same for a MODULE_SIG_KEY trusted cert).
def _write_certs(vault, certdir, package):
    os.makedirs(certdir, exist_ok=True)
    templates.write(os.path.join(certdir, "pk.pem"),
                    vault.sbsign_cert(package.uefi_keys_pk))
    for role in ("kek", "db", "dbx"):
        for i, name in enumerate(getattr(package, "uefi_keys_%s" % role)):
            templates.write(os.path.join(certdir, "%s-%d.pem" % (role, i)),
                            vault.sbsign_cert(name))

# One shell recipe line per role: convert each cert to an EFI
# Signature List, then concatenate a role's lists into the file
# provision-keys enrolls -- efi-updatevar/KeyTool both read several
# EFI_SIGNATURE_LIST structures back to back as one update. pk.auth
# (not pk.esl): PK always needs an authenticated write, so Builder
# resigns this file with the vault post-build (seine/uefi_auth_sign.py)
# -- it is an unsigned ESL wearing that name until then.
def _install_commands(package):
    lines = ["cert-to-efi-sig-list -g $(OWNER_GUID) debian/certs/pk.pem "
            "debian/$(PACKAGE)/usr/share/$(PACKAGE)/pk.auth"]
    for role in ("kek", "db", "dbx"):
        names = getattr(package, "uefi_keys_%s" % role)
        if len(names) == 0:
            continue
        esls = []
        for i in range(len(names)):
            pem = "debian/certs/%s-%d.pem" % (role, i)
            esl = "debian/certs/%s-%d.esl" % (role, i)
            lines.append("cert-to-efi-sig-list -g $(OWNER_GUID) %s %s"
                         % (pem, esl))
            esls.append(esl)
        lines.append("cat %s > debian/$(PACKAGE)/usr/share/$(PACKAGE)/%s.esl"
                     % (" ".join(esls), role))
    return "; \\\n\t".join(lines)

def extend(builder, package, sourcedir, epoch):
    if package.uefi_keys == False:
        return

    debian = templates.reset_debian(sourcedir)

    _write_certs(builder._vault(), os.path.join(debian, "certs"), package)

    context = {
        **templates.base_context(package, epoch),
        # Stable per package name, not random: identical specs keep
        # identical digests across runs.
        "owner_guid": str(uuid.uuid5(uuid.NAMESPACE_DNS, package.name)),
        "install_commands": _install_commands(package),
        "reboot": package.uefi_keys_reboot,
    }
    templates.render_files(
        debian, uefi_keys_packaging()[0], context,
        names={"service": f"{package.name}.service"})
