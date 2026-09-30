from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from portage_github_binrepo import github
from portage_github_binrepo import package
from portage_github_binrepo import push
from tests.test_binrepo import FakeClient
from tests.test_binrepo import make_packages
from tests.test_binrepo import make_remote_packages
from tests.test_binrepo import write_pkgdir

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Never


def test_push_initializes_uninitialized_repository(tmp_path: Path) -> None:
    client = Mock(repository="owner/repo")
    client.check.return_value = {"initialized": False, "default_branch": "main"}
    client.get_content.return_value = None
    client.put_content.return_value = {"content": {"sha": "index"}}
    write_pkgdir(tmp_path, make_packages(), {})

    assert push.push(client, tmp_path, "testing") == {
        "uploaded": 0,
        "removed": 0,
        "unchanged": 0,
    }

    client.initialize_repository.assert_called_once_with("main", "testing")
    client.put_content.assert_called_once()
    assert client.put_content.call_args.args[:2] == ("Packages", "testing")


def test_uncertain_commit_preserves_assets_until_index_can_be_read(
    tmp_path: Path,
) -> None:
    path = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(path), {path: b"x"})
    client = FakeClient()
    put_content = client.put_content
    get_content = client.get_content

    def apply_then_fail(
        path: str, branch: str, content: bytes, message: str, sha: str | None = None
    ) -> Never:
        put_content(path, branch, content, message, sha)
        raise github.GitHubError("response lost")  # noqa: TRY003

    object.__setattr__(client, "put_content", apply_then_fail)
    object.__setattr__(
        client,
        "get_content",
        Mock(side_effect=[None, github.GitHubError("read failed")]),
    )

    with pytest.raises(github.GitHubError, match="read failed"):
        push.push(client, tmp_path)

    assert client.contents is not None
    assert len(client.releases) == 1
    assert len(client.assets[client.releases["binrepo/0"]["id"]]) == 1
    assert client.deleted_releases == []
    assert client.deleted_refs == []

    object.__setattr__(client, "put_content", put_content)
    object.__setattr__(client, "get_content", get_content)
    assert push.push(client, tmp_path) == {"uploaded": 0, "removed": 0, "unchanged": 1}


def test_failed_cleanup_marker_update_is_retried(tmp_path: Path) -> None:
    path = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(path), {path: b"x"})
    client = FakeClient()
    push.push(client, tmp_path)
    write_pkgdir(tmp_path, make_packages(), {})
    put_content = client.put_content

    def fail_cleanup_update(
        path: str, branch: str, content: bytes, message: str, sha: str | None = None
    ) -> github.ContentUpdate:
        if message == "Remove cleanup markers":
            raise github.GitHubError("cleanup update failed")  # noqa: TRY003
        return put_content(path, branch, content, message, sha)

    object.__setattr__(client, "put_content", fail_cleanup_update)

    with pytest.raises(github.GitHubError, match="cleanup update failed"):
        push.push(client, tmp_path)

    assert client.releases == {}
    assert package.CLEANUP_FIELD in (client.contents or "")
    object.__setattr__(client, "put_content", put_content)

    assert push.push(client, tmp_path) == {"uploaded": 0, "removed": 0, "unchanged": 0}
    assert package.CLEANUP_FIELD not in (client.contents or "")


def test_uncertain_upload_reuses_matching_asset(tmp_path: Path) -> None:
    path = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(path), {path: b"x"})
    client = FakeClient()
    upload_asset = client.upload_asset

    def apply_then_fail(release_id: int, path: Path, name: str) -> Never:
        upload_asset(release_id, path, name)
        raise github.GitHubError("response lost")  # noqa: TRY003

    object.__setattr__(client, "upload_asset", apply_then_fail)

    assert push.push(client, tmp_path) == {"uploaded": 1, "removed": 0, "unchanged": 0}
    assert len(client.assets[client.releases["binrepo/0"]["id"]]) == 1


@pytest.mark.parametrize("failure", ("upload", "name", "size", "release"))
def test_failed_publish_does_not_commit_index(failure: str, tmp_path: Path) -> None:
    path = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(tmp_path, make_packages(path), {path: b"x"})
    client = FakeClient()
    if failure == "release":
        object.__setattr__(
            client,
            "create_release",
            Mock(side_effect=github.GitHubError("release failed")),
        )
    elif failure == "upload":
        object.__setattr__(
            client,
            "upload_asset",
            Mock(side_effect=github.GitHubError("upload failed")),
        )
    else:
        upload_asset = client.upload_asset

        def invalid_metadata(release_id: int, path: Path, name: str) -> github.Asset:
            asset = upload_asset(release_id, path, name)
            if failure == "name":
                asset["name"] = "wrong-name"
            else:
                asset["size"] = 123
            return asset

        object.__setattr__(client, "upload_asset", invalid_metadata)

    with pytest.raises(
        github.GitHubError,
        match="failed"
        if failure in {"release", "upload"}
        else "invalid asset metadata",
    ):
        push.push(client, tmp_path)

    assert client.contents is None
    assert client.puts == 0
    assert client.releases == {}
    assert client.assets == {}


@pytest.mark.parametrize(
    ("cleanup", "active_assets", "message"),
    (
        ({(1, 9, "binrepo/0")}, {9}, "cleanup references active asset"),
        (
            {(1, 9, "binrepo/0"), (1, 10, "binrepo/1")},
            set(),
            "conflicting release metadata",
        ),
    ),
)
def test_invalid_cleanup_is_rejected_before_any_deletion(
    cleanup: set[tuple[int, int, str]], active_assets: set[int], message: str
) -> None:
    client = Mock()

    with pytest.raises(ValueError, match=message):
        push._delete_cleanup(client, "binrepo", cleanup, active_assets, set())

    assert client.mock_calls == []


@pytest.mark.parametrize(
    ("replacement", "message"),
    (
        ("", "missing PGB-LOCAL-PATH"),
        ("PGB-LOCAL-PATH: cat/pkg/pkg-1.gpkg.tar", "duplicate PATH"),
        ("PGB-RELEASE-ID: 2", "conflicting release metadata"),
    ),
)
def test_invalid_remote_metadata_is_rejected_before_upload(
    replacement: str, message: str, tmp_path: Path
) -> None:
    remote = make_remote_packages(
        "cat/pkg/pkg-1.gpkg.tar", "cat/other/other-2.gpkg.tar"
    )
    if replacement.startswith("PGB-RELEASE-ID"):
        remote = remote.replace("PGB-RELEASE-ID: 1", replacement, 1)
    else:
        remote = remote.replace(
            "PGB-LOCAL-PATH: cat/other/other-2.gpkg.tar", replacement
        )
    client = FakeClient(remote)
    write_pkgdir(tmp_path, make_packages(), {})

    with pytest.raises(ValueError, match=message):
        push.push(client, tmp_path)

    assert client.contents == remote
    assert client.releases == {}
    assert client.puts == 0
    assert client.deleted_refs == []


@pytest.mark.parametrize("size", ("", "nope", "-1"))
def test_invalid_local_size_is_rejected_before_publish(
    size: str, tmp_path: Path
) -> None:
    path = "cat/pkg/pkg-1.gpkg.tar"
    index = make_packages(path).replace("SIZE: 1", f"SIZE: {size}")
    write_pkgdir(tmp_path, index, {path: b"x"})
    client = FakeClient()

    with pytest.raises(
        ValueError, match="invalid SIZE" if size != "-1" else "size mismatch"
    ):
        push.push(client, tmp_path)

    assert client.releases == {}
    assert client.puts == 0


@pytest.mark.parametrize("kind", ("missing", "directory", "symlink"))
def test_local_package_must_be_file_inside_pkgdir(kind: str, tmp_path: Path) -> None:
    pkgdir = tmp_path / "pkgdir"
    pkgdir.mkdir()
    path = "cat/pkg/pkg-1.gpkg.tar"
    write_pkgdir(pkgdir, make_packages(path), {})
    source = pkgdir / path
    if kind != "missing":
        source.parent.mkdir(parents=True)
        if kind == "directory":
            source.mkdir()
        else:
            outside = tmp_path / "outside"
            outside.write_bytes(b"x")
            source.symlink_to(outside)
    client = FakeClient()

    with pytest.raises(ValueError, match="package file is missing"):
        push.push(client, pkgdir)

    assert client.releases == {}
    assert client.puts == 0


def test_failed_push_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = object()
    monkeypatch.setattr(push, "lockfile", Mock(return_value=lock))
    unlockfile = Mock()
    monkeypatch.setattr(push, "unlockfile", unlockfile)
    client = FakeClient()

    with pytest.raises(ValueError, match="Packages index is missing"):
        push.push_locked(client, tmp_path)

    unlockfile.assert_called_once_with(lock)
