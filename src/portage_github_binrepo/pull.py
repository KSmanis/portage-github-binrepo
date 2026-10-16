"""Pull binrepo indexes and assets from GitHub."""

from __future__ import annotations

import errno
import gzip
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote
from urllib.parse import urlparse

from portage.const import CACHE_PATH
from portage.locks import lockfile
from portage.locks import unlockfile

from portage_github_binrepo.github import BINREPO_BRANCH
from portage_github_binrepo.github import GitHubError
from portage_github_binrepo.github import write_stream
from portage_github_binrepo.package import _restore_package_paths
from portage_github_binrepo.package import asset_id
from portage_github_binrepo.package import asset_ids
from portage_github_binrepo.package import make_empty_packages
from portage_github_binrepo.package import parse_packages
from portage_github_binrepo.package import release_coordinates
from portage_github_binrepo.package import validate_branch

if TYPE_CHECKING:
    from typing import Final

    from portage_github_binrepo.github import PullAPI

GITHUB_HOST: Final = "github.com"
RAW_GITHUB_HOST: Final = "raw.githubusercontent.com"
COMPRESSED_INDEX_NAME: Final = "Packages.gz"


def write_empty_index(uri: str, destination: str | Path) -> bool:
    parsed = urlparse(uri)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.hostname != RAW_GITHUB_HOST
        or len(parts) < 4
        or parts[-1] not in {"Packages", COMPRESSED_INDEX_NAME}
    ):
        return False
    data = make_empty_packages().encode()
    if parts[-1] == COMPRESSED_INDEX_NAME:
        data = gzip.compress(data, mtime=0)
    write_stream(Path(destination), [data])
    return True


def pull(
    client: PullAPI, uri: str, destination: str | Path, packages_text: str | None = None
) -> None:
    parsed = urlparse(uri)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.hostname == RAW_GITHUB_HOST and len(parts) >= 4:
        _pull_index(client, uri, destination, parts)
        return
    if parsed.hostname != GITHUB_HOST or len(parts) < 7:
        raise ValueError("unsupported binrepo URI")  # noqa: TRY003
    if parts[2:4] != ["releases", "download"] or (
        f"{parts[0]}/{parts[1]}" != client.repository
    ):
        raise ValueError("asset URI does not match configured repository")  # noqa: TRY003
    if packages_text is None:
        raise GitHubError("cached Packages index is required for asset downloads")  # noqa: TRY003
    remote_path = "/".join(parts[4:])
    branch = "/".join(parts[4:-2])
    release_coordinates(remote_path, branch)
    metadata = parse_packages(packages_text).get(remote_path)
    if metadata is None:
        raise GitHubError(f"release asset not found in Packages: {remote_path}")  # noqa: TRY003
    client.download_asset(
        asset_id(metadata, asset_ids(packages_text)), Path(destination)
    )


def _pull_index(
    client: PullAPI, uri: str, destination: str | Path, parts: list[str]
) -> None:
    owner, repo = parts[:2]
    branch = validate_branch("/".join(parts[2:-1]))
    if f"{owner}/{repo}" != client.repository or parts[-1] not in {
        "Packages",
        COMPRESSED_INDEX_NAME,
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
    if parts[-1] == COMPRESSED_INDEX_NAME:
        data = gzip.compress(data, mtime=0)
    write_stream(Path(destination), [data])


def pull_all(client: PullAPI, pkgdir: str | Path, branch: str = BINREPO_BRANCH) -> None:
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
    remote_asset_ids = asset_ids(remote_text)
    local_text = _restore_package_paths(remote_text)
    local_entries = parse_packages(local_text)

    with tempfile.TemporaryDirectory(
        dir=pkgdir.parent, prefix=f".{pkgdir.name}."
    ) as temporary:
        staging = Path(temporary)
        write_stream(staging / "Packages", [local_text.encode()])
        for remote_path, local_path in zip(remote_entries, local_entries, strict=True):
            release_coordinates(remote_path, branch)
            client.download_asset(
                asset_id(remote_entries[remote_path], remote_asset_ids),
                staging / local_path,
            )
        _replace_cache(pkgdir, staging)


def pull_locked(
    client: PullAPI, pkgdir: str | Path, branch: str = BINREPO_BRANCH
) -> None:
    pkgdir = Path(pkgdir).resolve()
    pkgdir.mkdir(parents=True, exist_ok=True)
    lock = lockfile(str(pkgdir / "Packages"), wantnewlockfile=True)
    try:
        pull_all(client, pkgdir, branch)
    finally:
        unlockfile(lock)


def _replace_cache(pkgdir: Path, staging: Path) -> None:
    pkgdir.mkdir(parents=True, exist_ok=True)
    backup_prefix = f".{pkgdir.name}.binrepo-backup-"
    for retained in pkgdir.parent.glob(f"{backup_prefix}*"):
        raise FileExistsError(  # noqa: TRY003
            f"Recover retained cache backup before pulling: {retained}"
        )
    paths = [
        path
        for path in pkgdir.rglob("*")
        if (path.is_symlink() or path.is_file())
        and not path.name.endswith(".portage_lockfile")
    ]
    backup = Path(tempfile.mkdtemp(dir=pkgdir.parent, prefix=backup_prefix))
    saved: list[Path] = []
    installed: list[Path] = []
    try:
        for path in paths:
            relative = path.relative_to(pkgdir)
            _move_cache_file(path, backup / relative)
            saved.append(relative)
        for source in staging.rglob("*"):
            if not source.is_file():
                continue
            destination = pkgdir / source.relative_to(staging)
            # A failed cross-filesystem copy can leave a partial destination.
            installed.append(destination)
            _move_cache_file(source, destination)
    except BaseException:
        try:
            for destination in reversed(installed):
                # A failed rename may have collided with an existing directory.
                if destination.is_dir() and not destination.is_symlink():
                    continue
                destination.unlink(missing_ok=True)
            _remove_empty_cache_directories(pkgdir)
            for relative in saved:
                _move_cache_file(backup / relative, pkgdir / relative)
        except BaseException as error:
            raise OSError(  # noqa: TRY003
                f"Cache restoration failed; backup retained at {backup}: {error}"
            ) from error
        shutil.rmtree(backup)
        raise
    try:
        shutil.rmtree(backup)
    except OSError as error:
        raise OSError(  # noqa: TRY003
            f"New cache installed, but backup cleanup failed at {backup}; "
            f"remove the retained backup before retrying: {error}"
        ) from error
    _remove_empty_cache_directories(pkgdir)


def _remove_empty_cache_directories(pkgdir: Path) -> None:
    for path in sorted(
        pkgdir.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()


def _move_cache_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        shutil.copy2(source, destination, follow_symlinks=False)
        source.unlink()


def repository_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parsed.hostname not in {GITHUB_HOST, RAW_GITHUB_HOST} or len(parts) < 2:
        raise ValueError("unsupported binrepo URI")  # noqa: TRY003
    return f"{parts[0]}/{parts[1]}"


def cached_packages_path(uri: str, eroot: str | Path) -> Path:
    parsed = urlparse(uri)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.hostname != GITHUB_HOST
        or len(parts) < 7
        or parts[2:4] != ["releases", "download"]
    ):
        raise ValueError("unsupported binrepo asset URI")  # noqa: TRY003
    branch = parts[4:-2]
    if not branch:
        raise ValueError("unsupported binrepo asset URI")  # noqa: TRY003
    return (
        Path(eroot)
        / CACHE_PATH
        / "binhost"
        / RAW_GITHUB_HOST
        / parts[0]
        / parts[1]
        / Path(*branch)
        / "Packages"
    )
