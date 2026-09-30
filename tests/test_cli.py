from __future__ import annotations

import gzip
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from portage import getbinpkg

from portage_github_binrepo import cli
from portage_github_binrepo import pull
from tests.test_binrepo import make_remote_packages
from tests.test_binrepo import remote_entry

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("secret\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def cli_args(token_file: Path) -> list[str]:
    return ["--repository", "owner/repo", "--token-file", str(token_file)]


@pytest.fixture
def make_client(
    tmp_path: Path, token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> Mock:
    constructor = Mock(return_value=Mock(repository="owner/repo"))
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "missing.conf")
    monkeypatch.setattr(cli, "TOKEN_PATH", token_file)
    monkeypatch.setattr(cli, "GitHubClient", constructor)
    return constructor


@pytest.mark.parametrize(
    ("text", "message"),
    (
        ("repository = ", "Value validation failed"),
        ("repository = 'unterminated", "Value validation failed"),
        ("repository = owner/repo extra", "Value validation failed"),
        ("unknown = value", "Key validation failed at line: 1"),
    ),
)
def test_config_rejects_invalid_settings(
    text: str, message: str, tmp_path: Path
) -> None:
    config = tmp_path / "producer.conf"
    config.write_text(f"{text}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        cli.read_config(config)


def test_token_trims_whitespace(token_file: Path) -> None:
    assert cli.read_token(token_file) == "secret"


@pytest.mark.parametrize(
    ("kind", "message"),
    (("empty", "empty"), ("directory", "regular file"), ("public", "group or others")),
)
def test_token_rejects_invalid_file(kind: str, message: str, token_file: Path) -> None:
    if kind == "directory":
        token_file.unlink()
        token_file.mkdir(mode=0o700)
    elif kind == "public":
        token_file.chmod(0o644)
    else:
        token_file.write_text(" \n\t", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        cli.read_token(token_file)


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
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_token = Mock()
    monkeypatch.setattr(cli, "read_token", read_token)
    assert cli.main(["check"]) == 1
    assert capsys.readouterr().err == (
        "portage-github-binrepo: repository must be set in the global config "
        "or with --repository\n"
    )
    read_token.assert_not_called()
    make_client.assert_not_called()


@pytest.mark.parametrize("public", (False, True), ids=("private", "public"))
def test_explicit_options_bypass_global_config_and_initialize_repository(
    public: bool,
    cli_args: list[str],
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_config = Mock(side_effect=ValueError("invalid global config"))
    monkeypatch.setattr(cli, "read_config", read_config)
    client = make_client.return_value
    client.get_repository.return_value = None
    client.check.return_value = {
        "private": not public,
        "default_branch": "main",
        "access": "write",
        "initialized": False,
    }
    args = ["init", *cli_args, "--branch", "release/current"]
    if public:
        args.append("--public")
    assert cli.main(args) == 0
    read_config.assert_not_called()
    make_client.assert_called_once_with("owner/repo", "secret")
    client.create_repository.assert_called_once_with(private=not public)
    client.check.assert_called_once_with(write=True, branch="release/current")
    assert capsys.readouterr().out == (
        f"repository=owner/repo created=true private={str(not public).lower()} default_branch=main\n"
    )


@pytest.mark.parametrize("read_only", (False, True), ids=("producer", "consumer"))
def test_cli_uses_global_config(
    read_only: bool,
    tmp_path: Path,
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "producer.conf"
    config.write_text(
        "# comments may contain = signs\n"
        "repository = 'owner/repo'  # one required setting\n"
        "branch = testing\n",
        encoding="utf-8",
    )
    access = "read" if read_only else "write"
    make_client.return_value.check.return_value = {
        "private": True,
        "default_branch": "main",
        "access": access,
    }
    monkeypatch.setattr(cli, "CONFIG_PATH", config)
    assert cli.main(["check", *(["--read-only"] if read_only else [])]) == 0
    make_client.assert_called_once_with("owner/repo", "secret")
    make_client.return_value.check.assert_called_once_with(
        write=not read_only, branch="testing"
    )
    assert capsys.readouterr().out == (
        f"repository=owner/repo access={access} private=true default_branch=main\n"
    )


@pytest.mark.parametrize("cached", (False, True), ids=("missing-cache", "cached-index"))
def test_pull_cli_uses_portage_cached_index(
    cached: bool,
    tmp_path: Path,
    cli_args: list[str],
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = make_remote_packages("cat/pkg/pkg-1.gpkg.tar")
    remote_path, _ = remote_entry(packages, "cat/pkg/pkg-1.gpkg.tar")
    uri = f"https://github.com/owner/repo/releases/download/{remote_path}"
    if cached:
        index = pull.cached_packages_path(uri, tmp_path)
        index.parent.mkdir(parents=True)
        index.write_text(packages, encoding="utf-8")
    monkeypatch.setattr(cli, "config", lambda: {"EROOT": str(tmp_path)})
    destination = tmp_path / "package"
    assert cli.main(["pull", *cli_args, uri, str(destination)]) == (0 if cached else 1)
    if cached:
        make_client.return_value.download_asset.assert_called_once_with(9, destination)
        assert capsys.readouterr().err == ""
    else:
        assert "No such file or directory" in capsys.readouterr().err
        make_client.return_value.download_asset.assert_not_called()


@pytest.mark.parametrize("command", ("check", "pull"))
def test_cli_rejects_unreadable_token_outside_index_downloads(
    command: str,
    tmp_path: Path,
    cli_args: list[str],
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "read_token", Mock(side_effect=PermissionError("denied")))
    args = [command, *cli_args]
    destination = tmp_path / "package"
    if command == "pull":
        args += [
            "https://github.com/owner/repo/releases/download/binrepo/0/pkg.gpkg.tar",
            str(destination),
        ]
    assert cli.main(args) == 1
    assert capsys.readouterr().err == "portage-github-binrepo: denied\n"
    assert not destination.exists()
    make_client.assert_not_called()


@pytest.mark.parametrize("command", ("push", "pull"))
@pytest.mark.parametrize(
    "branch", (None, "testing"), ids=("default-branch", "custom-branch")
)
def test_cli_syncs_portage_pkgdir(
    command: str,
    branch: str | None,
    cli_args: list[str],
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = Mock(return_value={"uploaded": 0, "removed": 0, "unchanged": 0})
    monkeypatch.setattr(cli, "config", lambda: {"PKGDIR": "/binpkgs", "CHOST": "host"})
    monkeypatch.setattr(cli, f"{command}_locked", operation)
    args = [command, *cli_args]
    if branch is not None:
        args += ["--branch", branch]
    assert cli.main(args) == 0
    operation.assert_called_once_with(
        make_client.return_value, "/binpkgs", branch or "binrepo"
    )
    assert capsys.readouterr().out == (
        "uploaded=0 removed=0 unchanged=0\n" if command == "push" else ""
    )


@pytest.mark.parametrize("command", ("push", "pull"))
def test_cli_requires_pkgdir(
    command: str,
    cli_args: list[str],
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = Mock()
    monkeypatch.setattr(cli, "config", dict)
    monkeypatch.setattr(cli, f"{command}_locked", operation)
    assert cli.main([command, *cli_args]) == 1
    assert capsys.readouterr().err == (
        "portage-github-binrepo: PKGDIR must be set in Portage configuration\n"
    )
    operation.assert_not_called()
    make_client.assert_called_once_with("owner/repo", "secret")


def test_pull_cli_infers_repository_from_uri(
    tmp_path: Path, make_client: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    download = Mock()
    monkeypatch.setattr(cli, "pull", download)
    monkeypatch.setattr(cli, "config", lambda: {"EROOT": str(tmp_path)})
    uri = "https://raw.githubusercontent.com/owner/repo/host/Packages"
    assert cli.main(["pull", uri, str(tmp_path / "Packages")]) == 0
    make_client.assert_called_once_with("owner/repo", "secret")
    download.assert_called_once_with(
        make_client.return_value, uri, str(tmp_path / "Packages"), None
    )


@pytest.mark.parametrize("name", ("Packages", "Packages.gz"))
def test_pull_cli_returns_empty_index_for_unreadable_token(
    name: str,
    tmp_path: Path,
    make_client: Mock,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / name
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
    assert data.decode() == "PACKAGES: 0\nTIMESTAMP: 123\nVERSION: 0\n\n"
    assert capsys.readouterr().err == ""
    make_client.assert_not_called()


@pytest.mark.parametrize(
    ("command", "option", "attribute"),
    (("init", "--public", "public"), ("check", "--read-only", "read_only")),
)
def test_init_and_check_cli_options(
    command: str, option: str, attribute: str, cli_args: list[str]
) -> None:
    args = cli.make_parser().parse_args([command, *cli_args, option])
    assert args.command == command
    assert getattr(args, attribute) is True
    assert cli.make_parser().parse_args([command]).repository is None
