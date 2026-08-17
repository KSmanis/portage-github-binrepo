import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Never

import pytest
import requests

from portage_github_binrepo import cli
from portage_github_binrepo import github
from portage_github_binrepo import init as init_module
from portage_github_binrepo import package
from portage_github_binrepo import pull
from portage_github_binrepo import push
from tests.test_binrepo import make_packages
from tests.test_binrepo import write_pkgdir


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
        chost = f"x86_64-pc-linux-gnu-{suffix}"
        first = "cat/one/one-1-1.gpkg.tar"
        first_content = b"first"
        write_pkgdir(
            tmp_path,
            make_packages(first, sizes={first: len(first_content)}, chost=chost),
            {first: first_content},
        )

        assert push.push(client, tmp_path)["uploaded"] == 1
        branch = github.BINREPO_BRANCH
        assert client.get_content("Packages", branch)
        first_tag, first_name = package.release_coordinates(f"{branch}/{chost}/{first}")
        release = client.get_release(first_tag)
        assert release
        assert release["tag_name"] == first_tag

        pulled_index = tmp_path / "pulled-Packages"
        pull.pull(
            client,
            f"https://raw.githubusercontent.com/{repository}/{branch}/Packages",
            pulled_index,
        )
        first_remote_path = f"{first_tag}/{first_name}"
        assert f"PATH: {first_remote_path}" in pulled_index.read_text(encoding="utf-8")
        pulled_asset = tmp_path / "pulled-asset"
        pull.pull(
            client,
            f"https://github.com/{repository}/releases/download/{first_tag}/{first_name}",
            pulled_asset,
        )
        assert pulled_asset.read_bytes() == b"first"

        second = "cat/two/two-1-1.gpkg.tar"
        second_content = b"second"
        write_pkgdir(
            tmp_path,
            make_packages(second, sizes={second: len(second_content)}, chost=chost),
            {second: second_content},
        )
        second_tag, second_name = package.release_coordinates(
            f"{branch}/{chost}/{second}"
        )

        assert push.push(client, tmp_path) == {
            "uploaded": 1,
            "removed": 1,
            "unchanged": 0,
        }
        assert client.get_release(first_tag) is None
        assert client.get_release(second_tag) is not None

        updated_content = b"updated after uncertain index write"
        write_pkgdir(
            tmp_path,
            make_packages(second, sizes={second: len(updated_content)}, chost=chost),
            {second: updated_content},
        )
        put_content = client.put_content
        lose_response = True

        def apply_then_fail(
            path: str, branch: str, content: bytes, message: str, sha: str | None = None
        ) -> object:
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

        pull.pull(
            client,
            f"https://github.com/{repository}/releases/download/{second_tag}/{second_name}",
            pulled_asset,
        )
        assert pulled_asset.read_bytes() == updated_content
        index = client.get_content("Packages", branch)
        assert package.CLEANUP_FIELD not in client.content_bytes(index).decode()

        rejected_content = b"upload must be rolled back"
        write_pkgdir(
            tmp_path,
            make_packages(second, sizes={second: len(rejected_content)}, chost=chost),
            {second: rejected_content},
        )
        upload_asset = client.upload_asset
        list_assets = client.list_assets
        asset_list_count = 0

        def fail_upload(*_args: object, **_kwargs: object) -> Never:
            raise github.GitHubError("simulated upload failure")  # noqa: TRY003

        def fail_upload_reconciliation(release_id: int) -> list[object]:
            nonlocal asset_list_count
            asset_list_count += 1
            if asset_list_count == 2:
                raise github.GitHubError("simulated reconciliation failure")  # noqa: TRY003
            return list_assets(release_id)

        object.__setattr__(client, "upload_asset", fail_upload)
        object.__setattr__(client, "list_assets", fail_upload_reconciliation)
        try:
            with pytest.raises(
                github.GitHubError, match="simulated reconciliation failure"
            ):
                push.push(client, tmp_path)
        finally:
            object.__setattr__(client, "upload_asset", upload_asset)
            object.__setattr__(client, "list_assets", list_assets)

        pull.pull(
            client,
            f"https://github.com/{repository}/releases/download/{second_tag}/{second_name}",
            pulled_asset,
        )
        assert pulled_asset.read_bytes() == updated_content

        final_content = b"cleanup resumes"
        write_pkgdir(
            tmp_path,
            make_packages(second, sizes={second: len(final_content)}, chost=chost),
            {second: final_content},
        )
        delete_asset = client.delete_asset
        fail_cleanup = True

        def fail_backup_cleanup(asset_id: int) -> object:
            nonlocal fail_cleanup
            if fail_cleanup:
                fail_cleanup = False
                raise github.GitHubError("simulated cleanup failure")  # noqa: TRY003
            return delete_asset(asset_id)

        object.__setattr__(client, "delete_asset", fail_backup_cleanup)
        try:
            with pytest.raises(github.GitHubError, match="simulated cleanup failure"):
                push.push(client, tmp_path)
        finally:
            object.__setattr__(client, "delete_asset", delete_asset)

        index = client.get_content("Packages", branch)
        assert package.CLEANUP_FIELD in client.content_bytes(index).decode()
        assert push.push(client, tmp_path) == {
            "uploaded": 0,
            "removed": 0,
            "unchanged": 1,
        }
        index = client.get_content("Packages", branch)
        assert package.CLEANUP_FIELD not in client.content_bytes(index).decode()

        write_pkgdir(tmp_path, make_packages(), {})
        delete_ref = client.delete_ref
        fail_cleanup = True

        def apply_ref_delete_then_fail(ref: str) -> object:
            nonlocal fail_cleanup
            result = delete_ref(ref)
            if fail_cleanup:
                fail_cleanup = False
                raise github.GitHubError("simulated tag cleanup failure")  # noqa: TRY003
            return result

        object.__setattr__(client, "delete_ref", apply_ref_delete_then_fail)
        try:
            with pytest.raises(
                github.GitHubError, match="simulated tag cleanup failure"
            ):
                push.push(client, tmp_path)
        finally:
            object.__setattr__(client, "delete_ref", delete_ref)

        index = client.get_content("Packages", branch)
        assert package.CLEANUP_FIELD in client.content_bytes(index).decode()
        assert push.push(client, tmp_path) == {
            "uploaded": 0,
            "removed": 0,
            "unchanged": 0,
        }
        assert client.get_release(second_tag) is None
        index = client.get_content("Packages", branch)
        assert package.CLEANUP_FIELD not in client.content_bytes(index).decode()
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
