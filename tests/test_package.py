from __future__ import annotations

import pytest

from portage_github_binrepo import package


@pytest.mark.parametrize(
    "value",
    (
        "not-json",
        "{}",
        "[null]",
        '[{"asset_id":1,"release_id":2}]',
        '[{"asset_id":1,"release_id":2,"tag":3}]',
        '[{"asset_id":0,"release_id":2,"tag":"binrepo/0"}]',
        '[{"asset_id":1,"release_id":true,"tag":"binrepo/0"}]',
        '[{"asset_id":1,"release_id":2,"tag":"../escape"}]',
    ),
)
def test_cleanup_header_rejects_malformed_deletion_instructions(value: str) -> None:
    with pytest.raises(ValueError, match=r"invalid PGB-|unsafe package PATH"):
        package._cleanup_assets(f"PACKAGES: 0\nPGB-CLEANUP: {value}\n\n")


@pytest.mark.parametrize(
    ("headers", "message"),
    (
        ("PGB-ASSET-ID-9: binrepo/0/asset", "invalid PGB-ASSET-ID"),
        ("PGB-ASSET-ID-nope-PATH: binrepo/0/asset", "invalid PGB-ASSET-ID"),
        ("PGB-ASSET-ID-0-PATH: binrepo/0/asset", "invalid PGB-ASSET-ID"),
        ("PGB-ASSET-ID-9-PATH: ../escape", "unsafe package PATH"),
        (
            "PGB-ASSET-ID-9-PATH: binrepo/0/asset\nPGB-ASSET-ID-10-PATH: binrepo/0/asset",
            "duplicate PGB-ASSET-ID path",
        ),
    ),
)
def test_asset_id_headers_reject_ambiguous_or_unsafe_downloads(
    headers: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        package.asset_ids(f"PACKAGES: 0\n{headers}\n\n")


@pytest.mark.parametrize("build_id", ("nope", "0", "-1", "01"))
def test_asset_name_rejects_invalid_build_id(build_id: str) -> None:
    with pytest.raises(ValueError, match="invalid BUILD_ID"):
        package.asset_name(
            "cat/pkg/pkg-1.gpkg.tar", "cat/pkg-1", "host", "a" * 64, build_id
        )


@pytest.mark.parametrize(
    ("path", "cpv", "chost", "digest", "message"),
    (
        ("cat/pkg/file.zip", "cat/pkg-1", "host", "a" * 64, "unsupported package PATH"),
        (
            "cat/pkg/pkg-1.gpkg.tar",
            "cat/pkg",
            "host",
            "a" * 64,
            "invalid or missing CPV",
        ),
        (
            "cat/pkg/pkg-1.gpkg.tar",
            "cat/pkg-1",
            "host",
            "A" * 64,
            "invalid SHA256 digest",
        ),
        (
            f"cat/{'p' * 100}/pkg-1.gpkg.tar",
            f"cat/{'p' * 100}-1",
            "h" * 100,
            "a" * 64,
            "asset name is too long",
        ),
    ),
)
def test_asset_name_validates_identity(
    path: str, cpv: str, chost: str, digest: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        package.asset_name(path, cpv, chost, digest)


@pytest.mark.parametrize("shard", ("-1", "01", "one"))
def test_release_coordinates_require_canonical_shard(shard: str) -> None:
    with pytest.raises(ValueError, match="invalid release shard"):
        package.release_coordinates(f"binrepo/{shard}/asset", "binrepo")


@pytest.mark.parametrize(
    ("text", "message"),
    (
        ("PACKAGES: -1\n\n", "invalid PACKAGES count"),
        (
            "PACKAGES: 2\nCHOST: host\n\nPATH: cat/pkg/file\nCPV: cat/pkg-1\n\nPATH: cat/pkg/file\nCPV: cat/pkg-2\n\n",
            "duplicate PATH",
        ),
    ),
)
def test_package_index_rejects_invalid_counts_and_duplicate_paths(
    text: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        package.parse_packages(text)


@pytest.mark.parametrize("release_id", (None, "nope", "0", "01"))
def test_remote_ids_require_valid_release_id(release_id: str | None) -> None:
    metadata = {"PATH": "binrepo/0/asset"}
    if release_id is not None:
        metadata[package.RELEASE_ID_FIELD] = release_id

    with pytest.raises(ValueError, match="invalid PGB-RELEASE-ID"):
        package.remote_ids(metadata, {"binrepo/0/asset": 9})


def test_remote_ids_require_asset_mapping() -> None:
    with pytest.raises(ValueError, match="invalid PGB-ASSET-ID"):
        package.remote_ids(
            {"PATH": "binrepo/0/asset", package.RELEASE_ID_FIELD: "1"}, {}
        )
