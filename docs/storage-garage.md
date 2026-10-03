# Running a Garage S3 cache for seine

seine can share build caches (custom `.deb` packages, sbuild chroots,
container bootstraps, and root file-systems) across multiple machines
through network object storage, via `--shared-cache` or `storage.s3` in a
spec. This page shows how to stand up a lightweight S3 cache server using
[Garage](https://garagehq.deuxfleurs.fr/), running as a container on a
server or NAS of your choice.

**Security disclaimer.** This page gets an S3 cache running and reachable
-- nothing more. It makes no claim about how secure that instance is.
Network exposure, access keys, TLS certificates, firewall rules, and
backups are for you to review and harden before trusting this in
production. Treat what follows as a quick start for an admin who already
knows how to run storage services, not as a hardening guide.

## 1. Get the Garage container image

Pull the official Garage container image:

```
podman pull docker.io/dxflrs/garage:v1.1.0
```

Alternatively, download a statically linked binary from
[Garage releases](https://garagehq.deuxfleurs.fr/) if you prefer running
it directly as a systemd service.

## 2. Configure and start the server

Generate a random 32-byte hexadecimal RPC secret for cluster communication:

```
openssl rand -hex 32
```

Create a directory for configuration and storage:

```
mkdir -p garage/config garage/meta garage/data
```

Write `garage/config/garage.toml`:

```toml
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"

replication_factor = 1

rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "<paste-your-32-byte-hex-secret-here>"

[s3_api]
api_bind_addr = "[::]:3900"
s3_region = "garage"

[s3_web]
bind_addr = "[::]:3902"
root_domain = ".web.garage"
```

Start the container with Podman, bind-mounting the config, metadata, and
data directories (`:U` sets user ownership inside rootless containers):

```
podman run -d --name seine-garage \
  -p 3900:3900 \
  -p 3901:3901 \
  -v ./garage/config:/etc/garage:ro \
  -v ./garage/meta:/var/lib/garage/meta:U \
  -v ./garage/data:/var/lib/garage/data:U \
  docker.io/dxflrs/garage:v1.1.0 \
  server -c /etc/garage/garage.toml
```

Check the server logs to verify it started cleanly:

```
podman logs seine-garage
```

## 3. Initialize node and layout

Garage requires assigning storage capacity to nodes before it can store
data. Check the node ID:

```
podman exec seine-garage garage status
```

This outputs a node identifier (a 64-character hex string). Assign the
node to a zone (e.g. `dc1`) with a capacity limit (e.g. `100G`):

```
podman exec seine-garage garage layout assign <node-id> -z dc1 -c 100G
```

Review and apply the staged layout:

```
podman exec seine-garage garage layout show
podman exec seine-garage garage layout apply --version 1
```

Once applied, the node status shows `HEALTHY` and storage is active.

## 4. Create the cache bucket and access key

Create an API key for seine:

```
podman exec seine-garage garage key create seine-key
```

This prints an **Access key ID** (e.g. `GK...`) and a **Secret access key**
(a 64-character hex string). Save these credentials.

Create the bucket to hold seine's cache:

```
podman exec seine-garage garage bucket create seine-cache
```

Grant read and write permissions on the bucket to `seine-key`:

```
podman exec seine-garage garage bucket allow seine-cache \
  --read --write --key seine-key
```

Verify the bucket configuration:

```
podman exec seine-garage garage bucket info seine-cache
```

## 5. Point a build at it

You can configure seine to use your Garage cache through command-line
options, persistent user credentials, or directly in specification files.

### Option A: Command-line options

Pass the endpoint and bucket to `seine build`:

```
seine build \
  --shared-cache \
  --s3-endpoint=http://<your-host>:3900 \
  --s3-bucket=seine-cache \
  --cache-rootfs \
  your-spec.yaml
```

Set credentials via environment variables:

```
export AWS_ACCESS_KEY_ID="<your-access-key-id>"
export AWS_SECRET_ACCESS_KEY="<your-secret-access-key>"
```

### Option B: Persistent credentials file

Store credentials in `~/.config/seine/credentials.json`:

```json
{
  "s3-endpoint": "http://<your-host>:3900",
  "s3-bucket": "seine-cache",
  "s3-region": "garage",
  "s3-access-key": "<your-access-key-id>",
  "s3-secret-key": "<your-secret-access-key>"
}
```

Make sure the file permissions are restricted to your user:

```
chmod 600 ~/.config/seine/credentials.json
```

Once saved, simply pass `--shared-cache` (or `--cache-rootfs`) to any build:

```
seine build --shared-cache --cache-rootfs your-spec.yaml
```

### Option C: In the specification file

You can declare the cache endpoint directly in a spec:

```yaml
storage:
    s3:
        endpoint: http://<your-host>:3900
        bucket: seine-cache
        region: garage
```

Credentials are still resolved as above: the system keyring, then
`~/.config/seine/credentials.json` (or `$SEINE_CREDENTIALS_FILE`), then the
`SEINE_S3_ACCESS_KEY` / `SEINE_S3_SECRET_KEY` or `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` environment variables.

See [Caching](caching.md#network-storage-s3--garage) for details on what
seine caches in network storage, the clean-chroot gate, and diagnosing
cache hits and misses with `seine cache explain`.

## 6. Per-project keys

A distributed `seine-server` keeps development and production apart with one
key per bucket, because Garage has no temporary credentials (STS). For every
project, create the two buckets and two keys, and allow each key on its own
bucket only (read and write, and nothing else). The names below are examples;
the buckets are the ones of the project (`seine-<project>-dev` and
`seine-<project>-prod` unless you named them in `project create`):

```
podman exec seine-garage garage bucket create seine-my-project-dev
podman exec seine-garage garage bucket create seine-my-project-prod
podman exec seine-garage garage key create my-project-dev
podman exec seine-garage garage key create my-project-prod
podman exec seine-garage garage bucket allow seine-my-project-dev \
  --read --write --key my-project-dev
podman exec seine-garage garage bucket allow seine-my-project-prod \
  --read --write --key my-project-prod
```

Put the two key pairs under the project in the `storage:` section of the
server's `server.yaml` ([Storage credentials](distributed-build.md#storage-credentials)).
The server then uses the `dev` pair for the project's development builds and
hands it to their workers, and the `prod` pair only for `--release` builds.
Garage will not let a key create buckets, so create them yourself rather than
with `--provision-buckets` (which then only checks they exist).

## 7. Quota backstop

`seine-server` keeps a project's dev bucket below its own `high_water` by
deleting old artifacts (see
[Storage housekeeping](distributed-build.md#storage-housekeeping)). Garage
has a quota of its own, and it is only a hard stop: when a bucket is full,
Garage rejects every upload, which fails a build in the middle of its run.
Never rely on it as the primary mechanism.

Set it about 10% above `high_water`, so that housekeeping acts first. With
a `high_water` of 80G, for example:

```
podman exec seine-garage garage bucket set-quotas seine-my-project-dev \
  --max-size 88GiB
```

The same can be done through the Garage admin API (`quotas.maxSize` on
`PUT /v1/bucket`). Check the command against your Garage version
(`garage bucket --help`).

- **Key permissions.** Read and write on the bucket, as in
  [Per-project keys](#6-per-project-keys), is enough for everything the server
  does, including installing lifecycle rules and deleting objects. The key
  does not need the `owner` permission (checked on Garage v1.1.0).
- **Deleting a project.** `project delete --purge-storage` empties the
  buckets but never deletes them or their keys. Delete the buckets and keys
  on Garage yourself once you no longer need them.

## 8. What seine relies on

- **Client.** seine talks to S3 through `boto3` (a dependency of the `seine`
  packages as `python3-boto3`), signing with SigV4 and using path-style
  addressing.
- **Write-once is not enforced.** Garage ignores `If-None-Match`, so a second
  upload of a key overwrites the first; see
  [Caching](caching.md#storage-providers-and-sidecars) for how seine copes.
- **Every pull is verified** against the SHA-256 stored with the object. This
  catches an object that was altered or truncated without its metadata; it
  does not stop someone who can write the bucket from replacing both, so give
  the key only to builders you trust.
- **Distributed builds** keep worktrees and artifacts in the same buckets (see
  [Shared storage layout](distributed-build.md#4-shared-storage-layout)).
  Worktree bundles exclude secrets by default, but every project member can
  read what was uploaded: see the
  [security model](distributed-build.md#security-model). Only the server holds
  keys, one pair per project and environment (see
  [Per-project keys](#per-project-keys)); workers get the keys of their job's
  bucket from it. A client downloads artifacts from Garage with pre-signed
  URLs, so port 3900 must be reachable from developers as well.

## 9. Where to go from here

- **TLS encryption**: In production, place a reverse proxy (such as
  Caddy or Nginx) in front of port 3900 to provide HTTPS and valid TLS
  certificates. A client refuses to download artifacts from a plain `http://`
  endpoint unless `--insecure` is given.
- **Multi-node clustering**: Garage can replicate data across multiple
  servers or data centers by adding nodes and updating the layout with
  `replication_factor = 2` or `3`.
- **Space management**: `seine-server` expires old worktrees, artifacts and
  cache objects itself when `retention:` is configured (see
  [Storage housekeeping](distributed-build.md#storage-housekeeping)).
- **Firewalling**: Restrict port 3900 to trusted build machines and CI
  runners, and keep RPC port 3901 strictly internal between Garage
  nodes.
