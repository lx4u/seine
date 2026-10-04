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
- **No presigned URLs.** `generate_download_url` returns the direct
  repo URL; keep port 8081 reachable for developers or proxy downloads
  through the server.
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
Handing Artifactory credentials to remote build workers (the
scheduler/agent path) is not wired yet: distributed builds still stage
worktrees and artifacts through S3.
