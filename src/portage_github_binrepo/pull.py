"""Pull binrepo indexes and assets from GitHub."""

import gzip
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse

from portage.locks import lockfile
from portage.locks import unlockfile

from portage_github_binrepo.github import BINREPO_BRANCH
from portage_github_binrepo.github import GitHubError
from portage_github_binrepo.github import write_stream
from portage_github_binrepo.package import _restore_package_paths
from portage_github_binrepo.package import make_empty_packages
from portage_github_binrepo.package import parse_packages
from portage_github_binrepo.package import release_coordinates
from portage_github_binrepo.package import validate_branch
from portage_github_binrepo.package import validate_remote_package_path


def write_empty_index(uri: str, destination: str | Path) -> bool:
    parsed = urlparse(uri)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.hostname != "raw.githubusercontent.com"
        or len(parts) < 4
        or parts[-1] not in {"Packages", "Packages.gz"}
    ):
        return False
    data = make_empty_packages().encode()
    if parts[-1] == "Packages.gz":
        data = gzip.compress(data, mtime=0)
    write_stream(Path(destination), [data])
    return True


def pull(client: Any, uri: str, destination: str | Path) -> None:  # noqa: ANN401
    parsed = urlparse(uri)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
        owner, repo = parts[:2]
        branch = validate_branch("/".join(parts[2:-1]))
        if f"{owner}/{repo}" != client.repository or parts[-1] not in {
            "Packages",
            "Packages.gz",
        }:
            raise ValueError("index URI does not match configured repository")  # noqa: TRY003
        if not client.get_ref(f"heads/{branch}"):
            if client.check(write=False, branch=branch)["initialized"]:
                raise GitHubError(f"branch not found: {branch}")  # noqa: TRY003
            write_empty_index(uri, destination)
            return
        content = client.get_content("Packages", branch)
        if not content:
            raise GitHubError(f"Packages was not found on branch {branch}")  # noqa: TRY003
        data = client.content_bytes(content)
        if parts[-1] == "Packages.gz":
            data = gzip.compress(data, mtime=0)
        write_stream(Path(destination), [data])
        return
    if parsed.hostname != "github.com" or len(parts) < 7:
        raise ValueError("unsupported binrepo URI")  # noqa: TRY003
    if parts[2:4] != ["releases", "download"] or (
        f"{parts[0]}/{parts[1]}" != client.repository
    ):
        raise ValueError("asset URI does not match configured repository")  # noqa: TRY003
    tag = "/".join(parts[4:-1])
    name = parts[-1]
    release = client.get_release(tag)
    if not release:
        raise GitHubError(f"release not found: {tag}")  # noqa: TRY003
    asset = next(
        (item for item in client.list_assets(release["id"]) if item["name"] == name),
        None,
    )
    if not asset:
        raise GitHubError(f"release asset not found: {name}")  # noqa: TRY003
    client.download_asset(asset["id"], Path(destination))


def pull_all(
    client: Any,  # noqa: ANN401
    pkgdir: str | Path,
    branch: str = BINREPO_BRANCH,
) -> None:
    branch = validate_branch(branch)
    pkgdir = Path(pkgdir).resolve()
    pkgdir.parent.mkdir(parents=True, exist_ok=True)
    status = client.check(write=False, branch=branch)
    content = client.get_content("Packages", branch)
    if content:
        remote_text = client.content_bytes(content).decode("utf-8")
    elif status["initialized"]:
        raise GitHubError(  # noqa: TRY003
            f"Packages was not found on branch {branch}"
        )
    else:
        remote_text = make_empty_packages()

    remote_entries = parse_packages(remote_text)
    local_text = _restore_package_paths(remote_text)
    local_entries = parse_packages(local_text)

    with tempfile.TemporaryDirectory(
        dir=pkgdir.parent, prefix=f".{pkgdir.name}."
    ) as temporary:
        staging = Path(temporary)
        write_stream(staging / "Packages", [local_text.encode()])
        for remote_path, local_path in zip(remote_entries, local_entries, strict=True):
            validate_remote_package_path(remote_path, branch)
            tag, name = release_coordinates(remote_path)
            uri = (
                f"https://github.com/{client.repository}/releases/download/"
                f"{quote(tag, safe='/')}/{quote(name, safe='')}"
            )
            pull(client, uri, staging / local_path)
        _replace_cache(pkgdir, staging)


def pull_locked(
    client: Any,  # noqa: ANN401
    pkgdir: str | Path,
    branch: str = BINREPO_BRANCH,
) -> None:
    pkgdir = Path(pkgdir).resolve()
    pkgdir.mkdir(parents=True, exist_ok=True)
    lock = lockfile(str(pkgdir / "Packages"), wantnewlockfile=True)
    try:
        pull_all(client, pkgdir, branch)
    finally:
        unlockfile(lock)


def _replace_cache(pkgdir: Path, staging: Path) -> None:
    for path in sorted(
        pkgdir.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if path.name.endswith(".portage_lockfile"):
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    for source in staging.rglob("*"):
        if source.is_file():
            destination = pkgdir / source.relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)


def repository_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.hostname not in {"github.com", "raw.githubusercontent.com"}
        or len(parts) < 2
    ):
        raise ValueError("unsupported binrepo URI")  # noqa: TRY003
    return f"{parts[0]}/{parts[1]}"
