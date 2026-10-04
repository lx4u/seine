# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import contextlib
import hashlib
import os

import requests

from seine import vault


class ArtifactoryError(Exception):
    """Raised when an Artifactory API operation fails."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class ArtifactoryNotFoundError(ArtifactoryError):
    """Raised when a repository or artifact does not exist."""


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactoryClient:
    """Thin wrapper over the Artifactory REST API, with the verbs seine uses.

    Only the data plane is used: deploy, resolve, delete and AQL search.
    Repo creation, properties and folder listings need a Pro license, so
    seine treats them as admin-side and never calls them here.
    """

    def __init__(self, endpoint, repo, user=None, password=None, token=None,
                 prefix="artifactory", timeout=30):
        self.endpoint = endpoint.rstrip("/")
        self.repo = repo
        self.prefix = prefix.strip("/")
        self.timeout = timeout
        self._session = requests.Session()
        if token:
            vault.record_secret(token)
            self._session.headers["Authorization"] = f"Bearer {token}"
        elif user is not None:
            vault.record_secret(password)
            self._session.auth = (user, password)

    def _base(self, *parts):
        return "/".join([self.endpoint, self.prefix] + [p.strip("/") for p in parts])

    def _call(self, what, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as e:
            raise ArtifactoryError(f"{what} failed: {e}") from e
        if resp.status_code == 404:
            raise ArtifactoryNotFoundError(f"{what} failed: 404 not found",
                                          status_code=404)
        if resp.status_code >= 400:
            detail = ""
            with contextlib.suppress(Exception):
                detail = (resp.json().get("errors") or [{}])[0].get("message", "")
            msg = f"{what} failed: {resp.status_code} {detail}".strip()
            raise ArtifactoryError(msg, status_code=resp.status_code)
        return resp

    def _item_url(self, repo, path):
        return self._base(repo, path)

    def head_object(self, repo, path):
        """Return {"sha256", "size"}, None if the artifact is missing."""
        try:
            resp = self._call(f"HEAD {repo}/{path}", "HEAD", self._item_url(repo, path))
        except ArtifactoryNotFoundError:
            return None
        return {
            "sha256": (resp.headers.get("X-Checksum-Sha256") or "").lower() or None,
            "size": int(resp.headers.get("Content-Length", 0)),
        }

    def exists(self, repo, path):
        return self.head_object(repo, path) is not None

    def get_object(self, repo, path):
        """Fetch full artifact content as bytes."""
        return self._call(f"GET {repo}/{path}", "GET", self._item_url(repo, path)).content

    def download_file(self, repo, path, target_path):
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        resp = self._call(f"download {repo}/{path}", "GET",
                          self._item_url(repo, path), stream=True)
        with open(target_path, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                f.write(chunk)

    def _recorded_sha256(self, resp, sent):
        """The server-recorded sha256 of a deploy, else the sent one."""
        with contextlib.suppress(Exception):
            recorded = ((resp.json().get("checksums") or {}).get("sha256") or "").lower()
            if recorded:
                return recorded
        return sent

    def put_object(self, repo, path, data, sha256=None):
        """Upload bytes; return the server-recorded sha256."""
        body = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        sha256 = sha256 or hashlib.sha256(body).hexdigest()
        resp = self._call(f"PUT {repo}/{path}", "PUT", self._item_url(repo, path),
                          data=body, headers={"X-Checksum-Sha256": sha256})
        return self._recorded_sha256(resp, sha256)

    def upload_file(self, repo, path, file_path, sha256=None):
        """Stream a file; return the server-recorded sha256."""
        sha256 = sha256 or _file_sha256(file_path)
        with open(file_path, "rb") as f:
            resp = self._call(f"upload {repo}/{path}", "PUT",
                              self._item_url(repo, path), data=f,
                              headers={"X-Checksum-Sha256": sha256})
        return self._recorded_sha256(resp, sha256)

    def delete(self, repo, path):
        try:
            self._call(f"DELETE {repo}/{path}", "DELETE", self._item_url(repo, path))
        except ArtifactoryNotFoundError:
            pass

    def delete_paths(self, repo, paths):
        """Delete paths one by one; already-gone paths are fine."""
        for path in paths:
            self.delete(repo, path)

    def repo_info(self, repo):
        """Return repo metadata, None if the repo does not exist."""
        try:
            return self._call(f"GET repo {repo}", "GET",
                              self._base("api/storage", repo)).json()
        except ArtifactoryNotFoundError:
            return None

    def file_info(self, repo, path):
        """Return artifact metadata (size, checksums), None if missing."""
        try:
            return self._call(f"GET info {repo}/{path}", "GET",
                              self._base("api/storage", repo, path)).json()
        except ArtifactoryNotFoundError:
            return None

    def aql_search(self, repo, dir_match, limit=10000, fields=None):
        """List files under dir_match (a "dir*" glob) in one AQL query.

        CE supports neither sort nor offset, so paging is impossible:
        one query with a large limit. Sweeps over huge repos should
        narrow the prefix instead.

        A non-admin may only run a query that includes repo, path and name.
        """
        query = (
            f'items.find({{"repo":"{repo}","path":{{"$match":"{dir_match}"}},'
            f'"type":"file"}})'
            f'.include({",".join(f'"{f}"' for f in (fields or ("repo", "name", "path", "size", "modified", "sha256")))})'
            f'.limit({limit})'
        )
        resp = self._call("AQL search", "POST", self._base("api/search/aql"),
                          data=query, headers={"Content-Type": "text/plain"})
        return resp.json().get("results", [])

    def list_all_objects(self, repo, prefix="", limit=10000):
        """Yield {"key", "size", "last_modified", ...} under prefix."""
        directory, _, _ = prefix.rpartition("/")
        dir_match = f"{directory}*" if directory else "*"
        for item in self.aql_search(repo, dir_match, limit=limit):
            key = f"{item['path']}/{item['name']}" if item["path"] != "." else item["name"]
            if prefix and not key.startswith(prefix):
                continue
            yield {
                "key": key,
                "size": item.get("size", 0),
                "last_modified": item.get("modified"),
                "sha256": (item.get("sha256") or "").lower() or None,
            }
