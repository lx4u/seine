package main

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"strings"
	"time"

	pkcs7 "seine-pkcs7"
)

// Default attributes for PK/KEK/db/dbx: non-volatile, boot/runtime
// visible, and authenticated by a timestamp -- what sign-efi-sig-list
// itself writes for these four variables.
const defaultVariableAttributes = 0x27

var certTypePKCS7 = mustGUIDBytes("4aafd29d-68df-49ee-8aa9-347d375665a7")

// Canonical dashed string -> the mixed-endian bytes EFI itself writes
// (Data1-3 little-endian, Data4 as-is). Verified byte-for-byte against
// sign-efi-sig-list's own "-o" bundle for 'db'.
func guidBytes(s string) ([16]byte, error) {
	var out [16]byte
	raw, err := hex.DecodeString(strings.ReplaceAll(s, "-", ""))
	if err != nil || len(raw) != 16 {
		return out, fmt.Errorf("guid shall be 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'")
	}
	out[0], out[1], out[2], out[3] = raw[3], raw[2], raw[1], raw[0]
	out[4], out[5] = raw[5], raw[4]
	out[6], out[7] = raw[7], raw[6]
	copy(out[8:], raw[8:16])
	return out, nil
}

func mustGUIDBytes(s string) [16]byte {
	out, err := guidBytes(s)
	if err != nil {
		panic(err)
	}
	return out
}

// EFI_TIME, 16 bytes: only Year/Month/Day/Hour/Minute/Second carry
// meaning here, the rest (Pad1, Nanosecond, TimeZone, Daylight, Pad2)
// zeroed -- what both the digest buffer and the final header embed.
func efiTimeBytes(t time.Time) [16]byte {
	var out [16]byte
	binary.LittleEndian.PutUint16(out[0:], uint16(t.Year()))
	out[2] = byte(t.Month())
	out[3] = byte(t.Day())
	out[4] = byte(t.Hour())
	out[5] = byte(t.Minute())
	out[6] = byte(t.Second())
	return out
}

// UTF-16LE, no terminating NUL -- efi-updatevar/sign-efi-sig-list hash
// the variable name exactly as SetVariable() receives it.
func utf16le(s string) []byte {
	out := make([]byte, 0, len(s)*2)
	for _, r := range s {
		out = append(out, byte(r), byte(r>>8))
	}
	return out
}

// The buffer EFI_VARIABLE_AUTHENTICATION_2 signs: variable name +
// vendor GUID + attributes + timestamp + the new value. Both the
// digest and the CertData's embedded certificate cover this same
// buffer -- verified against sign-efi-sig-list -o's own bundle.
func authVarDigestBuffer(varName string, guid [16]byte, attributes uint32, stamp [16]byte, value []byte) []byte {
	buf := utf16le(varName)
	buf = append(buf, guid[:]...)
	var attrBytes [4]byte
	binary.LittleEndian.PutUint32(attrBytes[:], attributes)
	buf = append(buf, attrBytes[:]...)
	buf = append(buf, stamp[:]...)
	buf = append(buf, value...)
	return buf
}

// Builds the full '.auth' blob efi-updatevar -f expects: the
// EFI_VARIABLE_AUTHENTICATION_2 header (timestamp + WIN_CERTIFICATE_
// UEFI_GUID, CertType EFI_CERT_TYPE_PKCS7_GUID) followed by the new
// variable value -- byte layout verified against sign-efi-sig-list.
func signVariable(pair *pkcs7.KeyPair, varName, guidString string, attributes uint32, value []byte, signingTime time.Time) ([]byte, error) {
	guid, err := guidBytes(guidString)
	if err != nil {
		return nil, err
	}
	stamp := efiTimeBytes(signingTime)
	digestBuffer := authVarDigestBuffer(varName, guid, attributes, stamp, value)
	cms, err := pkcs7.SignDetachedWithCert(digestBuffer, pkcs7.OIDSHA256, pair.Key, pair.Cert)
	if err != nil {
		return nil, err
	}

	out := append([]byte{}, stamp[:]...)
	var header [8]byte
	binary.LittleEndian.PutUint32(header[0:], uint32(8+len(certTypePKCS7)+len(cms)))
	binary.LittleEndian.PutUint16(header[4:], 0x0200)
	binary.LittleEndian.PutUint16(header[6:], 0x0ef1)
	out = append(out, header[:]...)
	out = append(out, certTypePKCS7[:]...)
	out = append(out, cms...)
	out = append(out, value...)
	return out, nil
}
