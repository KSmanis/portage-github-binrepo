"""GitHub API client and streaming helpers."""

import base64
import re
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from portage.util import atomic_ofstream
from portage.util.backoff import ExponentialBackoff

from portage_github_binrepo.package import make_empty_packages
from portage_github_binrepo.package import validate_branch

API_VERSION = "2026-03-10"
BINREPO_BRANCH = "binrepo"
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BACKOFF = ExponentialBackoff(limit=60)


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self,
        repository: str,
        token: str,
        session: Any = None,  # noqa: ANN401
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        try:
            owner, repo = repository.split("/", 1)
        except ValueError as error:
            raise ValueError("repository must be OWNER/REPOSITORY") from error  # noqa: TRY003
        if not NAME_RE.fullmatch(owner) or not NAME_RE.fullmatch(repo):
            raise ValueError("invalid GitHub repository name")  # noqa: TRY003
        self.owner = owner
        self.repo = repo
        self.repository = repository
        self.session = session or requests.Session()
        self.sleep = sleep
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "portage-github-binrepo",
            }
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        expected: tuple[int, ...] = (200,),
        retries: int = 3,
        **kwargs: Any,
    ) -> Any:  # noqa: ANN401
        if url.startswith("/"):
            url = f"https://api.github.com{url}"
        for attempt in range(retries + 1):
            try:
                response = self.session.request(
                    method, url, timeout=kwargs.pop("timeout", (10, 300)), **kwargs
                )
            except requests.RequestException as error:
                if attempt == retries or method not in {"GET", "HEAD"}:
                    raise GitHubError(  # noqa: TRY003
                        f"GitHub {method} request failed: {error}"
                    ) from error
                self.sleep(BACKOFF(attempt))
                continue
            if response.status_code in expected:
                return response
            retryable = response.status_code in TRANSIENT_STATUSES or (
                response.status_code == 403
                and (
                    response.headers.get("Retry-After")
                    or response.headers.get("X-RateLimit-Remaining") == "0"
                )
            )
            if (
                retryable
                and attempt < retries
                and method in {"GET", "HEAD", "PUT", "PATCH", "DELETE"}
            ):
                delay = response.headers.get("Retry-After")
                self.sleep(min(float(delay), 60) if delay else BACKOFF(attempt))
                continue
            message = _response_message(response)
            raise GitHubError(  # noqa: TRY003
                f"GitHub {method} {response.url} returned {response.status_code}: {message}"
            )
        raise AssertionError("unreachable")

    def json(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> Any:  # noqa: ANN401
        response = self.request(method, path, expected=expected, **kwargs)
        if not response.content:
            return None
        return response.json()

    def paginate(self, path: str) -> Iterator[Any]:
        url = path
        while url:
            response = self.request("GET", url)
            yield from response.json()
            url = response.links.get("next", {}).get("url")

    def repository_data(self) -> Any:  # noqa: ANN401
        repository = self.get_repository()
        if not repository:
            raise GitHubError(  # noqa: TRY003
                f"repository is missing or inaccessible: {self.repository}"
            )
        return repository

    def authenticated_user(self) -> Any:  # noqa: ANN401
        return self.json("GET", "/user")

    def get_repository(self) -> Any:  # noqa: ANN401
        response = self.request("GET", self._repo_path(), expected=(200, 404))
        return None if response.status_code == 404 else response.json()

    def create_repository(self, private: bool = True) -> Any:  # noqa: ANN401
        user = self.authenticated_user()
        if user["login"].casefold() == self.owner.casefold():
            path = "/user/repos"
        else:
            path = f"/orgs/{quote(self.owner, safe='')}/repos"
        return self.json(
            "POST",
            path,
            expected=(201,),
            retries=0,
            json={
                "name": self.repo,
                "description": "Portage binary package repository",
                "private": private,
                "auto_init": False,
            },
        )

    def initialize_repository(
        self, default_branch: str, branch: str = BINREPO_BRANCH
    ) -> Any:  # noqa: ANN401
        branch = validate_branch(branch)
        readme = f"""# {self.repo}

Portage binary package repository maintained by
[portage-github-binrepo](https://github.com/KSmanis/portage-github-binrepo).

A portage hook updates this repository after every successful package merge.
Binary packages are stored as GitHub Release assets under
`{branch}/<CHOST>/<category>/<package>` tags. The package index is stored on the
[`{branch}`](https://github.com/{self.repository}/tree/{branch}) branch.

## Usage

For a public repository, create `/etc/portage/binrepos.conf/github.conf`:

```ini
[github]
sync-uri = https://raw.githubusercontent.com/{self.repository}/{branch}
```

Private repositories require `portage-github-binrepo` and a read-only GitHub
token. Add these commands to the same section:

```ini
fetchcommand = /usr/bin/portage-github-binrepo pull "${{URI}}" "${{DISTDIR}}/${{FILE}}"
resumecommand = /usr/bin/portage-github-binrepo pull "${{URI}}" "${{DISTDIR}}/${{FILE}}"
```

> [!NOTE]
>
> If GPKG packages are intentionally unsigned, add `verify-signature = false`.

For setup and maintenance instructions, refer to the
[portage-github-binrepo documentation](https://github.com/KSmanis/portage-github-binrepo#readme).
"""
        ref = self.get_ref(f"heads/{branch}")
        if ref:
            return ref
        if not self.get_ref(f"heads/{default_branch}"):
            try:
                self.json(
                    "PUT",
                    f"{self._repo_path()}/contents/README.md",
                    expected=(201,),
                    json={
                        "message": "Initialize repo",
                        "content": base64.b64encode(readme.encode()).decode("ascii"),
                    },
                )
            except GitHubError:
                if not self.get_ref(f"heads/{default_branch}"):
                    raise
        tree = self.json(
            "POST",
            f"{self._repo_path()}/git/trees",
            expected=(201,),
            retries=0,
            json={
                "tree": [
                    {
                        "path": "Packages",
                        "mode": "100644",
                        "type": "blob",
                        "content": make_empty_packages(),
                    }
                ]
            },
        )
        commit = self.json(
            "POST",
            f"{self._repo_path()}/git/commits",
            expected=(201,),
            retries=0,
            json={"message": "Initialize binrepo", "tree": tree["sha"], "parents": []},
        )
        try:
            return self.json(
                "POST",
                f"{self._repo_path()}/git/refs",
                expected=(201,),
                retries=0,
                json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
        except GitHubError:
            ref = self.get_ref(f"heads/{branch}")
            if ref and ref.get("object", {}).get("sha") == commit["sha"]:
                return ref
            raise

    def check(self, write: bool = True, branch: str = BINREPO_BRANCH) -> dict[str, Any]:
        branch = validate_branch(branch)
        repository = self.repository_data()
        permissions = repository.get("permissions", {})
        if not permissions.get("pull"):
            raise GitHubError(f"token cannot read repository: {self.repository}")  # noqa: TRY003
        if write and not permissions.get("push"):
            raise GitHubError(f"token cannot write repository: {self.repository}")  # noqa: TRY003
        default_branch = repository["default_branch"]
        ref = self.get_ref(f"heads/{branch}")
        initialized = bool(ref)
        return {
            "private": repository["private"],
            "default_branch": default_branch,
            "access": "write" if write else "read",
            "initialized": initialized,
        }

    def get_ref(self, ref: str) -> Any:  # noqa: ANN401
        response = self.request(
            "GET",
            f"{self._repo_path()}/git/ref/{quote(ref, safe='/')}",
            expected=(200, 404, 409),
        )
        return None if response.status_code in {404, 409} else response.json()

    def delete_ref(self, ref: str) -> None:
        self.request(
            "DELETE",
            f"{self._repo_path()}/git/refs/{quote(ref, safe='/')}",
            expected=(204, 404),
        )

    def get_content(self, path: str, ref: str) -> Any:  # noqa: ANN401
        response = self.request(
            "GET",
            f"{self._repo_path()}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
            expected=(200, 404),
        )
        if response.status_code == 404:
            return None
        return response.json()

    def content_bytes(self, content: Mapping[str, Any]) -> bytes:
        if content.get("encoding") == "base64" and content.get("content"):
            return base64.b64decode(content["content"])
        git_url = content.get("git_url")
        if not git_url:
            raise GitHubError("GitHub did not return content or a blob URL")  # noqa: TRY003
        blob = self.json("GET", git_url)
        if blob.get("encoding") != "base64":
            raise GitHubError("GitHub returned an unsupported blob encoding")  # noqa: TRY003
        return base64.b64decode(blob["content"])

    def put_content(
        self,
        path: str,
        branch: str,
        content: bytes,
        message: str,
        sha: str | None = None,
    ) -> Any:  # noqa: ANN401
        body = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return self.json(
            "PUT",
            f"{self._repo_path()}/contents/{quote(path, safe='/')}",
            expected=(200, 201),
            json=body,
        )

    def get_release(self, tag: str) -> Any:  # noqa: ANN401
        response = self.request(
            "GET",
            f"{self._repo_path()}/releases/tags/{quote(tag, safe='')}",
            expected=(200, 404),
        )
        return None if response.status_code == 404 else response.json()

    def create_release(self, tag: str, branch: str) -> Any:  # noqa: ANN401
        return self.json(
            "POST",
            f"{self._repo_path()}/releases",
            expected=(201,),
            retries=0,
            json={
                "tag_name": tag,
                "target_commitish": branch,
                "name": tag,
                "body": "",
                "draft": False,
                "prerelease": False,
                "make_latest": "false",
            },
        )

    def list_assets(self, release_id: int) -> list[Any]:
        return list(
            self.paginate(
                f"{self._repo_path()}/releases/{release_id}/assets?per_page=100"
            )
        )

    def upload_asset(self, release: Mapping[str, Any], path: Path, name: str) -> Any:  # noqa: ANN401
        upload_url = release["upload_url"].split("{", 1)[0]
        with path.open("rb") as source:
            return self.json(
                "POST",
                upload_url,
                expected=(201,),
                retries=0,
                params={"name": name},
                headers={"Content-Type": "application/octet-stream"},
                data=source,
            )

    def rename_asset(self, asset_id: int, name: str) -> Any:  # noqa: ANN401
        return self.json(
            "PATCH",
            f"{self._repo_path()}/releases/assets/{asset_id}",
            json={"name": name},
        )

    def delete_asset(self, asset_id: int) -> None:
        self.request(
            "DELETE",
            f"{self._repo_path()}/releases/assets/{asset_id}",
            expected=(204, 404),
        )

    def delete_release(self, release_id: int) -> None:
        self.request(
            "DELETE", f"{self._repo_path()}/releases/{release_id}", expected=(204, 404)
        )

    def download_asset(self, asset_id: int, destination: Path) -> None:
        with self.request(
            "GET",
            f"https://api.github.com{self._repo_path()}/releases/assets/{asset_id}",
            headers={"Accept": "application/octet-stream"},
            stream=True,
        ) as response:
            write_stream(destination, response.iter_content(chunk_size=1024 * 1024))

    def _repo_path(self) -> str:
        return f"/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}"


def _response_message(response: Any) -> str:  # noqa: ANN401
    try:
        data = response.json()
    except ValueError:
        return response.text[:500] or response.reason
    return str(data.get("message", data))[:500]


def write_stream(destination: str | Path, chunks: Iterable[bytes]) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with atomic_ofstream(destination, mode="wb", follow_links=False) as output:
        for chunk in chunks:
            if chunk:
                output.write(chunk)
