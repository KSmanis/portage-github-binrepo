"""Push local Portage packages to GitHub Releases."""

import hashlib
import sys
from collections import defaultdict
from pathlib import Path

from portage.locks import lockfile
from portage.locks import unlockfile

from portage_github_binrepo.github import BINREPO_BRANCH
from portage_github_binrepo.github import GitHubError
from portage_github_binrepo.github import PushAPI
from portage_github_binrepo.package import LOCAL_PATH_FIELD
from portage_github_binrepo.package import _cleanup_assets
from portage_github_binrepo.package import _push_commit_message
from portage_github_binrepo.package import _push_package_paths
from portage_github_binrepo.package import _restore_package_paths
from portage_github_binrepo.package import asset_ids
from portage_github_binrepo.package import asset_name
from portage_github_binrepo.package import parse_packages
from portage_github_binrepo.package import release_coordinates
from portage_github_binrepo.package import remote_ids
from portage_github_binrepo.package import validate_branch
from portage_github_binrepo.package import validate_package_path
from portage_github_binrepo.package import with_remote_uri

RELEASE_ASSET_LIMIT = 1000


def push(
    client: PushAPI, pkgdir: str | Path, branch: str = BINREPO_BRANCH
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
    local_packages = {
        path: _local_package(pkgdir, path, stanza)
        for path, stanza in local_entries.items()
    }
    if not status["initialized"]:
        client.initialize_repository(status["default_branch"], branch)
    previous = client.get_content("Packages", branch)
    if previous:
        previous_text = client.content_bytes(previous).decode("utf-8")
        previous_remote_entries = parse_packages(previous_text)
        previous_entries = parse_packages(_restore_package_paths(previous_text))
    else:
        previous_text = ""
        previous_remote_entries = {}
        previous_entries = {}

    previous_asset_ids = asset_ids(previous_text)

    previous_remote_by_local = _remote_entries_by_local(previous_remote_entries, branch)
    _delete_cleanup(
        client,
        branch,
        _cleanup_assets(previous_text),
        {
            remote_ids(metadata, previous_asset_ids)[1]
            for metadata in previous_remote_entries.values()
        },
        {
            remote_ids(metadata, previous_asset_ids)[0]
            for metadata in previous_remote_entries.values()
        },
    )

    changed = [
        path
        for path, stanza in local_entries.items()
        if previous_entries.get(path) != stanza
    ]
    removed = previous_entries.keys() - local_entries.keys()
    commit_message = _push_commit_message(
        local_entries, previous_entries, changed, removed
    )

    releases: dict[int, int] = {}
    counts: dict[int, int] = defaultdict(int)
    shards: dict[int, int] = {}
    for metadata in previous_remote_entries.values():
        release_id, _ = remote_ids(metadata, previous_asset_ids)
        shard = int(release_coordinates(metadata["PATH"], branch)[0].rsplit("/", 1)[1])
        existing = releases.setdefault(release_id, shard)
        if existing != shard or shards.setdefault(shard, release_id) != release_id:
            raise ValueError("Packages contains conflicting release metadata")  # noqa: TRY003
        counts[release_id] += 1

    published = {
        local_path: (metadata["PATH"], *remote_ids(metadata, previous_asset_ids))
        for local_path, metadata in previous_remote_by_local.items()
        if local_path in local_entries and local_path not in changed
    }
    desired_names = {}
    for package_path in changed:
        source = local_packages[package_path]
        metadata = local_entries[package_path]
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        desired_names[package_path] = asset_name(
            package_path,
            metadata["CPV"],
            metadata["CHOST"],
            digest,
            metadata.get("BUILD_ID"),
        )
        previous_metadata = previous_remote_by_local.get(package_path)
        if (
            previous_metadata
            and Path(previous_metadata["PATH"]).name == desired_names[package_path]
        ):
            published[package_path] = (
                previous_metadata["PATH"],
                *remote_ids(previous_metadata, previous_asset_ids),
            )

    uploaded: list[int] = []
    created_releases: list[tuple[int, str]] = []
    rollback_safe = True
    try:
        for package_path in changed:
            if package_path in published:
                continue
            release_id, shard = _release_with_capacity(
                client, branch, releases, counts, shards, created_releases
            )
            source = local_packages[package_path]
            name = desired_names[package_path]
            print(f"Uploading {package_path}", file=sys.stderr)
            try:
                asset = client.upload_asset(release_id, source, name)
            except GitHubError:
                asset = next(
                    (
                        item
                        for item in client.list_assets(release_id)
                        if item["name"] == name
                        and item.get("size") == source.stat().st_size
                    ),
                    None,
                )
                if asset is None:
                    raise
            if asset.get("name") != name or asset.get("size") != source.stat().st_size:
                raise GitHubError(f"GitHub returned invalid asset metadata for {name}")  # noqa: TRY003, TRY301
            asset_id = int(asset["id"])
            uploaded.append(asset_id)
            counts[release_id] += 1
            published[package_path] = (f"{branch}/{shard}/{name}", release_id, asset_id)

        cleanup = {
            (
                *remote_ids(metadata, previous_asset_ids),
                release_coordinates(metadata["PATH"], branch)[0],
            )
            for local_path, metadata in previous_remote_by_local.items()
            if local_path not in published
            or remote_ids(metadata, previous_asset_ids)[1] != published[local_path][2]
        }
        remote_text = _push_package_paths(local_text, published)
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
            for asset_id in reversed(uploaded):
                client.delete_asset(asset_id)
            for release_id, tag in reversed(created_releases):
                client.delete_release(release_id)
                client.delete_ref(f"tags/{tag}")
        raise

    _delete_cleanup(
        client,
        branch,
        cleanup,
        {asset_id for _, _, asset_id in published.values()},
        {release_id for _, release_id, _ in published.values()},
    )

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
        "uploaded": len(uploaded),
        "removed": len(removed),
        "unchanged": len(local_entries) - len(changed),
    }


def _remote_entries_by_local(
    entries: dict[str, dict[str, str]], branch: str
) -> dict[str, dict[str, str]]:
    result = {}
    for remote_path, metadata in entries.items():
        release_coordinates(remote_path, branch)
        local_path = metadata.get(LOCAL_PATH_FIELD)
        if local_path is None:
            raise ValueError(f"package stanza is missing {LOCAL_PATH_FIELD}")  # noqa: TRY003
        validate_package_path(local_path)
        if local_path in result:
            raise ValueError(f"duplicate {LOCAL_PATH_FIELD} in Packages: {local_path}")  # noqa: TRY003
        result[local_path] = metadata
    return result


def _release_with_capacity(
    client: PushAPI,
    branch: str,
    releases: dict[int, int],
    counts: dict[int, int],
    shards: dict[int, int],
    created: list[tuple[int, str]],
) -> tuple[int, int]:
    for release_id, shard in sorted(releases.items(), key=lambda item: item[1]):
        if counts[release_id] < RELEASE_ASSET_LIMIT:
            return release_id, shard
    shard = next(value for value in range(len(shards) + 1) if value not in shards)
    tag = f"{branch}/{shard}"
    try:
        release = client.create_release(tag, branch)
    except GitHubError as error:
        release = client.get_release(tag)
        if not release:
            raise
        if client.list_assets(int(release["id"])):
            raise GitHubError(  # noqa: TRY003
                f"release {tag} contains assets not recorded in Packages; delete it "
                f"and its tag with `gh release delete {tag} --cleanup-tag --repo "
                f"{client.repository}`, then retry"
            ) from error
    release_id = int(release["id"])
    releases[release_id] = shard
    shards[shard] = release_id
    counts[release_id] = 0
    created.append((release_id, tag))
    return release_id, shard


def _delete_cleanup(
    client: PushAPI,
    branch: str,
    cleanup: set[tuple[int, int, str]],
    active_asset_ids: set[int],
    active_release_ids: set[int],
) -> None:
    releases: dict[int, str] = {}
    for release_id, asset_id, tag in sorted(cleanup):
        release_coordinates(f"{tag}/asset", branch)
        if asset_id in active_asset_ids:
            raise ValueError(f"cleanup references active asset {asset_id}")  # noqa: TRY003
        if releases.setdefault(release_id, tag) != tag:
            raise ValueError("cleanup contains conflicting release metadata")  # noqa: TRY003
    for _, asset_id, _ in sorted(cleanup):
        client.delete_asset(asset_id)
    for release_id, tag in releases.items():
        if release_id not in active_release_ids:
            client.delete_release(release_id)
            client.delete_ref(f"tags/{tag}")


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


def push_locked(
    client: PushAPI, pkgdir: str | Path, branch: str = BINREPO_BRANCH
) -> dict[str, int]:
    pkgdir = Path(pkgdir).resolve()
    lock = lockfile(str(pkgdir / "Packages"), wantnewlockfile=True)
    try:
        return push(client, pkgdir, branch)
    finally:
        unlockfile(lock)
