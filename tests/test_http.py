from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
import requests
import responses
from inline_snapshot import snapshot
from portage import getbinpkg

from portage_github_binrepo import github

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

API = "https://api.github.com"


def request_json(request: requests.PreparedRequest) -> github.JSONValue:
    assert isinstance(request.body, str | bytes | bytearray)
    return json.loads(request.body)


@pytest.fixture
def http() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as mock:
        yield mock


def test_safe_request_retries_transient_response(http: responses.RequestsMock) -> None:
    http.get(f"{API}/resource", json=["busy"], status=503)
    http.get(f"{API}/resource", json={"ok": True})
    sleeps = []
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    assert client.json("GET", "/resource") == {"ok": True}
    assert len(http.calls) == 2
    assert sleeps == [1]
    assert http.calls[0].request.headers["Authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    ("headers", "delay"),
    (
        ({"Retry-After": "120"}, 120),
        ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1120"}, 120),
        ({}, 60),
    ),
)
def test_rate_limit_waits_as_directed(
    http: responses.RequestsMock,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    delay: int,
) -> None:
    http.get(
        f"{API}/resource",
        json={"message": "secondary rate limit exceeded"},
        status=429,
        headers=headers,
    )
    http.get(f"{API}/resource", json={"ok": True})
    sleeps = []
    monkeypatch.setattr(github.time, "time", lambda: 1000)
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    assert client.json("GET", "/resource") == {"ok": True}
    assert sleeps == [delay]


def test_rate_limited_upload_retries_with_exponential_backoff(
    http: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = "https://uploads.github.com/repos/owner/repo/releases/1/assets"
    bodies = []
    statuses = iter((429, 429, 429, 201))

    def respond(request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        assert isinstance(request.body, bytes)
        bodies.append(request.body)
        status = next(statuses)
        return (
            status,
            {},
            json.dumps(
                {"message": "secondary rate limit exceeded"}
                if status == 429
                else {"id": 1, "name": "package", "size": 7}
            ),
        )

    http.add_callback(responses.POST, url, callback=respond)
    source = tmp_path / "package"
    source.write_bytes(b"package")
    sleeps = []
    monkeypatch.setattr(
        github.time, "monotonic", Mock(side_effect=[0, 0, 8, 8, 16, 16, 24, 24])
    )
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    assert client.upload_asset(1, source, source.name)["id"] == 1
    assert bodies == [b"package"] * 4
    assert sleeps == [60, 120, 240]


def test_mutative_requests_are_spaced(http: responses.RequestsMock) -> None:
    http.put(f"{API}/resource", json={"ok": True})
    http.put(f"{API}/resource", json={"ok": True})
    sleeps = []
    clock = Mock(side_effect=[0, 0, 0, 0])
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(github.time, "monotonic", clock)
        client.json("PUT", "/resource")
        client.json("PUT", "/resource")

    assert sleeps == [github.MUTATION_INTERVAL]


def test_oversized_asset_is_rejected_before_upload(
    http: responses.RequestsMock, tmp_path: Path
) -> None:
    source = tmp_path / "package"
    with source.open("wb") as stream:
        stream.truncate(github.MAX_ASSET_SIZE)
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(ValueError, match="smaller than 2 GiB"):
        client.upload_asset(1, source, source.name)

    assert not http.calls


def test_delete_asset_accepts_not_found_after_retry(
    http: responses.RequestsMock,
) -> None:
    url = f"{API}/repos/owner/repo/releases/assets/1"
    http.delete(url, json={"message": "busy"}, status=503)
    http.delete(url, json={}, status=404)
    sleeps = []
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(github.time, "monotonic", Mock(side_effect=[0, 0, 8, 8]))
        client.delete_asset(1)

    assert len(http.calls) == 2
    assert sleeps == [1]


def test_non_idempotent_request_is_not_retried(http: responses.RequestsMock) -> None:
    http.post(f"{API}/resource", json={"message": "busy"}, status=503)
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(github.GitHubError, match="returned 503"):
        client.json("POST", "/resource", expected=(201,))

    assert len(http.calls) == 1


def test_json_rejects_empty_response(http: responses.RequestsMock) -> None:
    http.get(f"{API}/resource", body="")
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(github.GitHubError, match="empty JSON response"):
        client.json("GET", "/resource")


def test_list_assets_follows_link_header(http: responses.RequestsMock) -> None:
    second = "https://api.github.com/page/2"
    http.get(
        f"{API}/repos/owner/repo/releases/1/assets?per_page=100",
        json=[{"id": 1}],
        headers={"Link": f'<{second}>; rel="next"'},
    )
    http.get(second, json=[{"id": 2}])
    client = github.GitHubClient("owner/repo", "secret")

    assert client.list_assets(1) == [{"id": 1}, {"id": 2}]
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
    client = github.GitHubClient("owner/repo", "secret", sleep=Mock())

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
    bootstrap_body = request_json(http.calls[2].request)
    assert isinstance(bootstrap_body, dict)
    assert bootstrap_body["message"] == snapshot("Initialize repo")
    bootstrap_content = bootstrap_body["content"]
    assert isinstance(bootstrap_content, str)
    assert "sync-uri = https://raw.githubusercontent.com/owner/repo/binrepo" in (
        base64.b64decode(bootstrap_content).decode()
    )
    assert request_json(http.calls[3].request) == snapshot(
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
    assert request_json(http.calls[4].request) == snapshot(
        {"message": "Initialize binrepo", "tree": "tree", "parents": []}
    )
    assert request_json(http.calls[5].request) == snapshot(
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
    client = github.GitHubClient("owner/repo", "secret", sleep=Mock())

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
    client = github.GitHubClient("owner/repo", "secret", sleep=Mock())

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
    client = github.GitHubClient("owner/repo", "secret", sleep=Mock())

    assert client.initialize_repository("main") == {
        "ref": "refs/heads/binrepo",
        "object": {"sha": "commit"},
    }


def test_repository_initialization_propagates_failed_bootstrap(
    http: responses.RequestsMock,
) -> None:
    repo = f"{API}/repos/owner/repo"
    http.get(f"{repo}/git/ref/heads/binrepo", json={}, status=409)
    http.get(f"{repo}/git/ref/heads/main", json={}, status=409)
    http.put(
        f"{repo}/contents/README.md", body=requests.ConnectionError("bootstrap failed")
    )
    client = github.GitHubClient("owner/repo", "secret", sleep=Mock())

    with pytest.raises(github.GitHubError, match="bootstrap failed"):
        client.initialize_repository("main")

    assert [call.request.method for call in http.calls] == ["GET", "GET", "PUT", "GET"]


@pytest.mark.parametrize("ref", ({}, {"object": {"sha": "other-commit"}}))
def test_repository_initialization_does_not_accept_unrelated_ref(
    http: responses.RequestsMock, ref: github.GitRef
) -> None:
    repo = f"{API}/repos/owner/repo"
    http.get(f"{repo}/git/ref/heads/binrepo", json={}, status=404)
    http.get(f"{repo}/git/ref/heads/main", json={"object": {"sha": "main"}})
    http.post(f"{repo}/git/trees", json={"sha": "tree"}, status=201)
    http.post(f"{repo}/git/commits", json={"sha": "commit"}, status=201)
    http.post(f"{repo}/git/refs", body=requests.ConnectionError("ref creation failed"))
    http.get(f"{repo}/git/ref/heads/binrepo", json=ref, status=200 if ref else 404)
    client = github.GitHubClient("owner/repo", "secret", sleep=Mock())

    with pytest.raises(github.GitHubError, match="ref creation failed"):
        client.initialize_repository("main")


def test_release_name_matches_tag_and_description_is_empty(
    http: responses.RequestsMock,
) -> None:
    http.post(f"{API}/repos/owner/repo/releases", json={"id": 1}, status=201)
    client = github.GitHubClient("owner/repo", "secret")

    assert client.create_release("host/cat/package", "host") == {"id": 1}
    assert request_json(http.calls[0].request) == snapshot(
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


def test_asset_upload_addresses_release_by_index_id(
    http: responses.RequestsMock, tmp_path: Path
) -> None:
    url = "https://uploads.github.com/repos/owner/repo/releases/42/assets"
    http.post(url, json={"id": 7, "name": "asset.gpkg.tar", "size": 7}, status=201)
    source = tmp_path / "asset.gpkg.tar"
    source.write_bytes(b"package")
    client = github.GitHubClient("owner/repo", "secret")

    assert client.upload_asset(42, source, source.name)["id"] == 7

    assert http.calls[0].request.url == f"{url}?name=asset.gpkg.tar"


@pytest.mark.parametrize("method", ("GET", "HEAD"))
def test_safe_request_retries_transport_failure(
    http: responses.RequestsMock, method: str
) -> None:
    http.add(method, f"{API}/resource", body=requests.ConnectionError("disconnected"))
    http.add(method, f"{API}/resource", body=requests.Timeout("timed out"))
    http.add(method, f"{API}/resource", status=200)
    sleeps = []
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    assert client.request(method, "/resource").status_code == 200

    assert len(http.calls) == 3
    assert sleeps == [1, 2]


def test_transport_retries_are_bounded(http: responses.RequestsMock) -> None:
    http.get(f"{API}/resource", body=requests.Timeout("timed out"))
    sleeps = []
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    with pytest.raises(
        github.GitHubError, match="GitHub GET request failed: timed out"
    ):
        client.request("GET", "/resource", retries=2)

    assert len(http.calls) == 3
    assert sleeps == [1, 2]


def test_permission_denial_is_not_retried(http: responses.RequestsMock) -> None:
    http.get(f"{API}/resource", json={"message": "Resource not accessible"}, status=403)
    sleeps = []
    client = github.GitHubClient("owner/repo", "secret", sleep=sleeps.append)

    with pytest.raises(
        github.GitHubError, match="returned 403: Resource not accessible"
    ):
        client.request("GET", "/resource")

    assert len(http.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("body", ("", "upstream failure" * 100))
def test_non_json_errors_include_bounded_message(
    http: responses.RequestsMock, body: str
) -> None:
    http.get(f"{API}/resource", body=body, status=502)
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(github.GitHubError) as error:
        client.request("GET", "/resource", retries=0)

    assert str(error.value) == (
        f"GitHub GET {API}/resource returned 502: {body[:500] or 'Bad Gateway'}"
    )


@pytest.mark.parametrize(
    ("repository", "endpoint", "private"),
    (
        ("OWNER/repo", "/user/repos", True),
        ("organization/repo", "/orgs/organization/repos", False),
    ),
)
def test_repository_creation_selects_user_or_organization(
    http: responses.RequestsMock, repository: str, endpoint: str, private: bool
) -> None:
    http.get(f"{API}/user", json={"login": "owner"})
    http.post(
        f"{API}{endpoint}",
        json={"private": private, "default_branch": "main"},
        status=201,
    )
    client = github.GitHubClient(repository, "secret")

    assert client.create_repository(private=private) == {
        "private": private,
        "default_branch": "main",
    }

    assert request_json(http.calls[1].request) == {
        "name": "repo",
        "description": "Portage binary package repository",
        "private": private,
        "auto_init": False,
    }


def test_check_rejects_inaccessible_repository(http: responses.RequestsMock) -> None:
    http.get(f"{API}/repos/owner/repo", json={"message": "Not Found"}, status=404)
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(
        github.GitHubError, match="repository is missing or inaccessible"
    ):
        client.check()

    assert len(http.calls) == 1


@pytest.mark.parametrize("sha", (None, "previous"))
def test_content_update_sends_encoded_index_and_optional_sha(
    http: responses.RequestsMock, sha: str | None
) -> None:
    url = f"{API}/repos/owner/repo/contents/Packages"
    http.put(url, json={"content": {"sha": "new"}}, status=201 if sha is None else 200)
    client = github.GitHubClient("owner/repo", "secret")
    content = b"PACKAGES: 0\n\n"

    assert client.put_content(
        "Packages", "release/current", content, "Update index", sha
    ) == {"content": {"sha": "new"}}

    body = request_json(http.calls[0].request)
    expected: dict[str, github.JSONValue] = {
        "message": "Update index",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": "release/current",
    }
    if sha is not None:
        expected["sha"] = sha
    assert body == expected


@pytest.mark.parametrize("missing", (False, True))
def test_content_read_uses_requested_branch(
    http: responses.RequestsMock, missing: bool
) -> None:
    content: github.Content = {"encoding": "base64", "content": "aW5kZXg="}
    http.get(
        f"{API}/repos/owner/repo/contents/Packages",
        json={} if missing else content,
        status=404 if missing else 200,
    )
    client = github.GitHubClient("owner/repo", "secret")

    assert client.get_content("Packages", "release/current") == (
        None if missing else content
    )

    assert http.calls[0].request.url == (
        f"{API}/repos/owner/repo/contents/Packages?ref=release%2Fcurrent"
    )


def test_content_bytes_decodes_inline_content(http: responses.RequestsMock) -> None:
    client = github.GitHubClient("owner/repo", "secret")

    assert (
        client.content_bytes({"encoding": "base64", "content": "aW5k\nZXg="})
        == b"index"
    )

    assert not http.calls


@pytest.mark.parametrize("encoding", ("base64", "none"))
def test_content_bytes_fetches_blob_when_content_is_omitted(
    http: responses.RequestsMock, encoding: str
) -> None:
    url = f"{API}/repos/owner/repo/git/blobs/sha"
    http.get(url, json={"encoding": "base64", "content": "aW5kZXg="})
    client = github.GitHubClient("owner/repo", "secret")

    assert (
        client.content_bytes({"encoding": encoding, "content": "", "git_url": url})
        == b"index"
    )

    assert len(http.calls) == 1


def test_content_bytes_requires_blob_url(http: responses.RequestsMock) -> None:
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(github.GitHubError, match="content or a blob URL"):
        client.content_bytes({"encoding": "none"})

    assert not http.calls


def test_content_bytes_rejects_unsupported_blob_encoding(
    http: responses.RequestsMock,
) -> None:
    url = f"{API}/repos/owner/repo/git/blobs/sha"
    http.get(url, json={"encoding": "utf-8", "content": "index"})
    client = github.GitHubClient("owner/repo", "secret")

    with pytest.raises(github.GitHubError, match="unsupported blob encoding"):
        client.content_bytes({"git_url": url})


@pytest.mark.parametrize("missing", (False, True))
def test_release_lookup_uses_encoded_tag(
    http: responses.RequestsMock, missing: bool
) -> None:
    http.get(
        f"{API}/repos/owner/repo/releases/tags/release%2Fcurrent%2F0",
        json={} if missing else {"id": 42},
        status=404 if missing else 200,
    )
    client = github.GitHubClient("owner/repo", "secret")

    assert client.get_release("release/current/0") == (None if missing else {"id": 42})


@pytest.mark.parametrize("status", (204, 404))
def test_release_cleanup_accepts_already_deleted_resources(
    http: responses.RequestsMock, status: int
) -> None:
    http.delete(f"{API}/repos/owner/repo/releases/42", status=status)
    http.delete(f"{API}/repos/owner/repo/git/refs/tags/binrepo/0", status=status)
    client = github.GitHubClient("owner/repo", "secret", sleep=Mock())

    client.delete_release(42)
    client.delete_ref("tags/binrepo/0")

    assert len(http.calls) == 2


def test_asset_download_streams_binary_data(
    http: responses.RequestsMock, tmp_path: Path
) -> None:
    data = b"\x00\xffpackage" * 200_000
    http.get(f"{API}/repos/owner/repo/releases/assets/7", body=data)
    client = github.GitHubClient("owner/repo", "secret")
    destination = tmp_path / "nested/package.gpkg.tar"

    client.download_asset(7, destination)

    assert destination.read_bytes() == data
    assert http.calls[0].request.headers["Accept"] == "application/octet-stream"
    assert http.calls[0].request.headers["Authorization"] == "Bearer secret"
