from __future__ import annotations

import errno
import gzip
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from portage_github_binrepo import cli
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
    ("failed_asset", "fail_install"),
    (
        pytest.param(None, False, id="success"),
        pytest.param(9, False, id="first-download-fails"),
        pytest.param(10, False, id="second-download-fails"),
        pytest.param(None, True, id="installation-fails"),
    ),
)
def test_pull_locked_stages_downloads_before_replacing_cache(
    failed_asset: int | None,
    fail_install: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    if fail_install:
        replace = Path.replace

        def fail_replace(source: Path, destination: Path) -> Path:
            if source.name == "two-2.gpkg.tar":
                raise OSError(errno.EIO, "installation failed")
            return replace(source, destination)

        monkeypatch.setattr(Path, "replace", fail_replace)
    expectation = nullcontext()
    if fail_install:
        expectation = pytest.raises(OSError, match="installation failed")
    elif failed_asset is not None:
        expectation = pytest.raises(github.GitHubError, match="download failed")

    with expectation:
        pull.pull_locked(client, pkgdir)

    if failed_asset is None and not fail_install:
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


@pytest.fixture
def cache_replacement(tmp_path: Path) -> tuple[Path, Path]:
    pkgdir = tmp_path / "pkgdir"
    staging = tmp_path / "staging"
    (pkgdir / "cat/old").mkdir(parents=True)
    (pkgdir / "cat/old/old-1.gpkg.tar").write_bytes(b"old package")
    (pkgdir / "Packages").write_bytes(b"old index")
    (pkgdir / "Packages.portage_lockfile").write_bytes(b"lock")
    (pkgdir / "cat/pkg").symlink_to("old", target_is_directory=True)
    (staging / "cat/pkg").mkdir(parents=True)
    (staging / "cat/pkg/pkg-1.gpkg.tar").write_bytes(b"new package")
    (staging / "Packages").write_bytes(b"new index")
    return pkgdir, staging


@pytest.mark.parametrize("phase", ("backup", "install"))
@pytest.mark.parametrize("failure_number", (1, 2))
@pytest.mark.parametrize("cross_device", (False, True))
def test_replace_cache_restores_files_after_failure(
    cache_replacement: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure_number: int,
    cross_device: bool,
) -> None:
    pkgdir, staging = cache_replacement
    root_inode = pkgdir.stat().st_ino
    lock = pkgdir / "Packages.portage_lockfile"
    lock_inode = lock.stat().st_ino
    replace = Path.replace
    copy = pull.shutil.copy2
    operations = 0

    def should_fail(source: Path) -> bool:
        nonlocal operations
        selected = (
            source.is_relative_to(staging)
            if phase == "install"
            else (
                source.is_relative_to(pkgdir)
                and not any(".binrepo-backup-" in part for part in source.parts)
            )
        )
        if selected:
            operations += 1
            return operations == failure_number
        return False

    def failing_replace(source: Path, destination: Path) -> Path:
        if cross_device:
            raise OSError(errno.EXDEV, "cross-device move")
        if should_fail(source):
            raise OSError(errno.EIO, "replacement failed")
        return replace(source, destination)

    def failing_copy(
        source: Path, destination: Path, *, follow_symlinks: bool
    ) -> Path | str:
        if should_fail(source):
            destination.write_bytes(b"partial copy")
            raise OSError(errno.ENOSPC, "replacement failed")
        return copy(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "replace", failing_replace)
    if cross_device:
        monkeypatch.setattr(pull.shutil, "copy2", failing_copy)

    with pytest.raises(OSError, match="replacement failed"):
        pull._replace_cache(pkgdir, staging)

    assert (pkgdir / "Packages").read_bytes() == b"old index"
    assert (pkgdir / "cat/old/old-1.gpkg.tar").read_bytes() == b"old package"
    assert (pkgdir / "cat/pkg").is_symlink()
    assert (pkgdir / "cat/pkg").readlink() == Path("old")
    assert not (pkgdir / "cat/pkg/pkg-1.gpkg.tar").exists()
    assert not list(pkgdir.parent.glob(f".{pkgdir.name}.binrepo-backup-*"))
    assert pkgdir.stat().st_ino == root_inode
    assert lock.stat().st_ino == lock_inode
    assert lock.read_bytes() == b"lock"


@pytest.mark.parametrize("via_cli", (False, True))
def test_replace_cache_retains_backup_when_rollback_fails(
    cache_replacement: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    via_cli: bool,
) -> None:
    pkgdir, staging = cache_replacement
    replace = Path.replace

    def failing_replace(source: Path, destination: Path) -> Path:
        if source.is_relative_to(staging):
            raise OSError(errno.EIO, "installation failed")
        if any(".binrepo-backup-" in part for part in source.parts):
            raise OSError(errno.EIO, "rollback failed")
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", failing_replace)

    if via_cli:
        monkeypatch.setattr(cli, "CONFIG_PATH", pkgdir / "missing.conf")
        monkeypatch.setattr(cli, "read_token", Mock(return_value="secret"))
        monkeypatch.setattr(cli, "GitHubClient", Mock())
        monkeypatch.setattr(cli, "config", lambda: {"PKGDIR": str(pkgdir)})
        monkeypatch.setattr(
            cli, "pull_locked", lambda *_args: pull._replace_cache(pkgdir, staging)
        )
        assert (
            cli.main(["pull", "--repository", "owner/repo", "--token-file", "token"])
            == 1
        )
        message = capsys.readouterr().err
    else:
        with pytest.raises(OSError, match="rollback failed") as raised:
            pull._replace_cache(pkgdir, staging)
        message = str(raised.value)

    (backup,) = pkgdir.parent.glob(f".{pkgdir.name}.binrepo-backup-*")
    assert (backup / "Packages").read_bytes() == b"old index"
    assert (backup / "cat/old/old-1.gpkg.tar").read_bytes() == b"old package"
    assert f"backup retained at {backup}" in message
    assert "rollback failed" in message
    assert not backup.is_relative_to(pkgdir)
    with pytest.raises(FileExistsError, match="Recover retained cache backup"):
        pull._replace_cache(pkgdir, staging)
    assert (backup / "Packages").read_bytes() == b"old index"


def test_replace_cache_restores_directory_shaped_destination(
    cache_replacement: tuple[Path, Path],
) -> None:
    pkgdir, staging = cache_replacement
    (staging / "cat/old").write_bytes(b"collides with old package directory")

    with pytest.raises(IsADirectoryError):
        pull._replace_cache(pkgdir, staging)

    assert (pkgdir / "Packages").read_bytes() == b"old index"
    assert (pkgdir / "cat/old/old-1.gpkg.tar").read_bytes() == b"old package"
    assert (pkgdir / "cat/pkg").is_symlink()
    assert not (pkgdir / "cat/pkg/pkg-1.gpkg.tar").exists()
    assert not list(pkgdir.parent.glob(f".{pkgdir.name}.binrepo-backup-*"))


def test_replace_cache_reports_cleanup_failure_after_installation(
    cache_replacement: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    pkgdir, staging = cache_replacement
    monkeypatch.setattr(
        pull.shutil, "rmtree", Mock(side_effect=OSError("cleanup failed"))
    )

    with pytest.raises(OSError, match="New cache installed") as raised:
        pull._replace_cache(pkgdir, staging)

    (backup,) = pkgdir.parent.glob(f".{pkgdir.name}.binrepo-backup-*")
    assert str(backup) in str(raised.value)
    assert (pkgdir / "Packages").read_bytes() == b"new index"
    assert (pkgdir / "cat/pkg/pkg-1.gpkg.tar").read_bytes() == b"new package"
    assert (backup / "Packages").read_bytes() == b"old index"


def test_replace_cache_keeps_directory_and_lock_on_success(
    cache_replacement: tuple[Path, Path],
) -> None:
    pkgdir, staging = cache_replacement
    root_inode = pkgdir.stat().st_ino
    lock = pkgdir / "Packages.portage_lockfile"
    lock_inode = lock.stat().st_ino

    pull._replace_cache(pkgdir, staging)

    assert (pkgdir / "Packages").read_bytes() == b"new index"
    assert (pkgdir / "cat/pkg/pkg-1.gpkg.tar").read_bytes() == b"new package"
    assert not (pkgdir / "cat/old").exists()
    assert not list(pkgdir.parent.glob(f".{pkgdir.name}.binrepo-backup-*"))
    assert pkgdir.stat().st_ino == root_inode
    assert lock.stat().st_ino == lock_inode
