// seine - Slim Embedded Images Now Easy
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"go.etcd.io/bbolt"
)

// Sibling buckets too big to be inlined: the case where bbolt's page
// layout used to depend on the order of a map.
func source(t *testing.T, path string) {
	db, err := bbolt.Open(path, 0600, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	err = db.Update(func(tx *bbolt.Tx) error {
		root, err := tx.CreateBucket([]byte("v1"))
		if err != nil {
			return err
		}
		for i := 0; i < 8; i++ {
			child, err := root.CreateBucket([]byte(fmt.Sprintf("ns%d", i)))
			if err != nil {
				return err
			}
			for j := 0; j < 40; j++ {
				key := []byte(fmt.Sprintf("key%03d", j))
				if err := child.Put(key, bytes.Repeat([]byte{byte(i)}, 100)); err != nil {
					return err
				}
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestSameDatabaseEveryTime(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "source.db")
	source(t, src)
	original, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}

	var first []byte
	for i := 0; i < 30; i++ {
		path := filepath.Join(dir, "copy.db")
		if err := os.WriteFile(path, original, 0600); err != nil {
			t.Fatal(err)
		}
		if err := normalize(path, 0, nil, nil); err != nil {
			t.Fatal(err)
		}
		got, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if first == nil {
			first = got
		} else if !bytes.Equal(first, got) {
			t.Fatalf("run %d produced a different file", i)
		}
	}
}
