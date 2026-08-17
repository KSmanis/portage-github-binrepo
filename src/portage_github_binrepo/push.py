"""Push local Portage packages to GitHub Releases."""

import sys
import uuid
from pathlib import Path
from typing import Any

from portage.locks import lockfile
from portage.locks import unlockfile

from portage_github_binrepo.github import BINREPO_BRANCH
from portage_github_binrepo.github import GitHubError
from portage_github_binrepo.package import BACKUP_MARKER
from portage_github_binrepo.package import _cleanup_paths
from portage_github_binrepo.package import _push_commit_message
from portage_github_binrepo.package import _push_package_paths
from portage_github_binrepo.package import _remote_package_path
from portage_github_binrepo.package import _restore_package_paths
from portage_github_binrepo.package import parse_packages
from portage_github_binrepo.package import release_coordinates
from portage_github_binrepo.package import validate_branch
from portage_github_binrepo.package import validate_remote_package_path
from portage_github_binrepo.package import with_remote_uri


def ensure_binrepo_branch(
    client: Any,  # noqa: ANN401
    status: dict[str, Any],
    branch: str,
) -> str:
    if not status["initialized"]:
        client.initialize_repository(status["default_branch"], branch)
    return branch


def push(
    client: Any,  # noqa: ANN401
    pkgdir: str | Path,
    branch: str = BINREPO_BRANCH,
) -> dict[str, int]:
    branch = validate_branch(branch)
    status = client.check(write=True, branch=branch)
    pkgdir = Path(pkgdir).resolve()
    local_path = pkgdir / "Packages"
    try:
        local_text = local_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ValueError(  # noqa: TRY003
            "Packages index is missing; run `emaint binhost --fix`"
        ) from error
    local_entries = parse_packages(local_text)
    remote_paths = {}
    for path, metadata in local_entries.items():
        remote_path = _remote_package_path(
            path, metadata["CPV"], metadata["CHOST"], branch
        )
        previous_path = remote_paths.get(remote_path)
        if previous_path is not None:
            raise ValueError(  # noqa: TRY003
                f"package PATHs {previous_path} and {path} both map to "
                f"{remote_path}; remove one package file and run "
                "`emaint binhost --fix`"
            )
        remote_paths[remote_path] = path
    local_packages = {
        path: _local_package(pkgdir, path, stanza)
        for path, stanza in local_entries.items()
    }
    branch = ensure_binrepo_branch(client, status, branch)
    previous = client.get_content("Packages", branch)
    if previous:
        previous_text = client.content_bytes(previous).decode("utf-8")
        previous_entries = parse_packages(_restore_package_paths(previous_text))
    else:
        previous_text = ""
        previous_entries = {}

    previous_remote_paths = {
        _remote_package_path(path, metadata["CPV"], metadata["CHOST"], branch): path
        for path, metadata in previous_entries.items()
    }

    changed = [
        path
        for path, stanza in local_entries.items()
        if previous_entries.get(path) != stanza
    ]
    removed = previous_entries.keys() - local_entries.keys()
    commit_message = _push_commit_message(
        local_entries, previous_entries, changed, removed
    )
    for remote_path in sorted(_cleanup_paths(previous_text)):
        validate_remote_package_path(remote_path, branch)
        _prune_asset(client, remote_path, keep=remote_path in previous_remote_paths)

    pushed = []
    created_releases = []
    cleanup = {
        remote_path
        for remote_path, path in previous_remote_paths.items()
        if path in removed
        or remote_path
        != _remote_package_path(
            path, local_entries[path]["CPV"], local_entries[path]["CHOST"], branch
        )
    }
    rollback_safe = True
    try:
        for package_path in changed:
            source = local_packages[package_path]
            metadata = local_entries[package_path]
            remote_path = _remote_package_path(
                package_path, metadata["CPV"], metadata["CHOST"], branch
            )
            tag, name = release_coordinates(remote_path)
            release = client.get_release(tag)
            if not release:
                release = client.create_release(tag, branch)
                created_releases.append(release)
            assets = {
                asset["name"]: asset for asset in client.list_assets(release["id"])
            }
            if any(
                asset_name.startswith(f"{name}{BACKUP_MARKER}") for asset_name in assets
            ):
                cleanup.add(remote_path)
            backup = None
            if name in assets:
                backup = assets[name]
                backup_name = f"{name}{BACKUP_MARKER}{uuid.uuid4().hex}"
                client.rename_asset(backup["id"], backup_name)
                backup = {**backup, "name": backup_name}
            try:
                print(f"Uploading {package_path}", file=sys.stderr)
                asset = client.upload_asset(release, source, name)
            except GitHubError:
                try:
                    reconciled = {
                        item["name"]: item for item in client.list_assets(release["id"])
                    }
                except GitHubError:
                    if backup:
                        client.rename_asset(backup["id"], name)
                    raise
                asset = reconciled.get(name)
                if not asset or asset.get("size") != source.stat().st_size:
                    if backup:
                        client.rename_asset(backup["id"], name)
                    raise
            pushed.append((asset, backup, name))
            if backup:
                cleanup.add(remote_path)

        remote_text = _push_package_paths(local_text, branch)
        index = with_remote_uri(remote_text, client.repository, cleanup)
        index_bytes = index.encode()
        index_sha = previous.get("sha") if previous else None
        if index != previous_text:
            try:
                committed = client.put_content(
                    "Packages", branch, index_bytes, commit_message, index_sha
                )
                index_sha = committed["content"]["sha"]
            except GitHubError:
                try:
                    committed = client.get_content("Packages", branch)
                except GitHubError:
                    rollback_safe = False
                    raise
                if not committed or client.content_bytes(committed) != index_bytes:
                    raise
                index_sha = committed["sha"]
    except Exception:
        if rollback_safe:
            for asset, backup, original_name in reversed(pushed):
                client.delete_asset(asset["id"])
                if backup:
                    client.rename_asset(backup["id"], original_name)
            for release in reversed(created_releases):
                if not client.list_assets(release["id"]):
                    client.delete_release(release["id"])
                    client.delete_ref(f"tags/{release['tag_name']}")
        raise

    for remote_path in sorted(cleanup):
        _prune_asset(client, remote_path, keep=remote_path in remote_paths)

    clean_index = with_remote_uri(remote_text, client.repository)
    if clean_index != index:
        clean_bytes = clean_index.encode()
        try:
            client.put_content(
                "Packages", branch, clean_bytes, "Remove cleanup markers", index_sha
            )
        except GitHubError:
            committed = client.get_content("Packages", branch)
            if not committed or client.content_bytes(committed) != clean_bytes:
                raise

    return {
        "uploaded": len(changed),
        "removed": len(removed),
        "unchanged": len(local_entries) - len(changed),
    }


def _local_package(pkgdir: Path, package_path: str, metadata: dict[str, str]) -> Path:
    path = (pkgdir / package_path).resolve()
    if not path.is_relative_to(pkgdir) or not path.is_file():
        raise ValueError(f"package file is missing: {package_path}")  # noqa: TRY003
    size = metadata.get("SIZE")
    try:
        expected_size = int(size or "")
    except (TypeError, ValueError) as error:
        raise ValueError(  # noqa: TRY003
            f"package stanza has an invalid SIZE: {package_path}"
        ) from error
    actual_size = path.stat().st_size
    if expected_size < 0 or actual_size != expected_size:
        raise ValueError(  # noqa: TRY003
            f"package size mismatch for {package_path}: expected {expected_size}, found {actual_size}"
        )
    return path


def _prune_asset(
    client: Any,  # noqa: ANN401
    package_path: str,
    keep: bool = False,
) -> None:
    tag, name = release_coordinates(package_path)
    release = client.get_release(tag)
    if not release:
        ref = f"tags/{tag}"
        if not keep and client.get_ref(ref):
            client.delete_ref(ref)
        return
    assets = client.list_assets(release["id"])
    for asset in assets:
        if asset["name"].startswith(f"{name}{BACKUP_MARKER}") or (
            not keep and asset["name"] == name
        ):
            client.delete_asset(asset["id"])
    remaining = client.list_assets(release["id"])
    if not remaining:
        client.delete_release(release["id"])
        client.delete_ref(f"tags/{tag}")


def push_locked(
    client: Any,  # noqa: ANN401
    pkgdir: str | Path,
    branch: str = BINREPO_BRANCH,
) -> dict[str, int]:
    pkgdir = Path(pkgdir).resolve()
    lock = lockfile(str(pkgdir / "Packages"), wantnewlockfile=True)
    try:
        return push(client, pkgdir, branch)
    finally:
        unlockfile(lock)
