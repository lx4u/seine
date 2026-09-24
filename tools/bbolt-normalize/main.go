// seine - Slim Embedded Images Now Easy
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"go.etcd.io/bbolt"
)

type entry struct {
	key      []byte
	val      []byte
	isBucket bool
}

func main() {
	timeKeysFlag := flag.String("time-keys", "createdat,updatedat,expireat,expiresat,created_at,updated_at,expire_at,expires_at", "comma-separated list of key names whose Go binary timestamps should be normalized")
	zeroKeysFlag := flag.String("zero-keys", "", "comma-separated list of key names whose values should be zeroed (e.g. size,inodes)")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "usage: bbolt-normalize [--time-keys=k1,k2,...] [--zero-keys=k1,k2,...] <db-path> [epoch]\n")
		flag.PrintDefaults()
	}
	flag.Parse()

	args := flag.Args()
	if len(args) < 1 {
		flag.Usage()
		os.Exit(1)
	}

	dbPath := args[0]
	epoch := int64(0)
	if len(args) >= 2 {
		v, err := strconv.ParseInt(args[1], 10, 64)
		if err == nil {
			epoch = v
		}
	}

	timeKeys := make([][]byte, 0)
	if *timeKeysFlag != "" {
		for _, k := range strings.Split(*timeKeysFlag, ",") {
			k = strings.TrimSpace(k)
			if k != "" {
				timeKeys = append(timeKeys, []byte(k))
			}
		}
	}

	zeroKeys := make([][]byte, 0)
	if *zeroKeysFlag != "" {
		for _, k := range strings.Split(*zeroKeysFlag, ",") {
			k = strings.TrimSpace(k)
			if k != "" {
				zeroKeys = append(zeroKeys, []byte(k))
			}
		}
	}

	if err := normalize(dbPath, epoch, timeKeys, zeroKeys); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// Rewrites the database in sorted order with the volatile values cleared.
// Each bucket goes in a transaction of its own: bbolt allocates the pages
// of the sibling buckets of one transaction in map order, which is random.
func normalize(dbPath string, epoch int64, timeKeys, zeroKeys [][]byte) error {
	tmpDst := dbPath + ".compacted"
	defer os.Remove(tmpDst)

	srcDB, err := bbolt.Open(dbPath, 0600, &bbolt.Options{ReadOnly: true})
	if err != nil {
		return fmt.Errorf("failed to open %s: %v", dbPath, err)
	}
	defer srcDB.Close()

	dstDB, err := bbolt.Open(tmpDst, 0600, &bbolt.Options{NoSync: true})
	if err != nil {
		return fmt.Errorf("failed to open temporary output %s: %v", tmpDst, err)
	}

	epochBytes, _ := time.Unix(epoch, 0).UTC().MarshalBinary()

	err = srcDB.View(func(txSrc *bbolt.Tx) error {
		var rootNames [][]byte
		_ = txSrc.ForEach(func(name []byte, _ *bbolt.Bucket) error {
			rootNames = append(rootNames, append([]byte(nil), name...))
			return nil
		})
		sort.Slice(rootNames, func(i, j int) bool {
			return bytes.Compare(rootNames[i], rootNames[j]) < 0
		})
		for _, name := range rootNames {
			err := copyBucket(dstDB, txSrc.Bucket(name), [][]byte{name}, epochBytes, timeKeys, zeroKeys)
			if err != nil {
				return err
			}
		}
		return nil
	})
	dstDB.Close()
	if err != nil {
		return fmt.Errorf("compaction error for %s: %v", dbPath, err)
	}

	if err := os.Rename(tmpDst, dbPath); err != nil {
		return fmt.Errorf("rename %s to %s failed: %v", tmpDst, dbPath, err)
	}
	return nil
}

func matchesKey(k []byte, keys [][]byte) bool {
	for _, tk := range keys {
		if bytes.EqualFold(k, tk) {
			return true
		}
	}
	return false
}

// Copies the bucket at 'path' and, one transaction each, the buckets below it.
func copyBucket(dstDB *bbolt.DB, bSrc *bbolt.Bucket, path [][]byte, epochBytes []byte, timeKeys [][]byte, zeroKeys [][]byte) error {
	var entries []entry
	_ = bSrc.ForEach(func(k, v []byte) error {
		kCopy := append([]byte(nil), k...)
		if child := bSrc.Bucket(k); child != nil {
			entries = append(entries, entry{key: kCopy, isBucket: true})
		} else {
			vCopy := append([]byte(nil), v...)
			if matchesKey(k, zeroKeys) {
				vCopy = []byte{0}
			} else if matchesKey(k, timeKeys) {
				if len(v) == 15 && v[0] == 1 {
					vCopy = epochBytes
				}
			}
			entries = append(entries, entry{key: kCopy, val: vCopy, isBucket: false})
		}
		return nil
	})

	sort.Slice(entries, func(i, j int) bool {
		return bytes.Compare(entries[i].key, entries[j].key) < 0
	})

	err := dstDB.Update(func(tx *bbolt.Tx) error {
		bDst, err := tx.CreateBucketIfNotExists(path[0])
		if err != nil {
			return err
		}
		for _, name := range path[1:] {
			if bDst, err = bDst.CreateBucketIfNotExists(name); err != nil {
				return err
			}
		}
		for _, e := range entries {
			if !e.isBucket {
				if err := bDst.Put(e.key, e.val); err != nil {
					return err
				}
			}
		}
		return bDst.SetSequence(bSrc.Sequence())
	})
	if err != nil {
		return err
	}

	for _, e := range entries {
		if e.isBucket {
			childPath := append(append([][]byte(nil), path...), e.key)
			if err := copyBucket(dstDB, bSrc.Bucket(e.key), childPath, epochBytes, timeKeys, zeroKeys); err != nil {
				return err
			}
		}
	}
	return nil
}
