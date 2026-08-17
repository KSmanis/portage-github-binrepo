import base64
import json
from collections.abc import Iterator
from typing import cast

import pytest
import requests
import responses
from inline_snapshot import snapshot
from portage import getbinpkg

from portage_github_binrepo import github

API = "https://api.github.com"


@pytest.fixture
def http() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as mock:
        yield mock


def test_safe_request_retries_transient_response(http: responses.RequestsMock) -> None:
    http.get(f"{API}/resource", json={"message": "busy"}, status=503)
    http.get(f"{API}/resource", json={"ok": True})
    sleeps = []
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    assert client.json("GET", "/resource") == {"ok": True}
    assert len(http.calls) == 2
    assert sleeps == [1]
    assert http.calls[0].request.headers["Authorization"] == "Bearer secret"


def test_delete_asset_accepts_not_found_after_retry(
    http: responses.RequestsMock,
) -> None:
    url = f"{API}/repos/owner/repo/releases/assets/1"
    http.delete(url, json={"message": "busy"}, status=503)
    http.delete(url, json={}, status=404)
    sleeps = []
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    client.delete_asset(1)

    assert len(http.calls) == 2
    assert sleeps == [1]


def test_non_idempotent_request_is_not_retried(http: responses.RequestsMock) -> None:
    http.post(f"{API}/resource", json={"message": "busy"}, status=503)
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(github.GitHubError, match="returned 503"):
        client.json("POST", "/resource", expected=(201,))

    assert len(http.calls) == 1


def test_pagination_follows_link_header(http: responses.RequestsMock) -> None:
    second = "https://api.github.com/page/2"
    http.get(
        f"{API}/page/1", json=[{"id": 1}], headers={"Link": f'<{second}>; rel="next"'}
    )
    http.get(second, json=[{"id": 2}])
    client = github.GitHubClient("owner/repo", "secret")

    assert list(client.paginate("/page/1")) == [{"id": 1}, {"id": 2}]
    assert http.calls[1].request.url == second


def test_check_accepts_repository_without_user_permissions(
    http: responses.RequestsMock,
) -> None:
    http.get(
        f"{API}/repos/owner/repo", json={"private": True, "default_branch": "main"}
    )
    http.get(
        f"{API}/repos/owner/repo/git/ref/heads/binrepo",
        json={"ref": "refs/heads/binrepo", "object": {"sha": "current"}},
    )
    client = github.GitHubClient("owner/repo", "secret")

    assert client.check() == snapshot(
        {
            "private": True,
            "default_branch": "main",
            "access": "write",
            "initialized": True,
        }
    )
    assert [call.request.url for call in http.calls] == snapshot(
        [
            "https://api.github.com/repos/owner/repo",
            "https://api.github.com/repos/owner/repo/git/ref/heads/binrepo",
        ]
    )


def test_empty_repository_ref_is_uninitialized(http: responses.RequestsMock) -> None:
    http.get(
        f"{API}/repos/owner/repo/git/ref/heads/main",
        json={"message": "Git Repository is empty."},
        status=409,
    )
    client = github.GitHubClient("owner/repo", "secret")

    assert client.get_ref("heads/main") is None


def test_check_accepts_empty_repository(http: responses.RequestsMock) -> None:
    http.get(
        f"{API}/repos/owner/repo", json={"private": True, "default_branch": "main"}
    )
    http.get(f"{API}/repos/owner/repo/git/ref/heads/binrepo", json={}, status=409)
    client = github.GitHubClient("owner/repo", "secret")

    assert client.check()["initialized"] is False


def test_repository_initializes_orphan_binrepo_branch(
    http: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(getbinpkg.time, "time", lambda: 123)
    repo = f"{API}/repos/owner/repo"
    http.get(f"{repo}/git/ref/heads/binrepo", json={}, status=409)
    http.get(f"{repo}/git/ref/heads/main", json={}, status=409)
    http.put(
        f"{repo}/contents/README.md", json={"content": {"sha": "bootstrap"}}, status=201
    )
    http.post(f"{repo}/git/trees", json={"sha": "tree"}, status=201)
    http.post(f"{repo}/git/commits", json={"sha": "commit"}, status=201)
    http.post(f"{repo}/git/refs", json={"ref": "refs/heads/binrepo"}, status=201)
    client = github.GitHubClient("owner/repo", "secret")

    client.initialize_repository("main")

    assert [(call.request.method, call.request.url) for call in http.calls] == snapshot(
        [
            ("GET", "https://api.github.com/repos/owner/repo/git/ref/heads/binrepo"),
            ("GET", "https://api.github.com/repos/owner/repo/git/ref/heads/main"),
            ("PUT", "https://api.github.com/repos/owner/repo/contents/README.md"),
            ("POST", "https://api.github.com/repos/owner/repo/git/trees"),
            ("POST", "https://api.github.com/repos/owner/repo/git/commits"),
            ("POST", "https://api.github.com/repos/owner/repo/git/refs"),
        ]
    )
    bootstrap_body = json.loads(cast("bytes", http.calls[2].request.body))
    assert bootstrap_body["message"] == snapshot("Initialize repo")
    assert "sync-uri = https://raw.githubusercontent.com/owner/repo/binrepo" in (
        base64.b64decode(bootstrap_body["content"]).decode()
    )
    assert json.loads(cast("bytes", http.calls[3].request.body)) == snapshot(
        {
            "tree": [
                {
                    "path": "Packages",
                    "mode": "100644",
                    "type": "blob",
                    "content": "PACKAGES: 0\nTIMESTAMP: 123\nVERSION: 0\n\n",
                }
            ]
        }
    )
    assert json.loads(cast("bytes", http.calls[4].request.body)) == snapshot(
        {"message": "Initialize binrepo", "tree": "tree", "parents": []}
    )
    assert json.loads(cast("bytes", http.calls[5].request.body)) == snapshot(
        {"ref": "refs/heads/binrepo", "sha": "commit"}
    )


def test_repository_with_existing_binrepo_branch_is_not_initialized(
    http: responses.RequestsMock,
) -> None:
    http.get(
        f"{API}/repos/owner/repo/git/ref/heads/binrepo",
        json={"ref": "refs/heads/binrepo"},
    )
    client = github.GitHubClient("owner/repo", "secret")

    assert client.initialize_repository("main") == {"ref": "refs/heads/binrepo"}
    assert len(http.calls) == 1


def test_repository_adds_binrepo_branch_without_changing_default_branch(
    http: responses.RequestsMock,
) -> None:
    repo = f"{API}/repos/owner/repo"
    http.get(f"{repo}/git/ref/heads/binrepo", json={}, status=404)
    http.get(f"{repo}/git/ref/heads/main", json={"ref": "refs/heads/main"})
    http.post(f"{repo}/git/trees", json={"sha": "tree"}, status=201)
    http.post(f"{repo}/git/commits", json={"sha": "commit"}, status=201)
    http.post(f"{repo}/git/refs", json={"ref": "refs/heads/binrepo"}, status=201)
    client = github.GitHubClient("owner/repo", "secret")

    client.initialize_repository("main")

    assert [call.request.method for call in http.calls] == [
        "GET",
        "GET",
        "POST",
        "POST",
        "POST",
    ]


def test_repository_recovers_lost_readme_response(http: responses.RequestsMock) -> None:
    repo = f"{API}/repos/owner/repo"
    http.get(f"{repo}/git/ref/heads/binrepo", json={}, status=404)
    http.get(f"{repo}/git/ref/heads/main", json={}, status=409)
    http.put(
        f"{repo}/contents/README.md", body=requests.ConnectionError("response lost")
    )
    http.get(f"{repo}/git/ref/heads/main", json={"ref": "refs/heads/main"})
    http.post(f"{repo}/git/trees", json={"sha": "tree"}, status=201)
    http.post(f"{repo}/git/commits", json={"sha": "commit"}, status=201)
    http.post(f"{repo}/git/refs", json={"ref": "refs/heads/binrepo"}, status=201)
    client = github.GitHubClient("owner/repo", "secret")

    assert client.initialize_repository("main") == {"ref": "refs/heads/binrepo"}


def test_repository_recovers_lost_binrepo_ref_response(
    http: responses.RequestsMock,
) -> None:
    repo = f"{API}/repos/owner/repo"
    http.get(f"{repo}/git/ref/heads/binrepo", json={}, status=404)
    http.get(f"{repo}/git/ref/heads/main", json={"ref": "refs/heads/main"})
    http.post(f"{repo}/git/trees", json={"sha": "tree"}, status=201)
    http.post(f"{repo}/git/commits", json={"sha": "commit"}, status=201)
    http.post(f"{repo}/git/refs", body=requests.ConnectionError("response lost"))
    http.get(
        f"{repo}/git/ref/heads/binrepo",
        json={"ref": "refs/heads/binrepo", "object": {"sha": "commit"}},
    )
    client = github.GitHubClient("owner/repo", "secret")

    assert client.initialize_repository("main") == {
        "ref": "refs/heads/binrepo",
        "object": {"sha": "commit"},
    }


def test_release_name_matches_tag_and_description_is_empty(
    http: responses.RequestsMock,
) -> None:
    http.post(f"{API}/repos/owner/repo/releases", json={"id": 1}, status=201)
    client = github.GitHubClient("owner/repo", "secret")

    assert client.create_release("host/cat/package", "host") == {"id": 1}
    assert json.loads(cast("bytes", http.calls[0].request.body)) == snapshot(
        {
            "tag_name": "host/cat/package",
            "target_commitish": "host",
            "name": "host/cat/package",
            "body": "",
            "draft": False,
            "prerelease": False,
            "make_latest": "false",
        }
    )
