"""GitHub API client and streaming helpers."""

import base64
import re
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from io import BufferedReader
from pathlib import Path
from typing import Literal
from typing import Protocol
from typing import TypedDict
from typing import Unpack
from urllib.parse import quote

import requests
from portage.util import atomic_ofstream
from portage.util.backoff import ExponentialBackoff

from portage_github_binrepo.package import make_empty_packages
from portage_github_binrepo.package import validate_branch

API_VERSION = "2026-03-10"
BINREPO_BRANCH = "binrepo"
MAX_ASSET_SIZE = 2 * 1024**3
MUTATION_INTERVAL = 8
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
MUTATIVE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BACKOFF = ExponentialBackoff(limit=60)


class GitHubError(RuntimeError):
    pass


class Repository(TypedDict):
    default_branch: str
    private: bool


class User(TypedDict):
    login: str


class CheckResult(TypedDict):
    private: bool
    default_branch: str
    access: Literal["read", "write"]
    initialized: bool


class GitObject(TypedDict):
    sha: str


class GitRef(TypedDict, total=False):
    ref: str
    object: GitObject


class Content(TypedDict, total=False):
    content: str
    encoding: str
    git_url: str
    sha: str


class ContentUpdate(TypedDict):
    content: Content


class Blob(TypedDict):
    content: str
    encoding: str


class Release(TypedDict, total=False):
    id: int
    target_commitish: str
    tag_name: str
    name: str
    body: str
    upload_url: str


class Asset(TypedDict, total=False):
    id: int
    name: str
    size: int


type JSONValue = (
    bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
)


class RequestOptions(TypedDict, total=False):
    data: BufferedReader
    headers: Mapping[str, str]
    json: JSONValue
    params: Mapping[str, str]
    stream: bool


class CheckAPI(Protocol):
    def check(
        self, write: bool = True, branch: str = BINREPO_BRANCH
    ) -> CheckResult: ...


class InitAPI(CheckAPI, Protocol):
    def get_repository(self) -> Repository | None: ...
    def create_repository(self, private: bool = True) -> Repository: ...


class PullAPI(CheckAPI, Protocol):
    repository: str

    def get_ref(self, ref: str) -> GitRef | None: ...
    def get_content(self, path: str, ref: str) -> Content | None: ...
    def content_bytes(self, content: Content) -> bytes: ...
    def download_asset(self, asset_id: int, destination: Path) -> None: ...


class PushAPI(CheckAPI, Protocol):
    repository: str

    def initialize_repository(
        self, default_branch: str, branch: str = BINREPO_BRANCH
    ) -> GitRef: ...
    def delete_ref(self, ref: str) -> None: ...
    def get_content(self, path: str, ref: str) -> Content | None: ...
    def content_bytes(self, content: Content) -> bytes: ...
    def put_content(
        self,
        path: str,
        branch: str,
        content: bytes,
        message: str,
        sha: str | None = None,
    ) -> ContentUpdate: ...
    def get_release(self, tag: str) -> Release | None: ...
    def create_release(self, tag: str, branch: str) -> Release: ...
    def list_assets(self, release_id: int) -> list[Asset]: ...
    def upload_asset(self, release_id: int, path: Path, name: str) -> Asset: ...
    def delete_asset(self, asset_id: int) -> None: ...
    def delete_release(self, release_id: int) -> None: ...


class GitHubClient:
    def __init__(
        self,
        repository: str,
        token: str,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
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
        self._last_mutation: float | None = None
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
        timeout: float | tuple[float, float] = (10, 300),
        **kwargs: Unpack[RequestOptions],
    ) -> requests.Response:
        if url.startswith("/"):
            url = f"https://api.github.com{url}"
        data = kwargs.get("data")
        data_position = data.tell() if data is not None else None
        for attempt in range(retries + 1):
            if attempt and data is not None and data_position is not None:
                data.seek(data_position)
            if method in MUTATIVE_METHODS:
                now = time.monotonic()
                if self._last_mutation is not None:
                    delay = MUTATION_INTERVAL - (now - self._last_mutation)
                    if delay > 0:
                        self.sleep(delay)
                self._last_mutation = time.monotonic()
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as error:
                if attempt == retries or method not in {"GET", "HEAD"}:
                    raise GitHubError(  # noqa: TRY003
                        f"GitHub {method} request failed: {error}"
                    ) from error
                self.sleep(BACKOFF(attempt))
                continue
            if response.status_code in expected:
                return response
            message = _response_message(response)
            rate_limited = response.status_code == 429 or (
                response.status_code == 403
                and (
                    response.headers.get("Retry-After")
                    or response.headers.get("X-RateLimit-Remaining") == "0"
                    or "rate limit" in message.casefold()
                )
            )
            if (
                (response.status_code in TRANSIENT_STATUSES or rate_limited)
                and attempt < retries
                and (
                    method in {"GET", "HEAD", "PUT", "PATCH", "DELETE"} or rate_limited
                )
            ):
                retry_after = response.headers.get("Retry-After")
                reset = response.headers.get("X-RateLimit-Reset")
                if retry_after:
                    delay = float(retry_after)
                elif response.headers.get("X-RateLimit-Remaining") == "0" and reset:
                    delay = max(0, float(reset) - time.time())
                elif rate_limited:
                    delay = 60 * BACKOFF(attempt)
                else:
                    delay = BACKOFF(attempt)
                self.sleep(delay)
                continue
            raise GitHubError(  # noqa: TRY003
                f"GitHub {method} {response.url} returned {response.status_code}: {message}"
            )
        raise AssertionError("unreachable")

    def json[T](
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Unpack[RequestOptions],
    ) -> T:
        response = self.request(method, path, expected=expected, **kwargs)
        if not response.content:
            raise GitHubError("GitHub returned an empty JSON response")  # noqa: TRY003
        return response.json()

    def get_repository(self) -> Repository | None:
        response = self.request("GET", self._repo_path(), expected=(200, 404))
        if response.status_code == 404:
            return None
        repository: Repository = response.json()
        return repository

    def create_repository(self, private: bool = True) -> Repository:
        user: User = self.json("GET", "/user")
        if user["login"].casefold() == self.owner.casefold():
            path = "/user/repos"
        else:
            path = f"/orgs/{quote(self.owner, safe='')}/repos"
        return self.json(
            "POST",
            path,
            expected=(201,),
            json={
                "name": self.repo,
                "description": "Portage binary package repository",
                "private": private,
                "auto_init": False,
            },
        )

    def initialize_repository(
        self, default_branch: str, branch: str = BINREPO_BRANCH
    ) -> GitRef:
        branch = validate_branch(branch)
        readme = f"""# {self.repo}

Portage binary package repository maintained by
[portage-github-binrepo](https://github.com/KSmanis/portage-github-binrepo).

A portage hook updates this repository after every successful package merge.
Binary packages are stored as GitHub Release assets under
`{branch}/<shard>` tags. The package index is stored on the
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
        tree: GitObject = self.json(
            "POST",
            f"{self._repo_path()}/git/trees",
            expected=(201,),
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
        commit: GitObject = self.json(
            "POST",
            f"{self._repo_path()}/git/commits",
            expected=(201,),
            json={"message": "Initialize binrepo", "tree": tree["sha"], "parents": []},
        )
        try:
            return self.json(
                "POST",
                f"{self._repo_path()}/git/refs",
                expected=(201,),
                json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
        except GitHubError:
            ref = self.get_ref(f"heads/{branch}")
            if ref and ref.get("object", {}).get("sha") == commit["sha"]:
                return ref
            raise

    def check(self, write: bool = True, branch: str = BINREPO_BRANCH) -> CheckResult:
        branch = validate_branch(branch)
        repository = self.get_repository()
        if not repository:
            raise GitHubError(  # noqa: TRY003
                f"repository is missing or inaccessible: {self.repository}"
            )
        default_branch = repository["default_branch"]
        ref = self.get_ref(f"heads/{branch}")
        initialized = bool(ref)
        return {
            "private": repository["private"],
            "default_branch": default_branch,
            "access": "write" if write else "read",
            "initialized": initialized,
        }

    def get_ref(self, ref: str) -> GitRef | None:
        response = self.request(
            "GET",
            f"{self._repo_path()}/git/ref/{quote(ref, safe='/')}",
            expected=(200, 404, 409),
        )
        if response.status_code in {404, 409}:
            return None
        result: GitRef = response.json()
        return result

    def delete_ref(self, ref: str) -> None:
        self.request(
            "DELETE",
            f"{self._repo_path()}/git/refs/{quote(ref, safe='/')}",
            expected=(204, 404),
        )

    def get_content(self, path: str, ref: str) -> Content | None:
        response = self.request(
            "GET",
            f"{self._repo_path()}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
            expected=(200, 404),
        )
        if response.status_code == 404:
            return None
        content: Content = response.json()
        return content

    def content_bytes(self, content: Content) -> bytes:
        if content.get("encoding") == "base64" and content.get("content"):
            return base64.b64decode(content["content"])
        git_url = content.get("git_url")
        if not git_url:
            raise GitHubError("GitHub did not return content or a blob URL")  # noqa: TRY003
        blob: Blob = self.json("GET", git_url)
        if blob["encoding"] != "base64":
            raise GitHubError("GitHub returned an unsupported blob encoding")  # noqa: TRY003
        return base64.b64decode(blob["content"])

    def put_content(
        self,
        path: str,
        branch: str,
        content: bytes,
        message: str,
        sha: str | None = None,
    ) -> ContentUpdate:
        body: dict[str, JSONValue] = {
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

    def get_release(self, tag: str) -> Release | None:
        response = self.request(
            "GET",
            f"{self._repo_path()}/releases/tags/{quote(tag, safe='')}",
            expected=(200, 404),
        )
        if response.status_code == 404:
            return None
        release: Release = response.json()
        return release

    def create_release(self, tag: str, branch: str) -> Release:
        return self.json(
            "POST",
            f"{self._repo_path()}/releases",
            expected=(201,),
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

    def list_assets(self, release_id: int) -> list[Asset]:
        assets: list[Asset] = []
        url = f"{self._repo_path()}/releases/{release_id}/assets?per_page=100"
        while url:
            response = self.request("GET", url)
            page: list[Asset] = response.json()
            assets.extend(page)
            url = response.links.get("next", {}).get("url")
        return assets

    def upload_asset(self, release_id: int, path: Path, name: str) -> Asset:
        if path.stat().st_size >= MAX_ASSET_SIZE:
            raise ValueError("release assets must be smaller than 2 GiB")  # noqa: TRY003
        upload_url = (
            f"https://uploads.github.com{self._repo_path()}"
            f"/releases/{release_id}/assets"
        )
        with path.open("rb") as source:
            return self.json(
                "POST",
                upload_url,
                expected=(201,),
                params={"name": name},
                headers={"Content-Type": "application/octet-stream"},
                data=source,
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


def _response_message(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:500] or response.reason
    return str(data.get("message", data) if isinstance(data, dict) else data)[:500]


def write_stream(destination: str | Path, chunks: Iterable[bytes]) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with atomic_ofstream(destination, mode="wb", follow_links=False) as output:
        for chunk in chunks:
            if chunk:
                output.write(chunk)
