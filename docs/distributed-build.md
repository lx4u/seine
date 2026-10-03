# Distributed builds with seine

seine can offload builds from a developer laptop to dedicated worker machines
across your network. The distributed system packages your local worktree,
stages it to shared S3 storage, schedules jobs to native architecture workers
(`amd64`, `arm64`), and streams live build logs back to your terminal over
WebSocket.

This guide walks through setting up:
- One central **`seine-server`** API and scheduler daemon.
- Two **`seine-agent`** worker nodes (for example, an `amd64` server and an
  `arm64` single-board computer).
- Pointers to shared S3 storage ([Garage](storage-garage.md)) and signing
  ([OpenBao](vault-openbao.md)).

---

## Architecture overview

```
 [ Developer Laptop ] ◄───────────────────────────┐ (5. Direct download)
   seine build --remote https://server:8000       │
        │ (1. Upload worktree tar.zst)            │
        ▼                                         │
 [ seine-server:8000 ] ──── (2. Stage bundle) ───► [ S3 / Garage Storage ]
        │                                                  ▲
        │ (3. Capability-aware scheduling)                 │ (4. Pull worktree & cache)
   ┌────┴───────────────────────────┐                      │ (5. Push deliverables)
   ▼                                ▼             ┌────────┴────────────────┐
[ Worker 1 (amd64) ]        [ Worker 2 (arm64) ]  │ OpenBao Vault (Signing) │
  seine-agent                 seine-agent         └─────────────────────────┘
  (Podman + libguestfs)       (Podman + libguestfs)
```

1. **Client** archives the local spec directory and streams a compressed bundle
   (`tar.zst`) to `seine-server` authenticated with a personal access token.
2. **Server** hashes the bundle while it receives it and stages it in the
   project's S3 bucket as `worktrees/<project>/<digest>.tar.zst`, where the
   digest is the SHA-256 of the upload. Development builds use the `dev`
   bucket, `--release` builds the `prod` one.
3. **Scheduler** checks registered workers, evaluates architecture scores
   (native `1.0`, cross `0.7`, emulation `0.3`), and assigns jobs to the best
   available worker, waiting `native_grace` seconds for a better one.
4. **Worker Agent** claims the job, pulls the bundle from S3 and checks its
   SHA-256, then runs `seine build` as an unprivileged subprocess (rootless
   Podman and libguestfs) and streams its output live over WebSocket.
5. **Artifact Delivery**: When the build completes, the worker uploads the built
   disk images and companion metadata directly to S3 (`artifacts/<project>/<build-id>/`)
   and reports a manifest (name, size, SHA-256) of what it uploaded.
   `seine-server` stores the manifest and generates temporary pre-signed
   download URLs for the names in it. The developer's client fetches the
   deliverables directly from S3, without relaying large disk images through
   the server, and checks each one against the manifest.

---

## Prerequisites

Before starting the server and agents, configure your shared storage and
optional signing services:

1. **Shared S3 Storage (Garage):**
   Workers require an S3-compatible bucket for shared caching and worktree
   staging. Follow the [Running a Garage S3 cache](storage-garage.md) guide
   to deploy a lightweight Garage instance.
2. **Production Signing Vault (OpenBao):**
   If you produce cryptographically signed release images or UKIs, follow the
   [OpenBao signing integration](vault-openbao.md) guide to configure transit keys
   for Secure Boot, kernel modules, and package repositories.

---

## 1. Setting up `seine-server`

Run `seine-server` on a central machine reachable by both your developers and
your build workers. The system users, directories and systemd units of the
packages are described in [Shared worker setup](worker-setup.md).

### Installation

Install the server package on Debian:

```bash
apt install seine-server
```

Or run directly from the source repository:

```bash
export SEINE_ENROLLMENT_TOKEN=$(openssl rand -hex 32)
uv run python -m seine.distributed.server.cli run --db-path /var/lib/seine/seine.db
```

### Server settings

Settings are merged in this order, the last one winning: defaults, the config
file, environment variables, command-line flags. The config file is
`/etc/seine/server.yaml` (`--config` or `SEINE_SERVER_CONFIG` name another
one); its keys may also sit under a `server:` mapping, and an unknown key is
an error.

| Key | Environment variable | Flag | Default |
|-----|----------------------|------|---------|
| `host` | `SEINE_HOST` | `--host` | `127.0.0.1` |
| `port` | `SEINE_PORT` | `--port` | `8000` |
| `db_path` | `SEINE_DB_PATH` | `--db-path` | `seine.db` |
| `enrollment_token` | `SEINE_ENROLLMENT_TOKEN` | `--enrollment-token` | none, required |
| `tls_cert`, `tls_key` | `SEINE_TLS_CERT`, `SEINE_TLS_KEY` | `--tls-cert`, `--tls-key` | none |
| `max_upload_bytes` | `SEINE_MAX_UPLOAD_BYTES` | | 512 MiB |
| `stale_after` | `SEINE_STALE_AFTER` | `--stale-after` | 120 (seconds) |
| `reap_interval` | `SEINE_REAP_INTERVAL` | | 30 (seconds) |
| `native_grace` | `SEINE_NATIVE_GRACE` | `--native-grace` | 30 (seconds) |
| `job_lost_grace` | `SEINE_JOB_LOST_GRACE` | `--job-lost-grace` | 90 (seconds) |
| `secret_ttl` | `SEINE_SECRET_TTL` | | 21600 (seconds, 6 hours) |
| `new_user_project` | `SEINE_NEW_USER_PROJECT` | | `none` |
| `storage.endpoint` | `SEINE_S3_ENDPOINT` | `--s3-endpoint` | none |
| `storage.region` | `SEINE_S3_REGION` | `--s3-region` | `garage` |
| `storage.projects`, `storage.default` | | | none, file only |
| `retention` | | | none, file only |

- **Retention.** Optional; without it nothing is ever deleted. See
  [Storage housekeeping](#storage-housekeeping).
- **Enrollment token.** There is no default: the server refuses to start
  without one. Generate it with `openssl rand -hex 32`. Workers present it
  once to register and receive a worker token of their own; registering an
  existing worker id replaces that worker's token, so keep the enrollment
  token as secret as the tokens it hands out.
- **Bind address.** The server listens on `127.0.0.1` only. To serve other
  hosts set `host` (for example `0.0.0.0`) and enable TLS: without it the
  server still starts but warns that tokens travel in clear text.
- **TLS.** `tls_cert` and `tls_key` must be given together. Clients and
  agents trust the certificate through `--ca-cert` / `SEINE_CA_CERT` when it
  is not a public one.
- **Upload size.** The worktree bundle is sent as the raw request body
  (`Content-Type: application/octet-stream`) and streamed to a temporary file.
  Anything above `max_upload_bytes` is refused with HTTP 413, and a bundle the
  storage rejects with HTTP 502.
- **Stale workers.** Agents heartbeat while idle and while a job runs. Every
  `reap_interval` seconds the server marks the workers silent for
  `stale_after` seconds offline and puts their running jobs back in the
  queue. A job is attempted at most three times: when its worker goes silent
  for the third time the job is marked failed instead.
- **Lost jobs.** Every heartbeat lists the jobs the agent still holds. A
  job the server has assigned to a worker for at least `job_lost_grace`
  seconds that the worker does not list is lost (the build process died
  with a restart or a power cut): it goes back in the queue, counting as an
  attempt, and is marked failed with "job lost by its worker" on the third.
  Workers that send no list are not checked. The old worker can no longer
  report on a requeued job: its status update is refused with HTTP 403.
- **Native preference.** A queued job is not given to a worker while another
  online worker with a free slot scores higher for its architecture and
  meets its constraints, so a native worker beats a cross one even when the
  cross one polls first. Once the job has waited `native_grace` seconds, any
  worker that qualifies takes it; `0` turns the preference off.
- **Feed credentials.** The logins and passwords of a build's authenticated
  apt feeds are kept in the server's memory only, for `secret_ttl` seconds
  from the submission. Past that, or once the build has ended (completed,
  failed or cancelled), they are dropped and never handed out again, so a
  build that waits in the queue longer than `secret_ttl` fails on its
  authenticated feeds. They are never written to the database or the logs.
- **Projects for new users.** `new_user_project` says what a user created
  from now on gets, besides the account: `none` (nothing), `auto`, or the name
  of an existing project. See [Projects for new users](#projects-for-new-users).

### Storage credentials

Garage has no temporary credentials (STS), so isolation between projects and
between development and production rests on static keys, one pair per bucket,
that you create on Garage (see
[Per-project keys](storage-garage.md#6-per-project-keys)) and give to the server.
They live in the `storage:` section of `/etc/seine/server.yaml` and nowhere
else: the environment and the command line only carry the endpoint and the
region, and the keys are never stored in the database or written to the logs.
Keep the file `0600` (or `0640 root:seine`).

```yaml
storage:
  endpoint: https://garage.example.org:3900
  region: garage
  projects:
    my-project:
      dev:  {access_key: GKdev..., secret_key: <dev secret>}
      prod: {access_key: GKprod..., secret_key: <prod secret>}
  default:                       # projects without an entry above
    dev:  {access_key: GKshared..., secret_key: <shared dev secret>}
```

- A project uses its own entry, else `default`, else it has no credentials.
  The entry is not mixed with `default`: a project that lists only `dev` has
  no `prod` pair.
- `access_key` and `secret_key` come together or not at all, and an unknown
  name is an error. With keys configured the server needs `endpoint`.
- The `dev` pair stages worktrees and runs development builds; the `prod`
  pair stages worktrees and runs `--release` builds, which need the
  `releaser` or `admin` role.
- With no pair for the project and environment, nothing falls back to the
  server's own environment: staging a worktree answers HTTP 409, and a job
  that is claimed fails with the reason in its `error_message`, which the
  client prints.

### Storage housekeeping

Without a `retention:` section nothing is ever deleted: the feature is opt-in.
With one, the server deletes what has aged out and keeps the dev bucket of
each project below a size limit.

#### What is kept, and what may be deleted

Each project has a `dev` and a `prod` bucket (see
[Shared storage layout](#4-shared-storage-layout)). Each holds three prefixes:

| Prefix | Dev bucket | Prod bucket |
|--------|------------|-------------|
| `artifacts/` | deleted by age (`artifacts`) and, oldest first, when the bucket is over `high_water` | never deleted; above `high_water` the server only logs a warning |
| `worktrees/` | deleted by age (`worktrees`) | deleted by age |
| `cache/` | deleted by age (`cache`) | deleted by age |

Artifacts of a `--release` build are never deleted, and neither are those of
a build that has not finished.

#### Configuration

`retention` is a key of `server.yaml` only (no environment variable or flag).
Omitted keys take the values below.

```yaml
retention:
  interval: 3600        # seconds between sweeps
  dev:
    worktrees: 3d
    artifacts: 14d      # counted from the end of the build
    cache: 30d
    high_water: 80%     # start evicting artifacts above this ...
    low_water: 60%      # ... and stop below this
    min_age: 1h         # pressure eviction only: skip builds younger than this
  prod:
    worktrees: 14d
    artifacts: never
    cache: 90d
    high_water: 85%     # prod only warns, it is never evicted
```

- **Durations** are `<n>s`, `<n>m`, `<n>h` or `<n>d` (above zero), or `never`
  for no expiry. `min_age` cannot be `never`. `interval` is a number of
  seconds or a duration.
- **Thresholds.** `high_water` and `low_water` are either `<n>%` (above 0, up
  to 100) of the project quota, or a size `<n>M`, `<n>G` or `<n>T` in binary
  units (1G is 1024 MiB). `low_water` must be below `high_water`.
- **Quota.** A percentage is relative to the quota of the project, which you
  set with `seine admin project update NAME --quota-gb N` (see
  [Users, roles and tokens](#users-roles-and-tokens)). A project with a
  percentage and no quota gets no pressure eviction, and a warning is logged:
  seine does not guess a size.
- **Percentages or sizes, not both.** Mixing them in one environment is an
  error at start-up, because they cannot be compared. If you give a size for
  one of `high_water` and `low_water`, give a size for the other too: the
  defaults are percentages.
- Unknown keys are an error.

#### Pressure eviction

When the dev bucket of a project holds `high_water` or more, the server
deletes the artifacts of builds until it holds `low_water` or less. The gap
between the two stops it from running again after every build.

- Oldest build first, by the time it finished.
- Only builds that finished (completed, failed or cancelled), are not release
  builds, still hold artifacts and ended at least `min_age` ago. `min_age`
  applies to pressure eviction only (the age limit uses `artifacts`), and
  matters: a client downloads the artifacts right after the build, and an
  image larger than the free space would otherwise evict itself.
- The usage is the sum of the sizes of the objects in the bucket, which the
  server counts itself.
- `min_age` must be longer than the slowest client download: a build older
  than `min_age` can be evicted while its client is still downloading, and the
  client then reports the build as expired.
- Housekeeping logs one INFO line per evicted build to the server log (the
  journal).
- If nothing is left to evict and the bucket is still above `high_water`, an
  error is logged and the sweep stops. The `cache/` prefix is not touched by
  this path.

#### When it runs

- every `interval` seconds, for every project;
- after each build that finishes, for the project of that build only (one
  image can take a large part of the quota, so an hourly pass alone would
  overshoot);
- on demand, with `seine admin storage gc`.

One run at a time: a second request while a run is in progress gets HTTP 409.
Failures are logged and never stop the
server.

```bash
seine admin storage gc --dry-run
seine admin storage gc --project demo
```

The command is for system administrators and takes the same `--server`,
`--token`, `--ca-cert` (or `$SEINE_CA_CERT`) and `--insecure` as the other
`seine admin` commands. `--dry-run` deletes nothing
and reports what it would. It prints one line per project, one per build, and
one for the worktrees:

```text
demo: usage 41.2 GB -> 23.8 GB, evicted 2 builds (17.4 GB)
  build bld-1a2b3c4d  pressure  9.1 GB
  build bld-5e6f7a8b  ttl  8.3 GB
  worktrees: 3 expired (420.0 MB)
```

A project that could not be processed is shown as `NAME: skipped, REASON`
(for example a percentage without a quota, or no dev storage configured). It
prints `retention is not configured on this server` when there is no
`retention:` section, and exits non-zero if the objects of some build could
not be deleted. Over the API this is `POST /api/v1/storage/gc` with
`{"project": "demo", "dry_run": true}`; system administrators only. An unknown
project gives 404.

The warning for a prod bucket above `high_water` appears only in the server
log, not in the output of the command.

#### What users see

The build stays in the database. When its artifacts have been evicted,
`GET /api/v1/builds/{id}` still answers 200 and carries `artifacts_expired_at`
and `artifacts_expired_reason` (`ttl` for age, `pressure` for the size
limit). `download_urls` is empty but `artifacts` still lists what the build
produced (name, size, SHA-256). It is not an HTTP 410.
The client prints a message such as `artifacts of build bld-1a2b3c4d expired
(storage pressure); rebuild to get them again` and exits 1 (nothing is checked
with `--no-download`), and the TUI keeps the artifacts in its list, marked
`expired` in the download column.

#### Lifecycle rules on the buckets

Each non-dry sweep installs up to three lifecycle rules on the dev and prod
bucket of every project, so Garage also expires objects when the server is not
looking:

- `seine-abort-multipart` aborts incomplete multipart uploads after 1 day;
- `seine-worktrees-expiry` expires `worktrees/` after the `worktrees` age;
- `seine-cache-expiry` expires `cache/` after the `cache` age.

Ages are rounded up to whole days (at least 1); with `never` the matching rule
is removed. Other rules on the bucket are kept, and nothing is sent when the
bucket already has the right ones. A bucket whose rules cannot be installed
is logged and reported, and the rest of the sweep goes on. Garage evaluates
lifecycle rules about once a day, so an expiry is not visible at once, and the
key needs no more than read and write (see
[Quota backstop](storage-garage.md#7-quota-backstop)).

Lifecycle rules look at the age of the object only, not at the build table.
So the server adds two things:

- **Worktrees.** Submitting a build restarts the age of its worktree, so a
  digest that is still in use does not expire. The sweep also deletes the
  worktrees older than the `worktrees` age, which covers lifecycle rules that
  have not run yet, but never one that a queued or running build of the
  project uses. Known limits: the lifecycle rule does not know about queued
  builds, so a build that waits longer than the `worktrees` age (3 days by
  default for dev) may find its worktree gone; and uploading a worktree that is
  already staged does not refresh it, only submitting a build does.
- **Cache.** An expired cache object is simply rebuilt and pushed again on the
  next miss. Its age counts from the last write, not the last use, and the
  `.touch` files rewritten on every hit may outlive their entry as tiny
  orphans.

#### Deleting a project

`project delete NAME --purge-storage` empties the dev and prod buckets of a
project before deleting it (see
[Users, roles and tokens](#users-roles-and-tokens)). The buckets and their
keys stay on the storage server.

### Service configuration

When using systemd, put the settings in `/etc/seine/server.yaml`; the
environment file `/etc/default/seine-server` overrides it:

```yaml
enrollment_token: <the output of openssl rand -hex 32>
db_path: /var/lib/seine/seine.db
tls_cert: /etc/seine/tls/server.crt
tls_key: /etc/seine/tls/server.key
```

Enable and start the service:

```bash
systemctl daemon-reload
systemctl enable --now seine-server
systemctl status seine-server
```

### Users, roles and tokens

`seine-server admin` works directly on the server database, so run it on the
server host as the `seine` user. `--db-path` defaults to `$SEINE_DB_PATH`,
then `seine.db`.

```bash
admin="sudo -u seine seine-server admin --db-path /var/lib/seine/seine.db"

# 1. The first administrator and its token
$admin user create alice --is-admin
$admin token issue alice --days 30
# Secret (shown only once): 4b29c1...

# 2. A project backed by your Garage S3 bucket
$admin project create demo --dev-bucket seine-cache --prod-bucket seine-cache

# 3. A developer with a token of their own
$admin user create bob
$admin member add demo bob developer
$admin token issue bob
```

- A user must exist before it can get a token or join a project.
- The token kind is `pat` (personal access token), which is the default and
  the only one. `--days` sets an expiry; without it the token never expires.
- The secret is printed once. The database keeps only its SHA-256 hash, so a
  lost token cannot be recovered: issue another and revoke the old one with
  `token revoke <id>` (`token list` shows the ids).
- `user update <id> --no-active` disables a user and its tokens stop working.
  The last active administrator cannot be demoted or disabled.
- `project update <name> --quota-gb N` sets a project's storage quota in GB;
  `--no-quota` removes it. `project list` shows it. Over the API this is
  `PATCH /api/v1/projects/<name>` with `{"quota_gb": N}` or `null`.
- `project delete <name> [--purge-storage]` removes the project from the
  server. Its buckets and objects remain unless `--purge-storage` is given:
  that empties the dev and prod buckets (objects and incomplete multipart
  uploads) first, then deletes the project; it is irreversible, needs a system
  administrator and is refused while a build is in progress (409). A storage
  failure answers 502 and keeps the project, so the command can be retried.
  The buckets and keys themselves stay on the storage server: delete them
  there. Over the API this is `DELETE /api/v1/projects/<name>?purge_storage=true`.
- `storage gc [--project P] [--dry-run]` runs storage housekeeping now; see
  [Storage housekeeping](#storage-housekeeping).

#### Projects for new users

With `new_user_project` set, `user create` (the command above and
`POST /api/v1/users`) also gives a new user somewhere to build, so they can
submit a build without being added to a project first. Administrators never
get one, and neither does anybody when it is `none`.

- `auto`: the user gets a home project named `home-` and their user id
  (lowercase, anything but letters and digits turned into `-`, a `-2`, `-3`
  suffix if the name is taken). They are a `developer` in it and it becomes
  their default project. It is dev-only: it has no prod bucket, and release
  builds and `env=prod` uploads are refused with HTTP 400. Names starting with
  `home-` cannot be given to a project by hand.
- a project name: the user joins that existing project as a `developer` and it
  becomes their default project. Nothing is created, so its cache stays
  shared. If the project does not exist, no user is created and the error says
  so.

Creating the user touches no storage. The home project's dev bucket
(`seine-home-<name>-dev`) is created by the first worktree upload, with the
`storage.default` dev key, which therefore needs to be allowed to create
buckets (on Garage, a key permission). If it is not, the upload fails with
HTTP 502 and "could not create bucket". Deactivating the user leaves the
project and its bucket in place.

| Role | Can |
|------|-----|
| system administrator (`user create --is-admin`) | everything, in every project: create projects, manage users and tokens through the API |
| project `admin` | manage the members of the project, delete it, cancel any build of it, submit release builds |
| `releaser` | submit builds, including release builds (`--release`) |
| `developer` | submit builds, but not release builds |

Every member of a project, whatever the role, can upload a worktree to it,
follow its builds, see their status and download their artifacts. Anyone
else gets HTTP 403.

---

## 2. Setting up the build workers (`seine-agent`)

Install and start `seine-agent` on your worker machines. In this example, we set
up two workers:
- **Worker 1 (`amd64`):** A fast x86_64 desktop or build server.
- **Worker 2 (`arm64`):** A 64-bit ARM board (such as a Raspberry Pi 4/5 or
  Rockchip RK3399/RK3588).

### Requirements on worker nodes

Each worker must have:
- Rootless Podman installed.
- Access to `/dev/kvm` for libguestfs appliance acceleration (user in the `kvm`
  group).
- No S3 endpoint or keys: the server hands each job the access to its own
  bucket (see [Storage credentials](#storage-credentials)). Do not put S3
  keys on a worker.
- Sufficient scratch disk space (at least 20 GB recommended under a persistent
  filesystem, rather than memory-backed `/tmp`).

### Agent setup

The same steps apply on both machines. On each one:

```bash
apt install seine-agent
```

Edit `/etc/default/seine-agent`:

```bash
SEINE_SERVER_URL=https://192.168.1.111:8000
SEINE_ENROLLMENT_TOKEN=<the token configured on the server>
SEINE_CA_CERT=/etc/seine/ca.pem
```

Start the agent:

```bash
systemctl enable --now seine-agent
```

The worker id is `worker-<hostname>-<arch>` unless `--worker-id` is given
(the packaged unit does not pass it). The work directory is
`/var/lib/seine-agent`. The agent refuses a plain `http://` URL unless the
host is a loopback one or `--insecure` is given; use that for testing only.

### How the agent runs a build

- **Unprivileged.** The agent refuses to run as root unless `--allow-root` is
  given, in which case the builds run as root too. Do not use it in production.
- **Environment.** A build inherits an allowlist of the agent's environment:
  `PATH`, `HOME`, `LANG`, `LC_*`, `TZ`, `TERM`, `TMPDIR`, `XDG_*`,
  `CONTAINERS_*`, the proxy variables and the `SEINE_*` variables whose name
  holds no `TOKEN`, `PASSWORD`, `SECRET` or `KEY`. The agent's tokens never
  reach it. The agent holds no S3 keys and drops any `AWS_*`, `SEINE_S3_*`
  and `SEINE_CREDENTIALS_FILE` it inherits. A job submitted with `--s3-cache`
  gets the access to its own bucket from the server instead: the key pair in
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, the endpoint, bucket and
  region as `--s3-*` flags. The keys are never on the command line and are
  masked in the logs the agent streams.
- **Feed credentials.** For a job with authenticated feeds the agent masks
  every login and password in the logs it streams, then writes them as JSON
  to `<work-dir>/secrets/<build-id>/feeds.json` (mode `0600`, in a `0700`
  directory outside the job directory, so it is never part of the artifacts)
  and gives its path to the build in `SEINE_FEEDAUTH_FILE`. The values are
  neither on the command line nor in the environment. The file is deleted
  when the job ends whatever its outcome, and the agent removes any left by a
  crash when it starts. The build uses these credentials as they are: it does
  not read a keyring, settings file or environment, and never prompts. A feed
  with no entry fails the build.
- **Job directory.** Each job runs in `<work-dir>/jobs/<build-id>`, wiped when
  the job ends whatever its outcome, with `podman unshare` as a fallback for
  files owned by container users: it first unmounts the build's leftover
  overlay mounts, and the wipe is retried a few times. A cleanup that fails is
  logged and never fails the job. The agent also wipes the directories (and
  containers) a crashed agent left in `<work-dir>/jobs` when it starts.
- **Containers.** `seine` labels the containers it starts with
  `seine.build_id=<build-id>`. When a job ends, and in particular when it is
  cancelled, the agent removes the containers carrying its label. Containers
  started by the ansible apt action plugin are not labelled yet and are not
  removed.
- **Cancel and shutdown.** The server passes a cancel request to the agent in
  the reply to a heartbeat. The agent sends SIGTERM to the build's process
  group, SIGKILL if it is still running after a grace period, cleans up and
  reports the job as cancelled. On SIGTERM or SIGINT the agent does the same
  to its running job, which it then reports as failed.
- **Final results.** The agent writes the final status of a job (and its
  artifact list) to `<work-dir>/pending/<job-id>.json` before reporting it,
  and removes the file once the server answers. While the server is
  unreachable it retries with a growing delay, then keeps the file. It sends
  the files left over when it starts and on each heartbeat, and lists these
  jobs in its heartbeats so the server does not consider them lost.

### Verifying worker registration

Check that each agent registered:

```bash
journalctl -u seine-agent
```

Look for a line starting with `[agent] Registered`.

---

## 3. Running distributed builds

Developers can now submit builds from any machine with network access to
`seine-server`.

### Submitting an `amd64` PC disk image build

From your workstation:

```bash
export SEINE_TOKEN=<alice-token>
seine build --remote https://192.168.1.111:8000 \
    --ca-cert /etc/seine/ca.pem \
    --project demo \
    examples/pc-image/main.yaml
```

The client will:
1. Take the target architecture from the specification
   (`distribution.architecture`), or the host's when it sets none.
   `--target-arch` overrides it.
2. Package the local directory into a compressed bundle and upload it to the
   server, which stages it in S3.
3. Submit the build and follow it, printing the output of the worker as it
   arrives over the WebSocket stream.
4. Download the deliverables of a completed build (see below).

### Feed credentials

A specification whose feeds carry `auth:` (see the
[specification](specification.md)) is built remotely with the credentials of
your own machine. Before uploading anything the client resolves them exactly
as `seine build` does locally, from the `keyring:`, `settings:`, `env:` and
`vault:` chains, asking in the terminal for what it does not find, and checks
each against the feed's Release file. A credential the feed rejects stops the
build before anything is sent.

The credentials travel once, in the build request, over TLS. The client
refuses to send them to a plain `http://` server unless it is on this
machine, `--insecure` or not. The server keeps them in memory for
`secret_ttl` and hands them to the workers running the build's jobs. A
specification without authenticated feeds sends nothing.

Every job of a build receives all the feeds of that build, including the
package builds that fan out of a `packages:` section, whether or not they use
them. Handing each job only the feeds it needs is not done yet.

### Client options

| Option | Meaning |
|--------|---------|
| `--remote URL` | The `seine-server` to build on. Only `https://` is accepted, except for a loopback host or with `--insecure`. |
| `--project NAME` | The project to build in (default `$SEINE_PROJECT`, else your default project on the server, see [Choosing a project](#choosing-a-project)). |
| `--token TOKEN` | Personal access token (default `$SEINE_TOKEN`). Without one, the client uses the token an earlier run saved (the keyring, else `~/.config/seine/credentials.json`), or asks for one in the terminal and offers to save it. It stops before uploading anything when it gets none. |
| `--ca-cert PATH` | CA bundle verifying the server certificate, and the storage endpoint when downloading (default `$SEINE_CA_CERT`). |
| `--insecure` | Allow plain `http://` to a server that is not on this machine. Testing only. |
| `--release` | A release build: needs the `releaser` or `admin` role. |
| `--dest-dir PATH`, `--no-download` | Where to put the artifacts, or skip downloading them. |

A token is saved only once the server has accepted it, in the keyring when
one is available and otherwise in `credentials.json`. A saved token the
server rejects (revoked, expired) is asked for again; after 3 rejected tokens
the client gives up with exit status 2.

`--packages-only`, `--s3-cache`, `--require-native` and `--min-arch-score` are
also sent to the server. It accepts no other build option, and rejects any
whose name looks like a secret (`token`, `secret`, `password`, `key`,
`credential`).

### Choosing a project

Without `--project` or `$SEINE_PROJECT`, the client asks the server who you
are (`GET /api/v1/me`) and takes, in this order:

1. your default project, if you set one;
2. the only project you belong to (not for administrators);
3. else, on a terminal, it lists your projects with your role in each, asks
   which one, and offers to keep the answer as your default;
4. else it stops before packing anything and asks you to pass `--project`.

An administrator can build in any project, so the list is every project and
they are always asked unless they have a default. Someone who belongs to no
project cannot build.

The default is part of your account on the server, shared by the CLI and the
TUI. `PATCH /api/v1/me` with `{"default_project": "NAME"}` sets it and
`null` clears it; it must be a project you belong to (an administrator may
pick any). It is dropped when you leave that project or the project is
deleted, and kept when your account is deactivated and back. `GET /api/v1/me`
returns it as `default_project`.

### Exit status and cancelling

| Status | Meaning |
|--------|---------|
| 0 | The build completed and every artifact was downloaded (or `--no-download`) |
| 1 | The build did not complete, or at least one artifact failed to download |
| 2 | Client, authentication or protocol error (no token, bad certificate, HTTP 4xx...) |
| 130 | Interrupted with Ctrl+C |

Ctrl+C asks the server to cancel the build and waits for it to stop; a second
Ctrl+C leaves at once. Queued jobs are cancelled immediately. A running job is
flagged, and its agent stops it when it next hears from the server, within one
heartbeat. The build becomes `cancelled` when no job is left. Only the user
who submitted a build, a project admin or a system administrator may cancel
it, and cancelling a finished build is refused with HTTP 409.

### Live logs

The client reads the log from `/api/v1/builds/<build-id>/stream` on the server,
over `wss://` (`ws://` for a plain `http://` server).
It is a convenience: when the stream cannot be opened or is closed, the client
says why and keeps following the build by polling its status.

For those writing a client: the token is never put in the URL. The first
message, sent within 5 seconds of connecting, must be `{"auth": "<token>"}`.
Otherwise the server closes the connection with one of these codes:

| Close code | Meaning |
|------------|---------|
| 4401 | Missing, invalid or expired token, disabled user, or no first message |
| 4403 | Not a member of the build's project, or a viewer sent a message |
| 4404 | No such build |

A viewer then receives the history of the build followed by live messages of
the form `{"build_id", "source", "text", "timestamp"}`. The history is
bounded, so a long build may have lost its oldest lines.

The stream also carries structured events, told apart by a `type` field
instead of `text`, each with `build_id` and `timestamp`:

| `type` | Fields | Meaning |
|--------|--------|---------|
| `task_plan` | `tasks`: list of `{name, needs, cached?}` | The planned tasks, sent once before the build starts |
| `task_started` | `task` | A task began; several may run at once |
| `task_finished` | `task`, `failed` | A task ended |
| `sampled` | `sample`: `{load, cpu}` | Machine load sample |
| `say` | `text` | A status message, such as an interrupt notice |

The plan is kept apart from the log history, so a viewer that joins late or
reconnects still gets it first. A client that does not know these events can
ignore every message without `text`. Upgrade the server before the agents:
an older server closes the stream of an agent that sends events.

### Submitting an `arm64` Raspberry Pi image build

Build an ARM64 image using your remote ARM64 worker:

```bash
seine build --remote https://192.168.1.111:8000 \
    --ca-cert /etc/seine/ca.pem \
    --project demo \
    examples/rpi4-image/main.yaml
```

The scheduler matches the `arm64` target against the `arm64` worker and routes
the job directly to the ARM64 board without slow emulation.

### Scheduling constraints

You can fine-tune worker selection with command-line flags:

- `--require-native`: Require native CPU architecture. The job remains queued
  until a native worker becomes available.
- `--min-arch-score <score>`: Require a worker whose architecture score for the
  target is at least `score` (see [worker setup](worker-setup.md)).

Without these flags the scheduler still prefers the best-scoring worker: a
job waits up to the server's `native_grace` (30 seconds by default) for an idle
worker that scores higher, such as a native one, before a cross or emulating
worker may take it.

### Automatic artifact download

When a remote build completes successfully, the client asks `seine-server` for
the build's status, which carries the artifact manifest and a temporary
download URL for each artifact, and fetches them directly from S3:

```text
[client] Build bld-1a2b3c4d finished with status: COMPLETED
[client] Downloading pc-image.img... done (<size>, sha256 verified)
[client] Downloading pc-image.img.digest... done (<size>, sha256 verified)
[client] Downloaded 2 of 2 artifact(s) to ./deploy/trixie
```

Deliverables are saved to `./deploy/<release>/` by default, matching local build
behavior.

Each artifact is checked against the manifest, which the worker reported and
the server stored: name, size and SHA-256.

- The download goes to `<name>.part` and is renamed to `<name>` only when its
  size and SHA-256 both match. A failed transfer leaves nothing behind.
- A file that does not match is kept as `<name>.corrupt` and counts as a
  failure, so the exit status is 1.
- An artifact the server reported without a checksum is not downloaded.
- Names that are not plain file names are refused, and redirects from the
  storage endpoint are not followed.
- The download URLs follow the `https://` rule of `--remote`: a storage
  endpoint on plain `http://` needs `--insecure`, and one with a private
  certificate needs `--ca-cert`.
- Artifacts that the server evicted (see
  [Storage housekeeping](#storage-housekeeping)) have no download
  URLs: `GET /api/v1/builds/{id}` returns `artifacts_expired_at` and
  `artifacts_expired_reason` (`ttl` or `pressure`), and the client says so and
  exits 1.

#### Controlling artifact downloads

- **Custom target directory**: Use `--dest-dir <path>` to store downloaded files
  in a different directory:
  ```bash
  seine build --remote https://192.168.1.111:8000 \
      --dest-dir /var/images/releases \
      examples/pc-image/main.yaml
  ```
- **Skip download**: In CI/CD pipelines or when disk images are consumed directly
  from S3, pass `--no-download` to skip local downloading:
  ```bash
  seine build --remote https://192.168.1.111:8000 \
      --no-download \
      examples/pc-image/main.yaml
  ```

---

## 4. Shared storage layout

All shared build assets and outputs reside in your S3 bucket (for example,
`s3://seine-cache`), at the root of the bucket:

```text
s3://seine-cache/
├── worktrees/<project>/<digest>.tar.zst   # Staged source worktrees
├── cache/<kind>/<key>.tar.zst             # Shared cache: bootstraps, chroots,
│                                          # packages, rootfs (see caching.md)
└── artifacts/<project>/<build-id>/        # Harvested build deliverables
    ├── pc-image.img                       # Bootable full disk image
    ├── pc-image.img.digest                # SHA-256 integrity digest
    ├── pc-image.img.boot-signers.json     # UEFI / kernel module signing metadata
    └── pc-image.img.sbom.json             # Software bill of materials
```

After a successful build the worker uploads the files of the build's `deploy`
directory that match the deliverable patterns (disk images, root file-system
tarballs, `.digest`, `.recipe`, `.boot-signers*` and `.sbom*` files) to
`artifacts/<project>/<build-id>/`.

The server rejects a status report whose manifest names a key outside that
prefix, a name that is not a plain file name, or an entry without a 64-digit
SHA-256 and a size. It issues pre-signed URLs (valid for 1 hour) only for the
names in the manifest, and only to members of the project, so developers
download images from S3 without proxying them through the API server.

---

## Security model

- **Credentials.** A personal access token acts as its user, within the
  projects the user belongs to, with the role it has there (see
  [Users, roles and tokens](#users-roles-and-tokens)). Workers hold a worker
  token, which only works on the jobs assigned to them and on the log streams
  of the builds they hold. The enrollment token only registers workers.
  Tokens are stored hashed.
- **Transport.** Serve `seine-server` over TLS whenever it is reachable from a
  network. Clients and agents refuse plain `http://` to a non-loopback host
  unless told otherwise with `--insecure`.
- **Worktrees.** The bundle is an archive that is identical for the same tree
  with the same file mtimes; mtimes are kept because seine derives its
  timestamps from them. It leaves out `.git`, `__pycache__`, `.venv`,
  `/build/`, `*.pyc`, `*.db`, `*.db-shm`, `*.db-wal`, `.env`, `.env.*`,
  `*.key`, `id_rsa*`, `id_ed25519*`, `/deploy/` and `/home/`, plus whatever
  `.gitignore` and `.seineignore` say, and the client warns about files
  whose name contains `secret`, `token` or `credential` or ends in `.pem`. Every member of the project can read what is uploaded to its
  dev bucket, so keep secrets out of the tree.
- **Release staging.** A `--release` build runs with the `prod` key, so the
  client uploads its worktree with `?env=prod` and the server stages it in
  the `prod` bucket; this needs the `releaser` or `admin` role (HTTP 403
  otherwise) and an unknown `env` is refused with HTTP 400. Submitting a
  build whose worktree is not staged in the bucket it will run against
  answers HTTP 400.
- **Feed credentials.** They are resolved on the developer's machine and sent
  only over TLS (the client refuses plain `http://` except to this machine).
  The server holds them in memory for `secret_ttl`, drops them when the build
  ends, and never stores or logs them, nor shows them in a response. The
  worker masks them in the streamed logs and gives them to the build through a
  private file that is deleted when the job ends. A worker that runs a job
  can read that job's feed credentials, and every job of a build gets all of
  its feeds.
- **Builds.** Builds run as the unprivileged agent user and only see the
  environment described in
  [How the agent runs a build](#how-the-agent-runs-a-build).
- **Storage keys.** Workers hold no S3 keys. The server hands each job, in
  the job assignment sent over TLS to the worker that owns it, the key pair of
  its bucket only: the project's `dev` pair for a development build and the
  `prod` pair for a `--release` build, never both. The pair is not stored and
  not logged. A build submitted with `--s3-cache` can use its own project's
  key while it runs, so whoever may submit builds to a project can reach that
  project's bucket for the duration of the build, and no other.
- **Not protected yet.** A signing proxy for production keys is planned;
  until then keep production signing keys off the workers.

---

## Related documents

- [Running a Garage S3 cache](storage-garage.md): Shared S3 storage setup.
- [OpenBao signing integration](vault-openbao.md): Cryptographic signing keys.
- [Shared worker setup reference](worker-setup.md): Detailed systemd unit details.
- [Caching in seine](caching.md): Content-addressed caching architecture.
