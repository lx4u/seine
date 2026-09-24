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

	tmpDst := dbPath + ".compacted"
	defer os.Remove(tmpDst)

	srcDB, err := bbolt.Open(dbPath, 0600, &bbolt.Options{ReadOnly: true})
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to open %s: %v\n", dbPath, err)
		os.Exit(1)
	}

	dstDB, err := bbolt.Open(tmpDst, 0600, &bbolt.Options{NoSync: true})
	if err != nil {
		srcDB.Close()
		fmt.Fprintf(os.Stderr, "failed to open temporary output %s: %v\n", tmpDst, err)
		os.Exit(1)
	}

	epochBytes, _ := time.Unix(epoch, 0).UTC().MarshalBinary()

	err = dstDB.Update(func(txDst *bbolt.Tx) error {
		return srcDB.View(func(txSrc *bbolt.Tx) error {
			var rootNames [][]byte
			_ = txSrc.ForEach(func(name []byte, _ *bbolt.Bucket) error {
				nameCopy := make([]byte, len(name))
				copy(nameCopy, name)
				rootNames = append(rootNames, nameCopy)
				return nil
			})
			sort.Slice(rootNames, func(i, j int) bool {
				return bytes.Compare(rootNames[i], rootNames[j]) < 0
			})

			for _, name := range rootNames {
				bSrc := txSrc.Bucket(name)
				bDst, err := txDst.CreateBucketIfNotExists(name)
				if err != nil {
					return err
				}
				if err := copyBucket(bSrc, bDst, epochBytes, timeKeys, zeroKeys); err != nil {
					return err
				}
			}
			return nil
		})
	})

	srcDB.Close()
	dstDB.Close()

	if err != nil {
		fmt.Fprintf(os.Stderr, "compaction error for %s: %v\n", dbPath, err)
		os.Exit(1)
	}

	if err := os.Rename(tmpDst, dbPath); err != nil {
		fmt.Fprintf(os.Stderr, "rename %s to %s failed: %v\n", tmpDst, dbPath, err)
		os.Exit(1)
	}
}

func matchesKey(k []byte, keys [][]byte) bool {
	for _, tk := range keys {
		if bytes.EqualFold(k, tk) {
			return true
		}
	}
	return false
}

func copyBucket(bSrc, bDst *bbolt.Bucket, epochBytes []byte, timeKeys [][]byte, zeroKeys [][]byte) error {
	var entries []entry
	_ = bSrc.ForEach(func(k, v []byte) error {
		kCopy := make([]byte, len(k))
		copy(kCopy, k)
		if child := bSrc.Bucket(k); child != nil {
			entries = append(entries, entry{key: kCopy, isBucket: true})
		} else {
			vCopy := make([]byte, len(v))
			copy(vCopy, v)
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

	// Put all key-values first
	for _, e := range entries {
		if !e.isBucket {
			if err := bDst.Put(e.key, e.val); err != nil {
				return err
			}
		}
	}

	// Then recursively create and copy child buckets in sorted order
	for _, e := range entries {
		if e.isBucket {
			childSrc := bSrc.Bucket(e.key)
			childDst, err := bDst.CreateBucketIfNotExists(e.key)
			if err != nil {
				return err
			}
			if err := copyBucket(childSrc, childDst, epochBytes, timeKeys, zeroKeys); err != nil {
				return err
			}
		}
	}

	return bDst.SetSequence(bSrc.Sequence())
}
