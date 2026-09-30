from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from portage_github_binrepo import cli
from portage_github_binrepo import pull
from tests.test_binrepo import make_remote_packages
from tests.test_binrepo import remote_entry

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("value", ("", "'unterminated", "owner/repo extra"))
def test_config_rejects_invalid_values(value: str, tmp_path: Path) -> None:
    config = tmp_path / "producer.conf"
    config.write_text(f"repository = {value}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Value validation failed"):
        cli.read_config(config)


@pytest.mark.parametrize("directory", (False, True))
def test_token_rejects_empty_or_nonregular_file(
    directory: bool, tmp_path: Path
) -> None:
    token = tmp_path / "token"
    if directory:
        token.mkdir(mode=0o700)
    else:
        token.write_text(" \n\t", encoding="utf-8")
        token.chmod(0o600)

    with pytest.raises(ValueError, match="regular file" if directory else "empty"):
        cli.read_token(token)


def test_cli_requires_complete_pull_arguments(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    read_config = Mock()
    monkeypatch.setattr(cli, "read_config", read_config)

    assert cli.main(["pull", "https://example.com/Packages"]) == 1

    assert capsys.readouterr().err == (
        "portage-github-binrepo: pull requires both URI and destination, or neither\n"
    )
    read_config.assert_not_called()


def test_cli_requires_repository_before_reading_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    read_token = Mock()
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "missing.conf")
    monkeypatch.setattr(cli, "read_token", read_token)

    assert cli.main(["check"]) == 1

    assert capsys.readouterr().err == (
        "portage-github-binrepo: repository must be set in the global config "
        "or with --repository\n"
    )
    read_token.assert_not_called()


def test_explicit_options_bypass_global_config_and_init_public_repository(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    read_config = Mock(side_effect=ValueError("invalid global config"))
    read_token = Mock(return_value="secret")
    client = Mock()
    client.get_repository.return_value = None
    client.check.return_value = {
        "private": False,
        "default_branch": "main",
        "access": "write",
        "initialized": False,
    }
    make_client = Mock(return_value=client)
    monkeypatch.setattr(cli, "read_config", read_config)
    monkeypatch.setattr(cli, "read_token", read_token)
    monkeypatch.setattr(cli, "GitHubClient", make_client)

    assert (
        cli.main(
            [
                "init",
                "--repository",
                "other/repo",
                "--token-file",
                "other.token",
                "--branch",
                "release/current",
                "--public",
            ]
        )
        == 0
    )

    read_config.assert_not_called()
    read_token.assert_called_once_with("other.token")
    make_client.assert_called_once_with("other/repo", "secret")
    client.create_repository.assert_called_once_with(private=False)
    client.check.assert_called_once_with(write=True, branch="release/current")
    assert capsys.readouterr().out == (
        "repository=other/repo created=true private=false default_branch=main\n"
    )


def test_pull_cli_downloads_using_portage_cached_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages = make_remote_packages("cat/pkg/pkg-1.gpkg.tar")
    remote_path, _ = remote_entry(packages, "cat/pkg/pkg-1.gpkg.tar")
    uri = f"https://github.com/owner/repo/releases/download/{remote_path}"
    cached = pull.cached_packages_path(uri, tmp_path)
    cached.parent.mkdir(parents=True)
    cached.write_text(packages, encoding="utf-8")
    client = Mock(repository="owner/repo")
    monkeypatch.setattr(cli, "config", lambda: {"EROOT": str(tmp_path)})
    monkeypatch.setattr(cli, "read_token", Mock(return_value="secret"))
    monkeypatch.setattr(cli, "GitHubClient", Mock(return_value=client))
    destination = tmp_path / "package"

    assert (
        cli.main(
            [
                "pull",
                "--repository",
                "owner/repo",
                "--token-file",
                "token",
                "--branch",
                "binrepo",
                uri,
                str(destination),
            ]
        )
        == 0
    )

    client.download_asset.assert_called_once_with(9, destination)


def test_pull_cli_reports_missing_cached_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Mock(repository="owner/repo")
    monkeypatch.setattr(cli, "config", lambda: {"EROOT": str(tmp_path)})
    monkeypatch.setattr(cli, "read_token", Mock(return_value="secret"))
    monkeypatch.setattr(cli, "GitHubClient", Mock(return_value=client))

    assert (
        cli.main(
            [
                "pull",
                "--repository",
                "owner/repo",
                "--token-file",
                "token",
                "--branch",
                "binrepo",
                "https://github.com/owner/repo/releases/download/binrepo/0/pkg.gpkg.tar",
                str(tmp_path / "package"),
            ]
        )
        == 1
    )

    assert "No such file or directory" in capsys.readouterr().err
    client.download_asset.assert_not_called()


def test_pull_cli_does_not_hide_token_permission_error_for_assets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_client = Mock()
    monkeypatch.setattr(cli, "read_token", Mock(side_effect=PermissionError("denied")))
    monkeypatch.setattr(cli, "GitHubClient", make_client)

    assert (
        cli.main(
            [
                "pull",
                "--repository",
                "owner/repo",
                "--token-file",
                "token",
                "--branch",
                "binrepo",
                "https://github.com/owner/repo/releases/download/binrepo/0/pkg.gpkg.tar",
                str(tmp_path / "package"),
            ]
        )
        == 1
    )

    assert capsys.readouterr().err == "portage-github-binrepo: denied\n"
    assert not (tmp_path / "package").exists()
    make_client.assert_not_called()


def test_pull_cli_requires_pkgdir(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pull_locked = Mock()
    monkeypatch.setattr(cli, "config", dict)
    monkeypatch.setattr(cli, "read_token", Mock(return_value="secret"))
    monkeypatch.setattr(cli, "GitHubClient", Mock())
    monkeypatch.setattr(cli, "pull_locked", pull_locked)

    assert (
        cli.main(
            [
                "pull",
                "--repository",
                "owner/repo",
                "--token-file",
                "token",
                "--branch",
                "binrepo",
            ]
        )
        == 1
    )

    assert capsys.readouterr().err == (
        "portage-github-binrepo: PKGDIR must be set in Portage configuration\n"
    )
    pull_locked.assert_not_called()
