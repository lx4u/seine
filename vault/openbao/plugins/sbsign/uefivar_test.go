package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
	"time"

	pkcs7 "seine-pkcs7"
)

// The GUID sign-efi-sig-list itself picks for 'db'/'dbx' -- verified
// against its own '-o' bundle, not taken from its (unused, for this
// purpose) '-g' flag.
const efiImageSecurityDatabaseGUID = "d719b2cb-3d3a-4596-a3bc-dad00e67656f"

func readTestdata(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "uefivar", name))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// signVariable() reproduces sign-efi-sig-list's own '-t "2020-01-01"'
// output byte-for-byte: the fixture is exactly what that host tool
// wrote, key and cert included.
func TestSignVariableMatchesSignEfiSigListByteForByte(t *testing.T) {
	keyPEM := string(readTestdata(t, "db.key.pem"))
	certPEM := string(readTestdata(t, "db.cert.pem"))
	key, cert, err := pkcs7.ParseKeyPair(keyPEM, certPEM)
	if err != nil {
		t.Fatal(err)
	}
	esl := readTestdata(t, "db.esl")
	want := readTestdata(t, "db.auth")

	stamp := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
	got, err := signVariable(&pkcs7.KeyPair{Key: key, Cert: cert}, "db",
		efiImageSecurityDatabaseGUID, defaultVariableAttributes, esl, stamp)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("mismatch: got %d bytes, want %d bytes", len(got), len(want))
	}
}

func TestSignVariableIsDeterministic(t *testing.T) {
	keyPEM := string(readTestdata(t, "db.key.pem"))
	certPEM := string(readTestdata(t, "db.cert.pem"))
	key, cert, err := pkcs7.ParseKeyPair(keyPEM, certPEM)
	if err != nil {
		t.Fatal(err)
	}
	esl := readTestdata(t, "db.esl")
	stamp := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
	pair := &pkcs7.KeyPair{Key: key, Cert: cert}
	first, err := signVariable(pair, "db", efiImageSecurityDatabaseGUID,
		defaultVariableAttributes, esl, stamp)
	if err != nil {
		t.Fatal(err)
	}
	second, err := signVariable(pair, "db", efiImageSecurityDatabaseGUID,
		defaultVariableAttributes, esl, stamp)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(first, second) {
		t.Fatal("same input and time signed differently")
	}
}

func TestGuidBytesRejectsGarbage(t *testing.T) {
	if _, err := guidBytes("not-a-guid"); err == nil {
		t.Fatal("garbage guid accepted")
	}
}
