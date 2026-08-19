import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
import requests

from portage_github_binrepo import cli
from portage_github_binrepo import github
from portage_github_binrepo import init as init_module
from portage_github_binrepo import package
from portage_github_binrepo import pull
from portage_github_binrepo import push
from tests.test_binrepo import write_pkgdir


def representative_packages(
    entries: list[tuple[str, str, str, bytes]],
) -> tuple[str, dict[str, bytes]]:
    stanzas = [
        "\n".join(
            (f"CPV: {cpv}", f"CHOST: {chost}", f"PATH: {path}", f"SIZE: {len(content)}")
        )
        for path, cpv, chost, content in entries
    ]
    return (
        "\n\n".join((f"PACKAGES: {len(entries)}", *stanzas)) + "\n\n",
        {path: content for path, _, _, content in entries},
    )


@pytest.mark.live
def test_private_repository_round_trip(tmp_path: Path) -> None:
    token_path_value = os.environ.get("PORTAGE_GITHUB_BINREPO_LIVE_TOKEN_FILE")
    if token_path_value:
        token = cli.read_token(token_path_value)
    elif os.environ.get("PORTAGE_GITHUB_BINREPO_LIVE_USE_GH") == "1":
        gh = shutil.which("gh")
        if gh is None:
            pytest.skip("gh is not installed")
        token = subprocess.run(  # noqa: S603
            [gh, "auth", "token"], check=True, capture_output=True, text=True
        ).stdout.strip()
    else:
        pytest.skip(
            "set PORTAGE_GITHUB_BINREPO_LIVE_TOKEN_FILE or PORTAGE_GITHUB_BINREPO_LIVE_USE_GH=1"
        )
    keep_repository = os.environ.get("PORTAGE_GITHUB_BINREPO_LIVE_KEEP") == "1"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": github.API_VERSION,
        "User-Agent": "portage-github-binrepo-live-test",
    }
    name = f"portage-github-binrepo-check-{secrets.token_hex(6)}"
    repository = None
    session = requests.Session()
    session.headers.update(headers)
    try:
        repository = os.environ.get("PORTAGE_GITHUB_BINREPO_LIVE_REPOSITORY")
        expected_created = repository is None
        if not repository:
            login_response = session.get(
                "https://api.github.com/user", timeout=(10, 60)
            )
            login_response.raise_for_status()
            repository = f"{login_response.json()['login']}/{name}"
        client = github.GitHubClient(repository, token)
        init = init_module.init_repository(client)
        assert init["created"] is expected_created
        assert init["private"] is True
        assert client.check(write=True)["access"] == "write"

        suffix = secrets.token_hex(4)
        entries = [
            (
                "x86_64/sys-apps/portage/portage-3.0.81-r1-1.gpkg.tar",
                "sys-apps/portage-3.0.81-r1",
                f"x86_64-pc-linux-gnu-{suffix}",
                b"representative amd64 gpkg",
            ),
            (
                "arm64/dev-lang/python-3.14.0-r1.gpkg.tar",
                "dev-lang/python-3.14.0-r1",
                f"aarch64-unknown-linux-gnu-{suffix}",
                b"representative arm64 flat gpkg",
            ),
            (
                "x86/sys-libs/zlib-1.3.1-r1.tbz2",
                "sys-libs/zlib-1.3.1-r1",
                f"i686-pc-linux-gnu-{suffix}",
                b"representative xpak",
            ),
        ]
        packages, files = representative_packages(entries)
        write_pkgdir(tmp_path, packages, files)

        assert push.push(client, tmp_path) == {
            "uploaded": 3,
            "removed": 0,
            "unchanged": 0,
        }
        branch = github.BINREPO_BRANCH
        remote_content = client.get_content("Packages", branch)
        assert remote_content is not None
        remote_text = client.content_bytes(remote_content).decode()
        remote_entries = package.parse_packages(remote_text)
        assert len(remote_entries) == 3
        assert {
            package.release_coordinates(path, branch)[0] for path in remote_entries
        } == {"binrepo/0"}
        assert (
            len(
                {
                    package.remote_ids(metadata, package.asset_ids(remote_text))[1]
                    for metadata in remote_entries.values()
                }
            )
            == 3
        )
        release = client.get_release("binrepo/0")
        assert release
        assert release["target_commitish"] == branch
        assert all("__" in Path(path).name for path in remote_entries)

        pulled_index = tmp_path / "pulled-Packages"
        pull.pull(
            client,
            f"https://raw.githubusercontent.com/{repository}/{branch}/Packages",
            pulled_index,
        )
        pulled_text = pulled_index.read_text(encoding="utf-8")
        for remote_path, metadata in remote_entries.items():
            local_path = metadata[package.LOCAL_PATH_FIELD]
            destination = tmp_path / "individual" / local_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            pull.pull(
                client,
                f"https://github.com/{repository}/releases/download/{remote_path}",
                destination,
                pulled_text,
            )
            assert destination.read_bytes() == files[local_path]

        mirror = tmp_path / "mirror"
        pull.pull_all(client, mirror)
        for local_path, content in files.items():
            assert (mirror / local_path).read_bytes() == content

        updated = b"updated arm64 package"
        entries[1] = (*entries[1][:3], updated)
        packages, files = representative_packages(entries)
        write_pkgdir(tmp_path, packages, files)
        assert push.push(client, tmp_path) == {
            "uploaded": 1,
            "removed": 0,
            "unchanged": 2,
        }
        assert len(client.list_assets(release["id"])) == 3

        uncertain = b"updated after uncertain index write"
        entries[2] = (*entries[2][:3], uncertain)
        packages, files = representative_packages(entries)
        write_pkgdir(tmp_path, packages, files)
        put_content = client.put_content
        lose_response = True

        def apply_then_fail(
            path: str, branch: str, content: bytes, message: str, sha: str | None = None
        ) -> github.ContentUpdate:
            nonlocal lose_response
            result = put_content(path, branch, content, message, sha)
            if lose_response:
                lose_response = False
                raise github.GitHubError("simulated lost index response")  # noqa: TRY003
            return result

        object.__setattr__(client, "put_content", apply_then_fail)
        try:
            assert push.push(client, tmp_path)["uploaded"] == 1
        finally:
            object.__setattr__(client, "put_content", put_content)

        cleanup_content = b"cleanup resumes by stored asset id"
        entries[0] = (*entries[0][:3], cleanup_content)
        packages, files = representative_packages(entries)
        write_pkgdir(tmp_path, packages, files)
        delete_asset = client.delete_asset
        fail_cleanup = True

        def fail_asset_cleanup(asset_id: int) -> None:
            nonlocal fail_cleanup
            if fail_cleanup:
                fail_cleanup = False
                raise github.GitHubError("simulated cleanup failure")  # noqa: TRY003
            delete_asset(asset_id)

        object.__setattr__(client, "delete_asset", fail_asset_cleanup)
        try:
            with pytest.raises(github.GitHubError, match="simulated cleanup failure"):
                push.push(client, tmp_path)
        finally:
            object.__setattr__(client, "delete_asset", delete_asset)
        index = client.get_content("Packages", branch)
        assert index is not None
        assert package.CLEANUP_FIELD in client.content_bytes(index).decode()
        assert push.push(client, tmp_path) == {
            "uploaded": 0,
            "removed": 0,
            "unchanged": 3,
        }
        index = client.get_content("Packages", branch)
        assert index is not None
        assert package.CLEANUP_FIELD not in client.content_bytes(index).decode()

        write_pkgdir(tmp_path, "PACKAGES: 0\n\n", {})
        assert push.push(client, tmp_path)["removed"] == 3
        assert client.get_release("binrepo/0") is None
    finally:
        if repository:
            if keep_repository:
                print(f"retained throwaway repository: https://github.com/{repository}")
            else:
                delete = session.delete(
                    f"https://api.github.com/repos/{repository}", timeout=(10, 60)
                )
                if delete.status_code != 204:
                    pytest.fail(
                        f"failed to delete throwaway repository https://github.com/{repository}"
                    )
                verify = session.get(
                    f"https://api.github.com/repos/{repository}", timeout=(10, 60)
                )
                if verify.status_code != 404:
                    pytest.fail(
                        f"throwaway repository still exists: https://github.com/{repository}"
                    )
