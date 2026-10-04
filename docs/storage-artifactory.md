# Running a JFrog Artifactory cache for seine

seine can share build caches through JFrog Artifactory as well as S3,
via `--storage-backend=artifactory`, `--artifactory-endpoint`,
`--artifactory-repo`, or `storage.artifactory` in a spec. This page
shows how to stand up the free Community Edition for C/C++ (generic
repositories included) with Docker.

**Security disclaimer.** Same deal as the Garage page: this gets a cache
running and reachable, nothing more. Harden exposure, tokens, TLS and
backups yourself before trusting it in production.

## 1. Start Artifactory CE

Pull and run the Community Edition with its embedded database:

```
docker volume create artifactory-data
docker run --name artifactory -d --restart unless-stopped \
  -p 8081:8081 -p 8082:8082 \
  -v artifactory-data:/var/opt/jfrog/artifactory \
  -e JF_SHARED_DATABASE_TYPE=derby \
  -e JF_SHARED_DATABASE_ALLOWNONPOSTGRESQL=true \
  -e JF_JFCONNECT_ENABLED=false \
  releases-docker.jfrog.io/jfrog/artifactory-cpp-ce:latest
```

First boot takes a few minutes. The router is ready when this answers 200:

```
curl -o /dev/null -w '%{http_code}\n' http://localhost:8082/router/api/v1/system/health
```

Open `http://localhost:8082`, log in as `admin` / `password` (you are
asked to change it, or change it with `POST
/artifactory/api/security/users/authorization/changePassword`, whose
fields are `userName`, `oldPassword`, `newPassword1` and
`newPassword2`).

Create one **generic local repository** per environment, e.g.
`seine-my-project-dev` and `seine-my-project-prod`, in the UI, or with
the configuration patch, the one repository call the free edition
accepts (`PUT /api/repositories` is Pro-only):

```
curl -u admin:<password> -H 'Content-Type: application/yaml' \
  -X PATCH http://localhost:8082/artifactory/api/system/configuration \
  --data-binary $'localRepositories:\n  seine-my-project-dev:\n    type: generic\n    repoLayout: simple-default\n'
```

A repository name cannot end in `-cache` (Artifactory keeps that suffix
for remote repositories), so seine's default repo is `seine-shared`.
seine never creates repos itself: a missing repo is a clear error
naming it.

## 2. Make a token

Make an identity token for seine (`POST /artifactory/api/security/token`
with `username=admin` and `scope=applied-permissions/user`; the free
edition has no `applied-permissions/admin` scope). An administrator's
token reaches every repo, so for anything shared give each environment
its own identity instead: a user, and a permission on that one repo
granting Read, Annotate, Deploy/Cache and Delete/Overwrite (the actions
build up on one another, so Delete/Overwrite brings the others; seine
needs it to overwrite aliases), then a token minted by that user with
the same call and `username=<that user>`. The free edition's web
interface (Administration, User Management) manages users and
permissions; the REST calls for them are Pro-only. Workers only ever
see the token the server hands their job. A token kept in a file:

```json
{
  "artifactory-endpoint": "http://<your-host>:8081",
  "artifactory-repo": "seine-shared",
  "artifactory-token": "<token>"
}
```

`chmod 600` the file. Environment variables work too:
`SEINE_ARTIFACTORY_TOKEN`, or `SEINE_ARTIFACTORY_USER` with
`SEINE_ARTIFACTORY_PASSWORD` for basic auth.

## 3. Point a build at it

Command line:

```
seine build \
  --shared-cache \
  --storage-backend=artifactory \
  --artifactory-endpoint=http://<your-host>:8081 \
  --artifactory-repo=seine-shared \
  your-spec.yaml
```

Or in the spec (credentials still resolve as above):

```yaml
storage:
  artifactory:
    endpoint: http://<your-host>:8081
    repo: seine-shared
```

`seine doctor` reports the repo as reachable or tells you why not
(bad token, missing repo, unreachable host).

## 4. What seine relies on

- **Client.** `seine/storage/artifactory/client.py` talks plain REST
  over `requests` (already a dependency): deploy with
  `X-Checksum-Sha256`, `HEAD` for existence, streaming `GET`, single
  `DELETE`, and AQL for listings.
- **Every pull is verified** against the server-recorded SHA-256, same
  as S3. A writer holding the token can replace both object and
  checksum, so hand the token only to builders you trust.
- **No conditional writes.** An empty-body `PUT` truncates, and two
  uploads race last-writer-wins. Digest-addressed keys are still safe
  (same content, same key); seine pre-checks them with `HEAD`.
- **No custom properties on CE.** Recipe and touch sidecars are plain
  small files next to the object, not metadata.
- **No server-side expiry.** Housekeeping deletes expired objects
  explicitly; there are no lifecycle rules to install. Worktree age is
  upload time, and digests of unfinished builds are spared.
- **No presigned URLs.** A plain repo URL needs credentials, which a
  client of a remote build does not have. See "Downloads" below for how
  the server gets artifacts to it.
- **Listings are one AQL query** (CE supports neither sort nor
  offset), so sweeps over repos with tens of thousands of files should
  narrow the prefix instead.

## 5. Server use

In `/etc/seine/server.yaml`:

```yaml
storage:
  type: artifactory
  artifactory_endpoint: https://artifactory.example.org:8081
  artifactory_projects:
    my-project:
      dev:  {token: <dev token>}
      prod: {token: <prod token>}
```

The repo name is the project's bucket name (`seine-my-project-dev`
unless renamed in `project create`). Retention sweeps, usage accounting
and artifact eviction work as with S3, minus lifecycle rules.
A claimed job carries one `storage` block (`type: s3` or
`type: artifactory`) holding only the credentials of its own bucket or
repo. The worker pulls the worktree and uploads artifacts through it,
and hands it to the child `seine build` through `SEINE_ARTIFACTORY_*`
(or `AWS_*`) plus the matching `--storage-backend` flags; ambient
storage variables of the agent are never inherited.

### Builds that run as their user

By default a worker gets the server's own token for the project and
environment. Unless that token belongs to an identity limited to the
repo (see "Make a token"), it reaches every repo on the instance, so a
build, whose specification and playbooks run arbitrary code, could read
or change other projects' repos. A user can bring their own token
instead, and the build then runs with only what that user may do in
Artifactory:

```
export SEINE_ARTIFACTORY_TOKEN=<your token>     # or SEINE_ARTIFACTORY_USER + SEINE_ARTIFACTORY_PASSWORD
seine build --remote https://seine.example.org ...
```

The client looks the credential up where it looks up any other
(`SEINE_ARTIFACTORY_*`, the keyring, `~/.config/seine/credentials.json`),
never prompts for it, and sends it, with the build and over https only,
to a server that reports it runs Artifactory. The server checks that the
credential can read and write the project's repo (a small probe object
is deployed and removed) and refuses the build with a 403 otherwise,
instead of letting it fail after hours. The worker then pulls the
worktree, runs the build and uploads the artifacts with that credential,
and the server's own never leaves it. The secret is kept in memory only,
for `secret_ttl`, like feed credentials, and the user's token must stay
valid for as long as the build runs, since uploads happen at its end.

`storage.artifactory_job_tokens` sets whether a build may or must do
this:

| Value | Without a token |
| --- | --- |
| `optional` (default) | the build runs on the server's credential, as an S3 build runs on the server's key |
| `required` | the build is refused, saying how to bring one; no storage credential of the server ever reaches a worker |

The server still needs its own credential for what it does itself:
staging worktrees, retention sweeps and purges. Give it one that is
limited to the repos it manages.

Seine project membership no longer decides who may write a repo for a
build that brings its own token: Artifactory does, so keep its
permissions in step with the project's members.

### Downloads

An S3 client downloads straight from storage with a presigned URL that
authorises that one object. Artifactory has no equivalent: a link needs
a credential, and even one limited to a repo reads every object in it.
A token of the `readers` group read other projects' and the prod repos
in testing, and anonymous access grants nothing until a permission
gives it something. `storage.artifactory_downloads`
chooses how clients get an artifact:

| Value | What happens | Exposes |
| --- | --- | --- |
| `proxy` (default) | The server streams the artifact itself (`GET /api/v1/builds/{id}/artifacts/{name}`), after the same project-membership check as the artifact list. The client sends only its own seine token to the server. | Nothing: no Artifactory credential leaves the server. Artifact traffic flows through it. |
| `byot` | A client that holds its own Artifactory credential (see "Builds that run as their user") gets the plain repo URL and downloads with that credential, so Artifactory's own permissions decide, and the traffic does not cross the server. Any other client is served through the server, as in `proxy`. | What each user's Artifactory permissions allow; seine project membership does not decide. |
| `direct` | The client gets the plain repo URL. | The repo must allow anonymous read, so anyone who can reach Artifactory can read what the repo holds. For public artifacts only. |

```yaml
storage:
  type: artifactory
  artifactory_downloads: proxy   # proxy | byot | direct
```

With `byot` the client announces that it holds a credential (an
`X-Seine-Own-Credential` header on its requests to the server), which
is how the server picks the plain URL for it. The credential is sent to
the storage endpoint the server reported in the user's profile and to no
other origin, over https only (or to a loopback host), never over plain
http even with `--insecure`, and redirects are not followed with it.
Both the CLI and the TUI do this.

The client verifies the storage endpoint with the same CA bundle it
uses for the seine server (`--ca-cert` or `SEINE_CA_CERT`). When
Artifactory has its own private certificate, put both certificates in
that one file (`cat seine-ca.crt artifactory.crt > bundle.pem`).

A client checks the SHA-256 and size of every download against the
manifest in every mode.
