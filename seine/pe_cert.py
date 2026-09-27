# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7

WIN_CERT_TYPE_PKCS_SIGNED_DATA = 0x0002
IMAGE_DIRECTORY_ENTRY_SECURITY = 4

# The Security directory's VirtualAddress is a raw file offset, not an
# RVA like every other PE data directory -- Authenticode's one exception.
def _security_directory(pe):
    if pe[0:2] != b"MZ" or len(pe) < 0x40:
        return None
    pe_offset = struct.unpack_from("<I", pe, 0x3c)[0]
    if pe[pe_offset:pe_offset + 4] != b"PE\0\0":
        return None
    optional_header = pe_offset + 4 + 20
    magic = struct.unpack_from("<H", pe, optional_header)[0]
    if magic == 0x10b:
        data_directory = optional_header + 96
    elif magic == 0x20b:
        data_directory = optional_header + 112
    else:
        return None
    entry = data_directory + IMAGE_DIRECTORY_ENTRY_SECURITY * 8
    offset, size = struct.unpack_from("<II", pe, entry)
    return (offset, size) if size else None

# Authenticode WIN_CERTIFICATE structures are padded with null bytes to
# align entries on 8-byte boundaries. Trim trailing padding by reading
# the ASN.1 length of the outer TLV so strict DER parsers don't fail
# with ExtraData.
def _der_payload(data):
    if len(data) < 2:
        return data
    if data[1] < 0x80:
        total = 2 + data[1]
    elif data[1] == 0x80:
        return data
    else:
        num_octets = data[1] & 0x7f
        if len(data) < 2 + num_octets:
            return data
        total = 2 + num_octets + int.from_bytes(data[2:2 + num_octets], "big")
    return data[:total] if total <= len(data) else data

# The leaf's subject never appears as another embedded cert's issuer;
# falls back to the first cert if that heuristic can't tell.
def _leaf(certs):
    issuers = {cert.issuer for cert in certs}
    leaves = [cert for cert in certs if cert.subject not in issuers]
    return leaves[0] if len(leaves) == 1 else certs[0]

# Reads the Authenticode signer's certificate straight out of a signed
# PE's bytes, or None if it carries no PKCS#7 signed-data certificate.
def extract_signer_cert(pe):
    directory = _security_directory(pe)
    if directory is None:
        return None
    offset, end = directory[0], directory[0] + directory[1]
    while offset < end:
        length, revision, cert_type = struct.unpack_from("<IHH", pe, offset)
        if cert_type == WIN_CERT_TYPE_PKCS_SIGNED_DATA:
            data = _der_payload(pe[offset + 8:offset + length])
            certs = pkcs7.load_der_pkcs7_certificates(data)
            if certs:
                return _leaf(certs)
        offset += (length + 7) & ~7  # entries are 8-byte aligned

def fingerprint(cert):
    return cert.fingerprint(hashes.SHA256()).hex()

def subject(cert):
    return cert.subject.rfc4514_string()

def to_pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)
