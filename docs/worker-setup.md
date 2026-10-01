# Shared Worker Infrastructure Setup

This guide explains how to install, configure, and operate the distributed
`seine-server` daemon and `seine-agent` worker nodes.

## Architecture Overview

The distributed build backend consists of two main services:

- **`seine-server`**: Central API server and scheduler. Manages projects,
  role-based access control (RBAC), access tokens, worktree staging in S3,
  and architecture-aware build job queues.
- **`seine-agent`**: Daemon running on build worker machines. Discovers host
  hardware capabilities, enrolls with `seine-server`, claims matching jobs,
  and streams live stdout/stderr logs over WebSocket.

## Users, directories and permissions

The packages create two different system users, so that a build cannot read
the server's state even when both run on one host:

| Package | User | State directory (0750) | Configuration (0640) |
|---------|------|------------------------|----------------------|
| `seine-server` | `seine` | `/var/lib/seine` | `/etc/seine/server.yaml`, `/etc/default/seine-server` (`root:seine`) |
| `seine-agent` | `seine-agent` | `/var/lib/seine-agent` | `/etc/default/seine-agent` (`root:seine-agent`) |

Both users have `/usr/sbin/nologin` as shell and their state directory as home.
The state directories are created by the systemd units (`StateDirectory=`).

## 1. Setting Up `seine-server`

### Installation

Install the server package on your central host:

```bash
apt install seine-server
```

### Configuration

The server reads `/etc/seine/server.yaml`; the environment file
`/etc/default/seine-server` (`SEINE_*` variables) overrides it. Both are
readable by root and the `seine` user only. All the settings are listed in
[Distributed builds](distributed-build.md#server-settings).

There is **no default enrollment token**. Until you set one the service
refuses to start with a "no enrollment token" error. Generate a token and put
it in `/etc/seine/server.yaml`:

```bash
openssl rand -hex 32
```

```yaml
enrollment_token: <the generated token>
db_path: /var/lib/seine/seine.db
```

The S3 endpoint and the per-project keys the server uses and hands to jobs
go in the same file, in a `storage:` section (see
[Storage credentials](distributed-build.md#storage-credentials)); keep the
file readable by `root` and `seine` only.

The server binds `127.0.0.1:8000` by default. To serve other hosts, set
`SEINE_HOST` (for example `0.0.0.0`) and enable TLS, otherwise tokens and logs
cross the network in clear text:

```yaml
tls_cert: /etc/seine/tls/server.crt
tls_key: /etc/seine/tls/server.key
```

The key must be readable by the `seine` user (for example `0640 root:seine`).
The unit drops all capabilities, so a port below 1024 needs
`CAP_NET_BIND_SERVICE` added to `CapabilityBoundingSet=` in a drop-in.

The unit runs with `NoNewPrivileges`, `ProtectSystem=strict` and a private
`/tmp`; it can only write to `/var/lib/seine`.

### Starting the Service

Enable and start the systemd unit:

```bash
systemctl daemon-reload
systemctl enable --now seine-server
systemctl status seine-server
```

### Creating Users, Projects and Tokens

Run the administrative CLI as the `seine` user, against the server database.
Create the first administrator and issue its token; the secret is shown
once and cannot be recovered:

```bash
sudo -u seine seine-server admin --db-path /var/lib/seine/seine.db user create alice --is-admin
sudo -u seine seine-server admin --db-path /var/lib/seine/seine.db token issue alice
```

Then create a project and add members:

```bash
# Create a project with automatic S3 bucket provisioning
sudo -u seine seine-server admin --db-path /var/lib/seine/seine.db project create my-project --provision-buckets

# Add alice as a project member with developer role
sudo -u seine seine-server admin --db-path /var/lib/seine/seine.db member add my-project alice developer
```

## 2. Setting Up `seine-agent`

### Installation

Install `seine-agent` on your worker host (e.g. an `arm64` or `amd64` machine):

```bash
apt install seine-agent
```

Podman and the virtualization packages come in as dependencies of the `seine`
package. The agent package adds `uidmap`, and its postinst gives the
`seine-agent` user its own subuid/subgid range (`1000000-1065535`), which
rootless podman needs. It also adds the user to the `kvm` group, as libguestfs
needs `/dev/kvm`.

The agent refuses to run as root (`--allow-root` overrides this and makes the
builds run as root as well; it is not meant for production). Builds run in
rootless podman under the `seine-agent` user, with an allowlisted environment:
the agent's tokens are not passed to them, and the agent holds no S3 keys:
a job that uses the S3 cache gets the keys of its own bucket from the server
(see [How the agent runs a build](distributed-build.md#how-the-agent-runs-a-build)).

### Configuration

Configure the agent in `/etc/default/seine-agent` (readable by root and the
`seine-agent` user only):

```bash
SEINE_SERVER_URL=https://<server>:8000
SEINE_ENROLLMENT_TOKEN=<the token configured on the server>
SEINE_CA_CERT=/etc/seine/ca.pem
```

The agent needs no S3 endpoint or keys. As on the server there is no default
token: the agent does not start until `SEINE_ENROLLMENT_TOKEN` is set. `SEINE_CA_CERT` (or `--ca-cert`) is the CA
bundle used to verify the server certificate when it is not a public one.
Plain `http://` to a non-loopback server is refused unless `--insecure` is
given; use that for testing only.

The work directory is `/var/lib/seine-agent`. Keep it on a real disk, not on
tmpfs: it holds the job trees and podman's image storage. Each job directory
is wiped when the job ends.

### Why the agent unit is less locked down

`seine-agent.service` deliberately has no `NoNewPrivileges=yes`,
`ProtectSystem=strict` or `RestrictSUIDSGID=yes`. Rootless podman runs the
setuid `newuidmap`/`newgidmap` helpers and writes to its own storage, and
those protections would break it. The unit keeps what is compatible: private
`/tmp`, no access to `/home`, read-only kernel tunables and no module loading.
Rootless podman gets `XDG_RUNTIME_DIR=/run/seine-agent` from
`RuntimeDirectory=`, as the service has no login session.

### Starting the Agent

Enable and start the worker daemon:

```bash
systemctl daemon-reload
systemctl enable --now seine-agent
systemctl status seine-agent
```

Stopping the agent cancels the running job and cleans up first; systemd waits
up to 60 seconds for that.

Inspect worker logs to confirm successful registration:

```bash
journalctl -u seine-agent -f
```

## 3. Capability Discovery & Architecture Scoring

Upon startup, `seine-agent` automatically inspects:
- Native CPU architecture (`uname -m`).
- Emulation capabilities registered in `/proc/sys/fs/binfmt_misc`.
- Available cross-compiler toolchains.
- Available disk space in the work directory.

The scheduler calculates architecture suitability scores:
- **1.0**: Native execution (target architecture matches worker native architecture).
- **0.7**: Cross-compilation (worker has native cross-compiler installed).
- **0.3**: Emulation via QEMU user-static (`binfmt_misc`).

Build requests can constrain worker selection via `--require-native` or
`--min-arch-score`.

## 4. Submitting Remote Builds

From any developer machine with network access to `seine-server`:

```bash
export SEINE_TOKEN="<your-pat-token>"

# Submit build to remote server and stream live logs
seine build --remote https://<server>:8000 --project my-project spec.yaml
```

The client will pack the local worktree into a zstd bundle, upload it to the
server (which stages it in S3), submit the job to the scheduler, stream logs
until completion and download the artifacts. Options, exit status and
cancelling are described in [Distributed builds](distributed-build.md#3-running-distributed-builds).
