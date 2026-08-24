import base64
import errno
import gzip
import hashlib
from collections.abc import Iterator
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Never
from unittest.mock import Mock

import pytest
from inline_snapshot import snapshot
from portage import getbinpkg
from portage.versions import pkgsplit

from portage_github_binrepo import cli
from portage_github_binrepo import github
from portage_github_binrepo import init
from portage_github_binrepo import package as package_module
from portage_github_binrepo import pull
from portage_github_binrepo import push

Asset = github.Asset
Release = github.Release
Content = github.Content


def make_packages(
    *paths: str, sizes: Mapping[str, int] | None = None, chost: str = "host"
) -> str:
    sizes = sizes or {}
    entries = []
    for index, path in enumerate(paths, 1):
        parts = Path(path).parts
        if len(parts) == 2:
            filename = parts[1]
            stem = filename.removesuffix(".gpkg.tar").removesuffix(".tbz2")
            split = pkgsplit(stem)
            assert split is not None
            cpv = f"{parts[0]}/{stem}"
        else:
            cpv = f"{parts[0]}/{parts[1]}-{index}"
        entries.append(
            "\n".join(
                [f"CPV: {cpv}", f"PATH: {path}", f"SIZE: {sizes.get(path, index)}"]
            )
        )
    return "\n\n".join([f"PACKAGES: {len(paths)}\nCHOST: {chost}", *entries]) + "\n\n"


def make_remote_packages(
    *paths: str, sizes: Mapping[str, int] | None = None, chost: str = "host"
) -> str:
    packages = make_packages(*paths, sizes=sizes, chost=chost)
    entries = package_module.parse_packages(packages)
    published = {}
    for asset_id, (path, metadata) in enumerate(entries.items(), 9):
        name = package_module.asset_name(
            path, metadata["CPV"], metadata["CHOST"], "0" * 64
        )
        published[path] = (f"{github.BINREPO_BRANCH}/0/{name}", 1, asset_id)
    return package_module._push_package_paths(packages, published)


def remote_entry(packages: str, local_path: str) -> tuple[str, dict[str, str]]:
    return next(
        (path, metadata)
        for path, metadata in package_module.parse_packages(packages).items()
        if metadata[package_module.LOCAL_PATH_FIELD] == local_path
    )


def remote_ids(packages: str, metadata: Mapping[str, str]) -> tuple[int, int]:
    return package_module.remote_ids(metadata, package_module.asset_ids(packages))


def expected_asset_name(
    path: str, cpv: str, chost: str, content: bytes, build_id: str | None = None
) -> str:
    return package_module.asset_name(
        path, cpv, chost, hashlib.sha256(content).hexdigest(), build_id
    )


class FakeClient:
    repository = "owner/repo"

    def __init__(self, previous: str | None = None) -> None:
        self.contents: str | None = previous
        self.releases: dict[str, Release] = {}
        self.assets: dict[int, list[Asset]] = {}
        self.next_id = 1
        self.deleted_releases = []
        self.deleted_refs = []
        self.messages = []
        self.puts = 0
        self.checks = []
        self.check_branches = []
        self.put_branches = []
        self.release_branches = []

    def check(
        self, write: bool = True, branch: str = github.BINREPO_BRANCH
    ) -> github.CheckResult:
        self.checks.append(write)
        self.check_branches.append(branch)
        return {
            "private": True,
            "default_branch": "main",
            "access": "write" if write else "read",
            "initialized": True,
        }

    def get_ref(self, ref: str) -> github.GitRef:
        del ref
        return {"object": {"sha": "root"}}

    def delete_ref(self, ref: str) -> None:
        self.deleted_refs.append(ref)

    def get_content(self, path: str, ref: str) -> Content | None:
        del path, ref
        if self.contents is None:
            return None
        return {
            "content": base64.b64encode(self.contents.encode()).decode(),
            "sha": "old",
        }

    def content_bytes(self, content: Content) -> bytes:
        return base64.b64decode(content["content"])

    def put_content(
        self,
        path: str,
        branch: str,
        content: bytes,
        message: str,
        sha: str | None = None,
    ) -> github.ContentUpdate:
        del path, sha
        self.messages.append(message)
        self.puts += 1
        self.put_branches.append(branch)
        self.contents = content.decode()
        return {"content": {"sha": "new"}}

    def initialize_repository(
        self, default_branch: str, branch: str = github.BINREPO_BRANCH
    ) -> github.GitRef:
        del default_branch, branch
        return {"object": {"sha": "root"}}

    def download_asset(self, asset_id: int, destination: Path) -> None:
        del asset_id, destination
        raise AssertionError

    def get_release(self, tag: str) -> Release | None:
        return self.releases.get(tag)

    def create_release(self, tag: str, branch: str) -> Release:
        self.release_branches.append(branch)
        release: Release = {
            "id": self.next_id,
            "tag_name": tag,
            "name": tag,
            "body": "",
            "upload_url": "unused",
        }
        self.next_id += 1
        self.releases[tag] = release
        self.assets[release["id"]] = []
        return release

    def list_assets(self, release_id: int) -> list[Asset]:
        return list(self.assets[release_id])

    def upload_asset(self, release_id: int, path: Path, name: str) -> Asset:
        asset: Asset = {"id": self.next_id, "name": name, "size": path.stat().st_size}
        self.next_id += 1
        self.assets[release_id].append(asset)
        return asset

    def delete_asset(self, asset_id: int) -> None:
        for assets in self.assets.values():
            assets[:] = [asset for asset in assets if asset["id"] != asset_id]

    def delete_release(self, release_id: int) -> None:
        self.deleted_releases.append(release_id)
        self.assets.pop(release_id, None)
        for tag, release in list(self.releases.items()):
            if release["id"] == release_id:
                del self.releases[tag]


def write_pkgdir(tmp_path: Path, packages: str, files: Mapping[str, bytes]) -> None:
    (tmp_path / "Packages").write_text(packages, encoding="utf-8")
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_layout_uses_shards_and_readable_asset_names() -> None:
    packages = [
        "acct-group/android-0-r2.gpkg.tar",
        "cat/pkg/pkg-1-1.gpkg.tar",
        "cat/pkg-1.tbz2",
    ]
    local = make_packages(*packages)
    entries = package_module.parse_packages(local)
    published = {}
    for asset_id, (path, metadata) in enumerate(entries.items(), 9):
        name = package_module.asset_name(
            path, metadata["CPV"], metadata["CHOST"], "a" * 64
        )
        assert name.startswith(f"host__{metadata['CPV'].replace('/', '__')}")
        published[path] = (f"binrepo/0/{name}", 1, asset_id)
    remote = package_module._push_package_paths(local, published)
    assert (
        package_module.parse_packages(package_module._restore_package_paths(remote))
        == entries
    )
    assert (
        package_module.release_coordinates(
            next(iter(published.values()))[0], "binrepo"
        )[0]
        == "binrepo/0"
    )
    assert f"{package_module.LOCAL_PATH_FIELD}: {packages[0]}" in remote
    assert f"{package_module.RELEASE_ID_FIELD}: 1" in remote
    assert (
        f"{package_module.ASSET_ID_FIELD}-9-PATH: "
        f"{next(iter(published.values()))[0]}" in remote
    )
    text = package_module.with_remote_uri(local, "owner/repo")
    assert "URI: https://github.com/owner/repo/releases/download" in text
    assert f"PATH: {packages[1]}" in text


def test_remote_path_must_match_cpv() -> None:
    with pytest.raises(ValueError, match="CPV does not match package PATH"):
        package_module._validate_release_package(
            "cat/pkg/pkg-1.gpkg.tar", "cat/other-1"
        )


def test_remote_local_path_must_be_safe() -> None:
    package = "cat/pkg/pkg-1.gpkg.tar"
    remote = make_packages(package).replace(
        f"PATH: {package}",
        f"PATH: {package}\n{package_module.LOCAL_PATH_FIELD}: ../escape",
    )

    with pytest.raises(ValueError, match="unsafe package PATH"):
        package_module._restore_package_paths(remote)


@pytest.mark.parametrize(
    "name", ("with/slash", "with space", "", ".hidden", "two..dots")
)
def test_unsafe_chosts_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid CHOST"):
        package_module.validate_chost(name)


@pytest.mark.parametrize(
    "name", ("", "/binrepo", "binrepo/", "with space", "two..dots", "main.lock")
)
def test_unsafe_branches_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid binrepo branch"):
        package_module.validate_branch(name)


def test_branch_namespaces_packages_and_tags(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(package), {package: b"x"})
    client = FakeClient()

    push.push(client, tmp_path, "testing")

    assert list(client.releases) == ["testing/0"]
    assert "PATH: testing/0/host__cat__pkg-1__" in (client.contents or "")
    assert client.put_branches == ["testing"]


@pytest.mark.parametrize("path", ("../escape", "/absolute/file", "file"))
def test_unsafe_package_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        package_module.validate_package_path(path)


@pytest.mark.parametrize(
    ("packages", "message"),
    (
        ("VERSION: 0\n", "missing PACKAGES"),
        ("PACKAGES: nope\n", "invalid PACKAGES"),
        ("PACKAGES: 1\n\nCPV: cat/pkg-1\nSIZE: 1\n", "missing PATH"),
        ("PACKAGES: 2\n\nCPV: cat/pkg-1\nPATH: cat/pkg/file\nSIZE: 1\n", "declares 2"),
    ),
)
def test_malformed_packages_are_rejected(packages: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        package_module.parse_packages(packages)


def test_initial_push_and_noop(tmp_path: Path) -> None:
    package = "acct-group/android-0-r2.gpkg.tar"
    content = b"package"
    packages = make_packages(package, sizes={package: len(content)}).replace(
        "CPV: acct-group/android-0-r2",
        "CPV: acct-group/android-0-r2\nDESC: Package description",
    )
    write_pkgdir(tmp_path, packages, {package: content})
    client = FakeClient()

    assert push.push(client, tmp_path) == {"uploaded": 1, "removed": 0, "unchanged": 0}
    assert push.push(client, tmp_path) == {"uploaded": 0, "removed": 0, "unchanged": 1}
    assert client.puts == 1
    assert client.messages == ["Add acct-group/android-0-r2"]
    assert client.checks == [True, True]
    assert client.put_branches == ["binrepo"]
    assert client.release_branches == ["binrepo"]
    contents = client.contents
    assert contents is not None
    assert "URI: https://github.com/owner/repo/releases/download" in contents
    name = expected_asset_name(package, "acct-group/android-0-r2", "host", content)
    assert f"PATH: binrepo/0/{name}" in contents
    assert f"{package_module.LOCAL_PATH_FIELD}: {package}" in contents
    assert client.releases["binrepo/0"] == {
        "id": 1,
        "tag_name": "binrepo/0",
        "name": "binrepo/0",
        "body": "",
        "upload_url": "unused",
    }


def test_timestamp_only_change_is_noop(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1.gpkg.tar"
    content = b"x"
    packages = (
        make_packages(package)
        .replace("PACKAGES: 1", "PACKAGES: 1\nTIMESTAMP: 1")
        .replace("SIZE: 1", "MTIME: 1\nSIZE: 1")
    )
    write_pkgdir(tmp_path, packages, {package: content})
    client = FakeClient()
    push.push(client, tmp_path)
    assets = list(client.assets[client.releases["binrepo/0"]["id"]])
    index = client.contents
    write_pkgdir(
        tmp_path,
        packages.replace("TIMESTAMP: 1", "TIMESTAMP: 2").replace(
            "MTIME: 1", "MTIME: 2"
        ),
        {package: content},
    )

    assert push.push(client, tmp_path) == {"uploaded": 0, "removed": 0, "unchanged": 1}
    assert client.puts == 1
    assert client.assets[client.releases["binrepo/0"]["id"]] == assets
    assert client.contents == index


def test_push_aggregates_multiple_chosts_on_binrepo_branch(tmp_path: Path) -> None:
    packages = [
        "x86_64-pc-linux-gnu/cat/pkg/pkg-1.gpkg.tar",
        "aarch64-unknown-linux-gnu/cat/pkg/pkg-1.gpkg.tar",
    ]
    index = """PACKAGES: 2

CHOST: x86_64-pc-linux-gnu
CPV: cat/pkg-1
PATH: x86_64-pc-linux-gnu/cat/pkg/pkg-1.gpkg.tar
SIZE: 1

CHOST: aarch64-unknown-linux-gnu
CPV: cat/pkg-1
PATH: aarch64-unknown-linux-gnu/cat/pkg/pkg-1.gpkg.tar
SIZE: 1

"""
    write_pkgdir(tmp_path, index, dict.fromkeys(packages, b"x"))
    client = FakeClient()

    push.push(client, tmp_path)

    remote_entries = package_module.parse_packages(client.contents or "")
    assert client.put_branches == ["binrepo"]
    assert list(client.releases) == ["binrepo/0"]
    assert len(remote_entries) == 2
    assert all(path.startswith("binrepo/0/") for path in remote_entries)
    assert {metadata["CHOST"] for metadata in remote_entries.values()} == {
        "x86_64-pc-linux-gnu",
        "aarch64-unknown-linux-gnu",
    }


def test_push_reports_each_upload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = ["cat/one/one-1.gpkg.tar", "cat/two/two-1.gpkg.tar"]
    write_pkgdir(
        tmp_path,
        make_packages(*packages, sizes=dict.fromkeys(packages, 1)),
        dict.fromkeys(packages, b"x"),
    )

    push.push(FakeClient(), tmp_path)

    assert capsys.readouterr().err.splitlines() == [
        "Uploading cat/one/one-1.gpkg.tar",
        "Uploading cat/two/two-1.gpkg.tar",
    ]


def test_push_reports_missing_packages_index(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Packages index is missing"):
        push.push(FakeClient(), tmp_path)


def test_content_addressed_names_avoid_local_path_collisions(tmp_path: Path) -> None:
    packages = ["cat/pkg-1.gpkg.tar", "cat/pkg/pkg-1.gpkg.tar"]
    index = """PACKAGES: 2
CHOST: host

CPV: cat/pkg-1
PATH: cat/pkg-1.gpkg.tar
SIZE: 1

CPV: cat/pkg-1
PATH: cat/pkg/pkg-1.gpkg.tar
SIZE: 1

"""
    write_pkgdir(tmp_path, index, dict.fromkeys(packages, b"x"))
    client = FakeClient()

    push.push(client, tmp_path)

    assert len(package_module.parse_packages(client.contents or "")) == 2
    assert len({asset["name"] for asset in client.assets[1]}) == 2
    assert list(client.releases) == ["binrepo/0"]


def test_multi_instance_build_ids_avoid_asset_path_collisions(tmp_path: Path) -> None:
    packages = ["cat/pkg/pkg-1-1.gpkg.tar", "cat/pkg/pkg-1-2.gpkg.tar"]
    index = """PACKAGES: 2
CHOST: host

CPV: cat/pkg-1
BUILD_ID: 1
PATH: cat/pkg/pkg-1-1.gpkg.tar
SIZE: 1

CPV: cat/pkg-1
BUILD_ID: 2
PATH: cat/pkg/pkg-1-2.gpkg.tar
SIZE: 1

"""
    write_pkgdir(tmp_path, index, dict.fromkeys(packages, b"x"))
    client = FakeClient()

    push.push(client, tmp_path)

    remote_entries = package_module.parse_packages(client.contents or "")
    assert set(remote_entries) == {
        f"binrepo/0/{expected_asset_name(packages[0], 'cat/pkg-1', 'host', b'x', '1')}",
        f"binrepo/0/{expected_asset_name(packages[1], 'cat/pkg-1', 'host', b'x', '2')}",
    }


def test_mixed_depth_paths_share_package_release(tmp_path: Path) -> None:
    packages = ["cat/pkg-2.tbz2", "cat/pkg/pkg-1.gpkg.tar"]
    contents = dict.fromkeys(packages, b"x")
    write_pkgdir(
        tmp_path, make_packages(*packages, sizes=dict.fromkeys(packages, 1)), contents
    )
    client = FakeClient()

    push.push(client, tmp_path)

    assert list(client.releases) == ["binrepo/0"]
    release = client.releases["binrepo/0"]
    names = {asset["name"] for asset in client.assets[release["id"]]}
    assert len(names) == 2
    assert any(name.endswith(".gpkg.tar") for name in names)
    assert any(name.endswith(".tbz2") for name in names)


def test_push_rolls_over_full_shards_without_listing_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(push, "RELEASE_ASSET_LIMIT", 2)
    packages = [
        "cat/one/one-1.gpkg.tar",
        "cat/two/two-1.gpkg.tar",
        "cat/three/three-1.tbz2",
    ]
    contents = {path: str(index).encode() for index, path in enumerate(packages, 1)}
    write_pkgdir(
        tmp_path,
        make_packages(
            *packages, sizes={path: len(content) for path, content in contents.items()}
        ),
        contents,
    )
    client = FakeClient()
    list_assets = Mock(wraps=client.list_assets)
    object.__setattr__(client, "list_assets", list_assets)

    push.push(client, tmp_path)

    assert list(client.releases) == ["binrepo/0", "binrepo/1"]
    assert [
        len(client.assets[release["id"]]) for release in client.releases.values()
    ] == [2, 1]
    assert list_assets.call_count == 0
    assert client.release_branches == ["binrepo", "binrepo"]


def test_uncertain_release_creation_is_reconciled(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(package), {package: b"x"})
    client = FakeClient()
    create_release = client.create_release

    def create_then_fail(tag: str, branch: str) -> Never:
        create_release(tag, branch)
        raise github.GitHubError("lost release response")  # noqa: TRY003

    object.__setattr__(client, "create_release", create_then_fail)

    assert push.push(client, tmp_path)["uploaded"] == 1
    assert list(client.releases) == ["binrepo/0"]


def test_nonempty_unindexed_release_reports_manual_cleanup(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(package), {package: b"x"})
    client = FakeClient()
    release = client.create_release("binrepo/0", "binrepo")
    client.assets[release["id"]].append({"id": 2, "name": "orphan", "size": 1})
    object.__setattr__(
        client, "create_release", Mock(side_effect=github.GitHubError("already exists"))
    )

    with pytest.raises(github.GitHubError) as caught:
        push.push(client, tmp_path)
    assert str(caught.value) == snapshot(
        "release binrepo/0 contains assets not recorded in Packages; delete it and "
        "its tag with `gh release delete binrepo/0 --cleanup-tag --repo owner/repo`, "
        "then retry"
    )


def test_removed_package_prunes_empty_release(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    previous = make_remote_packages(package)
    client = FakeClient(previous)
    remote_path, metadata = remote_entry(previous, package)
    tag, name = package_module.release_coordinates(remote_path, "binrepo")
    release = client.create_release(tag, "main")
    client.assets[release["id"]].append(
        {"id": remote_ids(previous, metadata)[1], "name": name, "size": 1}
    )
    write_pkgdir(tmp_path, make_packages(), {})

    result = push.push(client, tmp_path)

    assert result == {"uploaded": 0, "removed": 1, "unchanged": 0}
    assert client.deleted_releases == [release["id"]]
    assert client.deleted_refs == [f"tags/{tag}"]


def test_changed_package_replaces_asset(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    previous = make_remote_packages(package)
    client = FakeClient(previous)
    remote_path, metadata = remote_entry(previous, package)
    tag, old_name = package_module.release_coordinates(remote_path, "binrepo")
    release = client.create_release(tag, "main")
    client.assets[release["id"]].append(
        {"id": remote_ids(previous, metadata)[1], "name": old_name, "size": 3}
    )
    content = b"new package"
    changed = make_packages(package, sizes={package: len(content)})
    write_pkgdir(tmp_path, changed, {package: content})

    push.push(client, tmp_path)

    assert [asset["name"] for asset in client.assets[release["id"]]] == [
        expected_asset_name(package, "cat/pkg-1", "host", content)
    ]
    assert client.assets[release["id"]][0]["size"] == len(b"new package")


def test_changed_package_prunes_previous_remote_coordinate(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    previous = make_remote_packages(package, chost="old-host")
    client = FakeClient(previous)
    remote_path, metadata = remote_entry(previous, package)
    old_tag, old_name = package_module.release_coordinates(remote_path, "binrepo")
    old_release = client.create_release(old_tag, "main")
    client.assets[old_release["id"]].append(
        {"id": remote_ids(previous, metadata)[1], "name": old_name, "size": 1}
    )
    write_pkgdir(tmp_path, make_packages(package, chost="new-host"), {package: b"x"})

    push.push(client, tmp_path)

    assert sorted(client.releases) == ["binrepo/0"]
    assert client.deleted_releases == []
    assert client.deleted_refs == []
    assert (
        "new-host__cat__pkg-1__" in next(iter(client.assets[old_release["id"]]))["name"]
    )


def test_metadata_only_change_reuses_content_addressed_asset(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    content = b"x"
    client = FakeClient()
    write_pkgdir(
        tmp_path, make_packages(package, sizes={package: 1}), {package: content}
    )
    push.push(client, tmp_path)
    assets = list(client.assets[client.releases["binrepo/0"]["id"]])
    write_pkgdir(
        tmp_path,
        make_packages(package, sizes={package: 1}).replace(
            "SIZE: 1", "DESC: metadata changed\nSIZE: 1"
        ),
        {package: content},
    )

    assert push.push(client, tmp_path)["uploaded"] == 0

    assert client.puts == 2
    assert "DESC: metadata changed" in (client.contents or "")
    assert client.assets[client.releases["binrepo/0"]["id"]] == assets


def test_index_failure_restores_replaced_asset(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    previous = make_remote_packages(package)
    client = FakeClient(previous)
    remote_path, metadata = remote_entry(previous, package)
    tag, old_name = package_module.release_coordinates(remote_path, "binrepo")
    release = client.create_release(tag, "main")
    client.assets[release["id"]].append(
        {"id": remote_ids(previous, metadata)[1], "name": old_name, "size": 3}
    )
    content = b"new package"
    changed = make_packages(package, sizes={package: len(content)})
    write_pkgdir(tmp_path, changed, {package: content})

    def fail_put(
        path: str, branch: str, content: bytes, message: str, sha: str | None = None
    ) -> Never:
        del path, branch, content, message, sha
        raise github.GitHubError("conflict")

    object.__setattr__(client, "put_content", fail_put)
    with pytest.raises(github.GitHubError, match="conflict"):
        push.push(client, tmp_path)

    assert client.assets[release["id"]] == [{"id": 9, "name": old_name, "size": 3}]


def test_index_failure_removes_created_release(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    content = b"new package"
    write_pkgdir(
        tmp_path,
        make_packages(package, sizes={package: len(content)}),
        {package: content},
    )
    client = FakeClient()
    object.__setattr__(
        client, "put_content", Mock(side_effect=github.GitHubError("conflict"))
    )

    with pytest.raises(github.GitHubError, match="conflict"):
        push.push(client, tmp_path)

    assert client.releases == {}
    assert client.deleted_refs == ["tags/binrepo/0"]


def test_applied_index_update_is_reconciled_before_cleanup(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    previous = make_remote_packages(package)
    client = FakeClient(previous)
    remote_path, metadata = remote_entry(previous, package)
    tag, old_name = package_module.release_coordinates(remote_path, "binrepo")
    release = client.create_release(tag, "main")
    client.assets[release["id"]].append(
        {"id": remote_ids(previous, metadata)[1], "name": old_name, "size": 3}
    )
    content = b"new package"
    changed = make_packages(package, sizes={package: len(content)})
    write_pkgdir(tmp_path, changed, {package: content})
    put_content = client.put_content

    def apply_then_fail(
        path: str, branch: str, content: bytes, message: str, sha: str | None = None
    ) -> Never:
        put_content(path, branch, content, message, sha)
        raise github.GitHubError("response lost")  # noqa: TRY003

    object.__setattr__(client, "put_content", apply_then_fail)

    assert push.push(client, tmp_path)["uploaded"] == 1
    assert client.assets[release["id"]] == [
        {
            "id": 2,
            "name": expected_asset_name(package, "cat/pkg-1", "host", content),
            "size": len(content),
        }
    ]
    contents = client.contents
    assert contents is not None
    assert package_module.CLEANUP_FIELD not in contents


def test_upload_reconciliation_failure_preserves_indexed_asset(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    previous = make_remote_packages(package)
    client = FakeClient(previous)
    remote_path, metadata = remote_entry(previous, package)
    tag, old_name = package_module.release_coordinates(remote_path, "binrepo")
    release = client.create_release(tag, "main")
    client.assets[release["id"]].append(
        {"id": remote_ids(previous, metadata)[1], "name": old_name, "size": 3}
    )
    content = b"new package"
    write_pkgdir(
        tmp_path,
        make_packages(package, sizes={package: len(content)}),
        {package: content},
    )

    def fail_upload(release_id: int, path: Path, name: str) -> Never:
        del release_id, path, name
        raise github.GitHubError("upload failed")  # noqa: TRY003

    object.__setattr__(client, "upload_asset", fail_upload)
    object.__setattr__(
        client,
        "list_assets",
        Mock(side_effect=github.GitHubError("reconciliation failed")),
    )

    with pytest.raises(github.GitHubError, match="reconciliation failed"):
        push.push(client, tmp_path)

    assert client.assets[release["id"]] == [{"id": 9, "name": old_name, "size": 3}]


def test_post_commit_cleanup_is_retried(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    previous = make_remote_packages(package)
    client = FakeClient(previous)
    remote_path, metadata = remote_entry(previous, package)
    tag, old_name = package_module.release_coordinates(remote_path, "binrepo")
    release = client.create_release(tag, "main")
    client.assets[release["id"]].append(
        {"id": remote_ids(previous, metadata)[1], "name": old_name, "size": 3}
    )
    content = b"new package"
    changed = make_packages(package, sizes={package: len(content)})
    write_pkgdir(tmp_path, changed, {package: content})
    delete_asset = client.delete_asset
    failed = False

    def fail_once(asset_id: int) -> None:
        nonlocal failed
        if not failed and asset_id == 9:
            failed = True
            raise github.GitHubError("cleanup failed")  # noqa: TRY003
        delete_asset(asset_id)

    object.__setattr__(client, "delete_asset", fail_once)

    with pytest.raises(github.GitHubError, match="cleanup failed"):
        push.push(client, tmp_path)

    contents = client.contents
    assert contents is not None
    assert package_module.CLEANUP_FIELD in contents
    assert push.push(client, tmp_path) == {"uploaded": 0, "removed": 0, "unchanged": 1}
    assert [asset["name"] for asset in client.assets[release["id"]]] == [
        expected_asset_name(package, "cat/pkg-1", "host", content)
    ]
    contents = client.contents
    assert contents is not None
    assert package_module.CLEANUP_FIELD not in contents


def test_removed_package_cleanup_is_retried(tmp_path: Path) -> None:
    packages = ["cat/one/pkg-1.gpkg.tar", "cat/two/pkg-2.gpkg.tar"]
    previous = make_remote_packages(*packages)
    client = FakeClient(previous)
    remote_entries = package_module.parse_packages(previous)
    tag = package_module.release_coordinates(next(iter(remote_entries)), "binrepo")[0]
    release = client.create_release(tag, "main")
    for remote_path, metadata in remote_entries.items():
        client.assets[release["id"]].append(
            {
                "id": remote_ids(previous, metadata)[1],
                "name": Path(remote_path).name,
                "size": 1,
            }
        )
    write_pkgdir(tmp_path, make_packages(), {})
    delete_ref = client.delete_ref
    failed = False

    def fail_once(ref: str) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise github.GitHubError("cleanup failed")  # noqa: TRY003
        delete_ref(ref)

    object.__setattr__(client, "delete_ref", fail_once)

    with pytest.raises(github.GitHubError, match="cleanup failed"):
        push.push(client, tmp_path)

    contents = client.contents
    assert contents is not None
    assert package_module.CLEANUP_FIELD in contents
    assert push.push(client, tmp_path) == {"uploaded": 0, "removed": 0, "unchanged": 0}
    assert client.releases == {}
    contents = client.contents
    assert contents is not None
    assert package_module.CLEANUP_FIELD not in contents


def test_cleanup_is_validated_before_deleting_assets() -> None:
    client = Mock()

    with pytest.raises(ValueError, match="does not match binrepo branch"):
        push._delete_cleanup(client, "binrepo", {(1, 2, "other/0")}, set(), set())

    client.delete_asset.assert_not_called()


def test_size_mismatch_is_rejected_before_push(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(package), {package: b"wrong size"})
    client = FakeClient()

    with pytest.raises(ValueError, match="package size mismatch"):
        push.push(client, tmp_path)

    assert client.releases == {}
    assert client.puts == 0


def test_token_file_must_be_private(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    assert cli.read_token(token_file) == "secret"
    token_file.chmod(0o644)
    with pytest.raises(ValueError, match="group or others"):
        cli.read_token(token_file)


def test_cli_uses_global_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = tmp_path / "producer.conf"
    config.write_text(
        "# comments may contain = signs\n"
        "repository = 'owner/repo'  # one required setting\n"
        "branch = testing\n",
        encoding="utf-8",
    )
    client = Mock()
    client.check.return_value = {
        "private": True,
        "default_branch": "main",
        "access": "read",
    }
    make_client = Mock(return_value=client)
    monkeypatch.setattr(cli, "CONFIG_PATH", config)
    monkeypatch.setattr(cli, "TOKEN_PATH", token_file)
    monkeypatch.setattr(cli, "GitHubClient", make_client)

    assert cli.main(["check", "--read-only"]) == 0

    make_client.assert_called_once_with("owner/repo", "secret")
    client.check.assert_called_once_with(write=False, branch="testing")
    assert capsys.readouterr().out == snapshot(
        "repository=owner/repo access=read private=true default_branch=main\n"
    )


def test_config_rejects_unknown_key(tmp_path: Path) -> None:
    config = tmp_path / "producer.conf"
    config.write_text("unknown = value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Key validation failed at line: 1"):
        cli.read_config(config)


def test_pull_cli_infers_repository_from_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    make_client = Mock(return_value=Mock())
    pull = Mock()
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "missing.conf")
    monkeypatch.setattr(cli, "TOKEN_PATH", token_file)
    monkeypatch.setattr(cli, "GitHubClient", make_client)
    monkeypatch.setattr(cli, "pull", pull)
    uri = "https://raw.githubusercontent.com/owner/repo/host/Packages"

    assert cli.main(["pull", uri, str(tmp_path / "Packages")]) == 0

    make_client.assert_called_once_with("owner/repo", "secret")
    pull.assert_called_once_with(
        make_client.return_value, uri, str(tmp_path / "Packages"), None
    )


def test_pull_cli_without_arguments_syncs_pkgdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    make_client = Mock(return_value=client)
    pull_locked = Mock()
    monkeypatch.setattr(cli, "config", lambda: {"PKGDIR": "/binpkgs"})
    monkeypatch.setattr(cli, "read_token", Mock(return_value="secret"))
    monkeypatch.setattr(cli, "GitHubClient", make_client)
    monkeypatch.setattr(cli, "pull_locked", pull_locked)

    assert (
        cli.main(["pull", "--repository", "owner/repo", "--token-file", "token"]) == 0
    )

    pull_locked.assert_called_once_with(client, "/binpkgs", "binrepo")


@pytest.mark.parametrize("name", ("Packages", "Packages.gz"))
def test_pull_cli_returns_empty_index_for_unreadable_token(
    name: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / name
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "missing.conf")
    monkeypatch.setattr(cli, "read_token", Mock(side_effect=PermissionError))
    monkeypatch.setattr(getbinpkg.time, "time", lambda: 123)

    assert (
        cli.main(
            [
                "pull",
                f"https://raw.githubusercontent.com/owner/repo/host/{name}",
                str(destination),
            ]
        )
        == 0
    )

    data = destination.read_bytes()
    if name.endswith(".gz"):
        data = gzip.decompress(data)
    assert data.decode() == snapshot("PACKAGES: 0\nTIMESTAMP: 123\nVERSION: 0\n\n")
    assert capsys.readouterr().err == ""


def test_non_pull_cli_rejects_unreadable_token(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "read_token", Mock(side_effect=PermissionError("denied")))

    assert (
        cli.main(["check", "--repository", "owner/repo", "--token-file", "token"]) == 1
    )

    assert capsys.readouterr().err == "portage-github-binrepo: denied\n"


def test_private_index_pull_and_gzip(tmp_path: Path) -> None:
    packages = make_packages("cat/pkg/file")
    client = FakeClient(packages)
    destination = tmp_path / "Packages.gz"

    pull.pull(
        client,
        "https://raw.githubusercontent.com/owner/repo/host/Packages.gz",
        destination,
    )

    assert gzip.decompress(destination.read_bytes()).decode() == packages


def test_private_index_pull_preserves_slashes_in_branch(tmp_path: Path) -> None:
    client = Mock(repository="owner/repo")
    client.get_ref.return_value = {"object": {"sha": "root"}}
    client.get_content.return_value = {"sha": "index"}
    client.content_bytes.return_value = b"PACKAGES: 0\n\n"
    destination = tmp_path / "Packages"

    pull.pull(
        client,
        "https://raw.githubusercontent.com/owner/repo/release/current/Packages",
        destination,
    )

    client.get_ref.assert_called_once_with("heads/release/current")
    client.get_content.assert_called_once_with("Packages", "release/current")
    assert destination.read_bytes() == b"PACKAGES: 0\n\n"


def test_pull_all_replaces_local_cache_with_remote(tmp_path: Path) -> None:
    package = "cat/pkg/pkg-1.gpkg.tar"
    content = b"remote package"
    remote = make_remote_packages(package, sizes={package: len(content)})
    client = Mock(repository="owner/repo")
    client.check.return_value = {"initialized": True}
    client.get_content.return_value = {"sha": "index"}
    client.content_bytes.return_value = remote.encode()
    client.get_release.return_value = {"id": 1}
    client.list_assets.return_value = [{"id": 2, "name": "pkg-1.gpkg.tar"}]

    def download_asset(_asset_id: int, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    client.download_asset.side_effect = download_asset
    old_package = "cat/old/old-1.gpkg.tar"
    write_pkgdir(
        tmp_path,
        make_packages(old_package, sizes={old_package: 3}),
        {old_package: b"old"},
    )
    (tmp_path / "Packages.gz").write_bytes(b"stale")

    pull.pull_locked(client, tmp_path)

    entries = package_module.parse_packages(
        (tmp_path / "Packages").read_text(encoding="utf-8")
    )
    assert list(entries) == [package]
    assert (tmp_path / package).read_bytes() == content
    assert not (tmp_path / old_package).exists()
    assert not (tmp_path / "Packages.gz").exists()
    client.check.assert_called_once_with(write=False, branch="binrepo")
    client.get_content.assert_called_once_with("Packages", "binrepo")


def test_replace_cache_copies_across_filesystems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = tmp_path / "pkgdir"
    staging = tmp_path / "staging"
    package = staging / "cat/pkg/pkg-1.gpkg.tar"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"package")
    (staging / "Packages").write_bytes(b"index")
    pkgdir.mkdir()

    def cross_device_replace(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(Path, "replace", cross_device_replace)

    pull._replace_cache(pkgdir, staging)

    assert (pkgdir / "cat/pkg/pkg-1.gpkg.tar").read_bytes() == b"package"
    assert (pkgdir / "Packages").read_bytes() == b"index"
    assert not package.exists()


@pytest.mark.parametrize("repository", ("download/repo", "owner/download"))
def test_private_asset_pull_allows_download_in_repository(
    repository: str, tmp_path: Path
) -> None:
    client = Mock(repository=repository)
    package = "cat/pkg/pkg-1.gpkg.tar"
    packages = make_remote_packages(package)
    remote_path, _ = remote_entry(packages, package)
    cached = getbinpkg.PackageIndex(allowed_pkg_keys={"CHOST", "CPV", "PATH", "SIZE"})
    cached.read(StringIO(packages))
    cached.modified = False
    output = StringIO()
    cached.write(output)
    packages = output.getvalue()
    destination = tmp_path / "pkg-1.gpkg.tar"

    pull.pull(
        client,
        f"https://github.com/{repository}/releases/download/{remote_path}",
        destination,
        packages,
    )

    assert package_module.RELEASE_ID_FIELD not in packages
    client.download_asset.assert_called_once_with(
        package_module.asset_ids(packages)[remote_path], destination
    )


def test_cached_packages_path_matches_portage_cache_layout(tmp_path: Path) -> None:
    assert pull.cached_packages_path(
        "https://github.com/owner/repo/releases/download/release/current/3/asset.gpkg.tar",
        tmp_path,
    ) == (
        tmp_path
        / "var/cache/edb/binhost/raw.githubusercontent.com/owner/repo/release/current/Packages"
    )


def test_failed_write_preserves_destination(tmp_path: Path) -> None:
    destination = tmp_path / "Packages"
    destination.write_bytes(b"existing")

    def fail_after_first_chunk() -> Iterator[bytes]:
        yield b"partial"
        raise OSError("download failed")  # noqa: TRY003

    with pytest.raises(OSError, match="download failed"):
        github.write_stream(destination, fail_after_first_chunk())

    assert destination.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [destination]


def test_pull_uninitialized_binrepo_returns_empty_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Mock(repository="owner/repo")
    client.get_ref.return_value = None
    client.check.return_value = {"initialized": False}
    destination = tmp_path / "Packages"
    monkeypatch.setattr(getbinpkg.time, "time", lambda: 123)

    pull.pull(
        client,
        "https://raw.githubusercontent.com/owner/repo/host/Packages",
        destination,
    )

    assert destination.read_text(encoding="utf-8") == snapshot(
        "PACKAGES: 0\nTIMESTAMP: 123\nVERSION: 0\n\n"
    )
    assert capsys.readouterr().err == ""
    client.check.assert_called_once_with(write=False, branch="host")
    client.get_content.assert_not_called()


def test_pull_rejects_missing_branch_on_initialized_repository(tmp_path: Path) -> None:
    client = Mock(repository="owner/repo")
    client.get_ref.return_value = None
    client.check.return_value = {"initialized": True}

    with pytest.raises(github.GitHubError, match="branch not found: stale"):
        pull.pull(
            client,
            "https://raw.githubusercontent.com/owner/repo/stale/Packages",
            tmp_path / "Packages",
        )

    client.check.assert_called_once_with(write=False, branch="stale")
    client.get_content.assert_not_called()


def test_init_creates_private_repository() -> None:
    client = Mock()
    client.get_repository.return_value = None
    client.check.return_value = {
        "private": True,
        "default_branch": "main",
        "access": "write",
        "initialized": True,
    }
    result = init.init_repository(client)
    assert result["created"] is True
    client.create_repository.assert_called_once_with(private=True)


def test_init_accepts_empty_existing_repository() -> None:
    client = Mock()
    client.get_repository.return_value = {"name": "repo"}
    client.check.return_value = {
        "private": True,
        "default_branch": "main",
        "access": "write",
        "initialized": False,
    }

    result = init.init_repository(client)

    assert result["created"] is False
    assert result["initialized"] is False
    client.check.assert_called_once_with(write=True, branch="binrepo")
    client.initialize_repository.assert_not_called()


def test_init_and_check_cli_options() -> None:
    init = cli.make_parser().parse_args(
        ["init", "--repository", "owner/repo", "--token-file", "token", "--public"]
    )
    check = cli.make_parser().parse_args(
        ["check", "--repository", "owner/repo", "--token-file", "token", "--read-only"]
    )

    assert init.command == "init"
    assert init.public is True
    assert check.command == "check"
    assert check.read_only is True
    assert cli.make_parser().parse_args(["check"]).repository is None


def test_push_cli_uses_portage_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock()
    push_locked = Mock(return_value={"uploaded": 0, "removed": 0, "unchanged": 0})
    monkeypatch.setattr(cli, "config", lambda: {"PKGDIR": "/binpkgs", "CHOST": "host"})
    monkeypatch.setattr(cli, "read_token", Mock(return_value="secret"))
    monkeypatch.setattr(cli, "GitHubClient", Mock(return_value=client))
    monkeypatch.setattr(cli, "push_locked", push_locked)

    assert (
        cli.main(
            [
                "push",
                "--repository",
                "owner/repo",
                "--token-file",
                "token",
                "--branch",
                "testing",
            ]
        )
        == 0
    )
    push_locked.assert_called_once_with(client, "/binpkgs", "testing")


def test_push_cli_requires_portage_settings(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "config", dict)
    monkeypatch.setattr(cli, "read_token", Mock(return_value="secret"))
    monkeypatch.setattr(cli, "GitHubClient", Mock())

    assert (
        cli.main(["push", "--repository", "owner/repo", "--token-file", "token"]) == 1
    )
    assert capsys.readouterr().err == snapshot(
        "portage-github-binrepo: PKGDIR must be set in Portage configuration\n"
    )


def test_push_is_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    lock = object()

    def fake_push(client: github.PushAPI, pkgdir: Path, branch: str) -> dict[str, int]:
        calls.append((client, pkgdir, branch))
        return {"uploaded": 0, "removed": 0, "unchanged": 1}

    monkeypatch.setattr(push, "push", fake_push)
    lockfile = Mock(return_value=lock)
    unlockfile = Mock()
    monkeypatch.setattr(push, "lockfile", lockfile)
    monkeypatch.setattr(push, "unlockfile", unlockfile)
    client = FakeClient()

    result = push.push_locked(client, tmp_path)

    assert result == {"uploaded": 0, "removed": 0, "unchanged": 1}
    assert len(calls) == 1
    lockfile.assert_called_once_with(str(tmp_path / "Packages"), wantnewlockfile=True)
    unlockfile.assert_called_once_with(lock)
