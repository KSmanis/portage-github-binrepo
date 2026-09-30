from __future__ import annotations

import errno
import gzip
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from portage_github_binrepo import github
from portage_github_binrepo import package
from portage_github_binrepo import pull
from tests.test_binrepo import make_packages
from tests.test_binrepo import make_remote_packages
from tests.test_binrepo import write_pkgdir


@pytest.mark.parametrize(
    ("uri", "message"),
    (
        ("https://example.com/owner/repo/binrepo/Packages", "unsupported binrepo URI"),
        ("https://github.com/owner/repo", "unsupported binrepo URI"),
        (
            "https://github.com/other/repo/releases/download/binrepo/0/asset",
            "asset URI does not match",
        ),
        (
            "https://github.com/owner/repo/archive/download/binrepo/0/asset",
            "asset URI does not match",
        ),
        (
            "https://raw.githubusercontent.com/other/repo/binrepo/Packages",
            "index URI does not match",
        ),
        (
            "https://raw.githubusercontent.com/owner/repo/binrepo/README.md",
            "index URI does not match",
        ),
    ),
)
def test_pull_rejects_unsupported_or_mismatched_uris(
    uri: str, message: str, tmp_path: Path
) -> None:
    client = Mock(repository="owner/repo")
    destination = tmp_path / "destination"

    with pytest.raises(ValueError, match=message):
        pull.pull(client, uri, destination)

    assert not destination.exists()
    assert client.mock_calls == []


@pytest.mark.parametrize("packages", (None, "PACKAGES: 0\n\n"))
def test_asset_pull_requires_cached_entry(packages: str | None, tmp_path: Path) -> None:
    client = Mock(repository="owner/repo")

    with pytest.raises(
        github.GitHubError,
        match="cached Packages index is required"
        if packages is None
        else "not found in Packages",
    ):
        pull.pull(
            client,
            "https://github.com/owner/repo/releases/download/binrepo/0/asset",
            tmp_path / "asset",
            packages,
        )

    client.download_asset.assert_not_called()


def test_missing_remote_index_preserves_destination(tmp_path: Path) -> None:
    destination = tmp_path / "Packages"
    destination.write_bytes(b"existing")
    client = Mock(repository="owner/repo")
    client.get_content.return_value = None

    with pytest.raises(github.GitHubError, match="Packages was not found"):
        pull.pull(
            client,
            "https://raw.githubusercontent.com/owner/repo/binrepo/Packages",
            destination,
        )

    assert destination.read_bytes() == b"existing"


@pytest.mark.parametrize(
    "failed_asset",
    (
        pytest.param(None, id="success"),
        pytest.param(9, id="first-download-fails"),
        pytest.param(10, id="second-download-fails"),
    ),
)
def test_pull_locked_stages_downloads_before_replacing_cache(
    failed_asset: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = tmp_path / "pkgdir"
    pkgdir.mkdir()
    old_package = "cat/old/old-1.gpkg.tar"
    old_index = make_packages(old_package, sizes={old_package: 3})
    write_pkgdir(pkgdir, old_index, {old_package: b"old"})
    (pkgdir / "Packages.gz").write_bytes(b"old gzip index")
    paths = ("cat/one/one-1.gpkg.tar", "cat/two/two-2.gpkg.tar")
    remote = make_remote_packages(*paths)
    client = Mock(repository="owner/repo")
    client.check.return_value = {"initialized": True}
    client.get_content.return_value = {"sha": "index"}
    client.content_bytes.return_value = remote.encode()

    def download(asset_id: int, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * (asset_id - 8))
        if asset_id == failed_asset:
            raise github.GitHubError("download failed")  # noqa: TRY003

    client.download_asset.side_effect = download
    lock = object()
    lockfile = Mock(return_value=lock)
    unlockfile = Mock()
    monkeypatch.setattr(pull, "lockfile", lockfile)
    monkeypatch.setattr(pull, "unlockfile", unlockfile)
    expectation = (
        nullcontext()
        if failed_asset is None
        else pytest.raises(github.GitHubError, match="download failed")
    )

    with expectation:
        pull.pull_locked(client, pkgdir)

    if failed_asset is None:
        assert list(package.parse_packages((pkgdir / "Packages").read_text())) == list(
            paths
        )
        for index, path in enumerate(paths, 1):
            assert (pkgdir / path).read_bytes() == b"x" * index
        assert not (pkgdir / old_package).exists()
        assert not (pkgdir / "Packages.gz").exists()
    else:
        assert (pkgdir / "Packages").read_text(encoding="utf-8") == old_index
        assert (pkgdir / "Packages.gz").read_bytes() == b"old gzip index"
        assert (pkgdir / old_package).read_bytes() == b"old"
        assert all(not (pkgdir / path).exists() for path in paths)
    assert list(tmp_path.iterdir()) == [pkgdir]
    assert client.download_asset.call_count == (1 if failed_asset == 9 else 2)
    client.check.assert_called_once_with(write=False, branch="binrepo")
    client.get_content.assert_called_once_with("Packages", "binrepo")
    lockfile.assert_called_once_with(str(pkgdir / "Packages"), wantnewlockfile=True)
    unlockfile.assert_called_once_with(lock)


@pytest.mark.parametrize("initialized", (False, True))
def test_pull_all_handles_missing_remote_index(
    initialized: bool, tmp_path: Path
) -> None:
    old_index = make_packages("cat/old/old-1.gpkg.tar")
    write_pkgdir(tmp_path, old_index, {"cat/old/old-1.gpkg.tar": b"old"})
    client = Mock(repository="owner/repo")
    client.check.return_value = {"initialized": initialized}
    client.get_content.return_value = None

    if initialized:
        with pytest.raises(github.GitHubError, match="Packages was not found"):
            pull.pull_all(client, tmp_path)
        assert (tmp_path / "Packages").read_text(encoding="utf-8") == old_index
        assert (tmp_path / "cat/old/old-1.gpkg.tar").read_bytes() == b"old"
    else:
        pull.pull_all(client, tmp_path)
        assert package.parse_packages((tmp_path / "Packages").read_text()) == {}
        assert list(tmp_path.iterdir()) == [tmp_path / "Packages"]

    client.download_asset.assert_not_called()


def test_cache_replacement_preserves_lock_and_unlinks_symlinks(tmp_path: Path) -> None:
    pkgdir = tmp_path / "pkgdir"
    pkgdir.mkdir()
    lock = pkgdir / ".Packages.portage_lockfile"
    lock.write_bytes(b"lock")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package").write_bytes(b"untouched")
    (pkgdir / "linked-directory").symlink_to(outside, target_is_directory=True)
    (pkgdir / "linked-package").symlink_to(outside / "package")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "Packages").write_bytes(b"new index")

    pull._replace_cache(pkgdir, staging)

    assert set(pkgdir.iterdir()) == {lock, pkgdir / "Packages"}
    assert lock.read_bytes() == b"lock"
    assert (pkgdir / "Packages").read_bytes() == b"new index"
    assert (outside / "package").read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "error",
    (
        pytest.param(errno.EXDEV, id="cross-device"),
        pytest.param(errno.EACCES, id="permission-denied"),
    ),
)
def test_cache_replacement_handles_move_errors(
    error: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir = tmp_path / "pkgdir"
    staging = tmp_path / "staging"
    pkgdir.mkdir()
    source = staging / "cat/pkg/pkg-1.gpkg.tar"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"package")
    (staging / "Packages").write_bytes(b"index")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError(error, "move failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    expectation = (
        nullcontext()
        if error == errno.EXDEV
        else pytest.raises(OSError, match="move failed")
    )

    with expectation:
        pull._replace_cache(pkgdir, staging)

    if error == errno.EXDEV:
        assert (pkgdir / "cat/pkg/pkg-1.gpkg.tar").read_bytes() == b"package"
        assert (pkgdir / "Packages").read_bytes() == b"index"
        assert not source.exists()
        assert not (staging / "Packages").exists()
    else:
        assert source.read_bytes() == b"package"
        assert (staging / "Packages").read_bytes() == b"index"
        assert not (pkgdir / "Packages").exists()


@pytest.mark.parametrize(
    "uri", ("https://example.com/owner/repo", "https://github.com/owner")
)
def test_repository_inference_rejects_unsupported_uri(uri: str) -> None:
    with pytest.raises(ValueError, match="unsupported binrepo URI"):
        pull.repository_from_uri(uri)


@pytest.mark.parametrize("name", ("Packages", "Packages.gz"))
@pytest.mark.parametrize(
    "branch",
    (
        pytest.param("host", id="simple-branch"),
        pytest.param("release/current", id="nested-branch"),
    ),
)
def test_private_index_pull_preserves_branch_and_compression(
    name: str, branch: str, tmp_path: Path
) -> None:
    packages = make_packages("cat/pkg/file")
    client = Mock(repository="owner/repo")
    client.get_ref.return_value = {"object": {"sha": "root"}}
    client.get_content.return_value = {"sha": "index"}
    client.content_bytes.return_value = packages.encode()
    destination = tmp_path / name

    pull.pull(
        client,
        f"https://raw.githubusercontent.com/owner/repo/{branch}/{name}",
        destination,
    )

    client.get_ref.assert_called_once_with(f"heads/{branch}")
    client.get_content.assert_called_once_with("Packages", branch)
    data = destination.read_bytes()
    if name.endswith(".gz"):
        data = gzip.decompress(data)
    assert data.decode() == packages
