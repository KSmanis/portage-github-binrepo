"""Portage Packages index parsing and release-path mapping."""

import hashlib
import json
import re
from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from io import StringIO
from pathlib import PurePosixPath

from portage.binpkg import get_binpkg_format
from portage.exception import InvalidBinaryPackageFormat
from portage.getbinpkg import PackageIndex
from portage.versions import catpkgsplit

CLEANUP_FIELD = "PGB-CLEANUP"
LOCAL_PATH_FIELD = "PGB-LOCAL-PATH"
ASSET_ID_FIELD = "PGB-ASSET-ID"
RELEASE_ID_FIELD = "PGB-RELEASE-ID"
# Portage preserves arbitrary global headers when it rewrites a remote Packages
# index into its cache, but filters package-stanza fields through an allowlist.
# Asset IDs therefore use PGB-ASSET-ID-{id}-PATH headers. LOCAL_PATH and
# RELEASE_ID remain stanza fields because only the canonical index consumers
# use them; Portage's package-fetch path does not.
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _read_index(text: str) -> PackageIndex:
    index = PackageIndex()
    index.read(StringIO(text))
    return index


def _write_index(index: PackageIndex) -> str:
    output = StringIO()
    index.modified = False
    index.write(output)
    return output.getvalue()


def make_empty_packages() -> str:
    index = PackageIndex(default_header_data={"VERSION": "0"})
    output = StringIO()
    index.write(output)
    return output.getvalue()


def parse_packages(text: str) -> dict[str, dict[str, str]]:
    index = _read_index(text)
    try:
        expected_count = int(index.header["PACKAGES"])
    except KeyError as error:
        raise ValueError("Packages header is missing PACKAGES") from error  # noqa: TRY003
    except ValueError as error:
        raise ValueError("Packages header has an invalid PACKAGES count") from error  # noqa: TRY003
    if expected_count < 0:
        raise ValueError("Packages header has an invalid PACKAGES count")  # noqa: TRY003
    if len(index.packages) != expected_count:
        raise ValueError(  # noqa: TRY003
            f"Packages header declares {expected_count} packages but contains {len(index.packages)}"
        )

    entries = {}
    default_chost = index.header.get("CHOST")
    for metadata in index.packages:
        path = metadata.get("PATH")
        if not path:
            raise ValueError("package stanza is missing PATH")  # noqa: TRY003
        validate_package_path(path)
        chost = validate_chost(metadata.get("CHOST", default_chost))
        metadata["CHOST"] = chost
        if path in entries:
            raise ValueError(f"duplicate PATH in Packages: {path}")  # noqa: TRY003
        entries[path] = metadata
    return entries


def with_remote_uri(
    text: str, repository: str, cleanup: Iterable[tuple[int, int, str]] = ()
) -> str:
    index = _read_index(text)
    index.header["URI"] = f"https://github.com/{repository}/releases/download"
    index.header.pop(CLEANUP_FIELD, None)
    if cleanup:
        value = json.dumps(
            [
                {"asset_id": asset_id, "release_id": release_id, "tag": tag}
                for release_id, asset_id, tag in sorted(cleanup)
            ],
            separators=(",", ":"),
        )
        index.header[CLEANUP_FIELD] = value
    return _write_index(index)


def _cleanup_assets(text: str) -> set[tuple[int, int, str]]:
    if not text:
        return set()
    value = _read_index(text).header.get(CLEANUP_FIELD)
    if value is None:
        return set()
    try:
        entries: list[dict[str, int | str]] = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"Packages header has an invalid {CLEANUP_FIELD}") from error  # noqa: TRY003
    if not isinstance(entries, list):
        raise ValueError(f"Packages header has an invalid {CLEANUP_FIELD}")  # noqa: TRY003, TRY004
    cleanup: set[tuple[int, int, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "asset_id",
            "release_id",
            "tag",
        }:
            raise ValueError(f"Packages header has an invalid {CLEANUP_FIELD}")  # noqa: TRY003
        asset_id = _positive_id(entry["asset_id"], ASSET_ID_FIELD)
        release_id = _positive_id(entry["release_id"], RELEASE_ID_FIELD)
        tag = entry["tag"]
        if not isinstance(tag, str):
            raise ValueError(f"Packages header has an invalid {CLEANUP_FIELD}")  # noqa: TRY003, TRY004
        validate_package_path(f"{tag}/asset")
        cleanup.add((release_id, asset_id, tag))
    return cleanup


def validate_package_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise ValueError(f"unsafe package PATH: {value}")  # noqa: TRY003


def validate_chost(value: str | None) -> str:
    if value is None or not _valid_name(value):
        raise ValueError(f"invalid CHOST: {value}")  # noqa: TRY003
    return value


def validate_branch(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 255
        or any(
            not _valid_name(part) or part.casefold().endswith(".lock")
            for part in value.split("/")
        )
    ):
        raise ValueError(f"invalid binrepo branch: {value}")  # noqa: TRY003
    return value


def _valid_name(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 100
        and bool(NAME_RE.fullmatch(value))
        and value[0].isalnum()
        and value[-1].isalnum()
        and ".." not in value
    )


def release_coordinates(package_path: str, branch: str) -> tuple[str, str]:
    validate_package_path(package_path)
    path = PurePosixPath(package_path)
    prefix = PurePosixPath(validate_branch(branch)).parts
    if path.parts[:-1][: len(prefix)] != prefix or len(path.parts) != len(prefix) + 2:
        raise ValueError(  # noqa: TRY003
            f"package PATH does not match binrepo branch {branch}: {package_path}"
        )
    shard = path.parts[-2]
    if not shard.isdecimal() or str(int(shard)) != shard:
        raise ValueError(f"invalid release shard in package PATH: {package_path}")  # noqa: TRY003
    return str(path.parent), path.name


def asset_name(
    package_path: str, cpv: str, chost: str, digest: str, build_id: str | None = None
) -> str:
    validate_chost(chost)
    _validate_release_package(package_path, cpv)
    category, package_version = cpv.split("/", 1)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"invalid SHA256 digest: {digest}")  # noqa: TRY003
    build_id_component = ""
    if build_id not in (None, ""):
        try:
            normalized_build_id = int(build_id)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid BUILD_ID: {build_id}") from error  # noqa: TRY003
        if normalized_build_id <= 0 or str(normalized_build_id) != str(build_id):
            raise ValueError(f"invalid BUILD_ID: {build_id}")  # noqa: TRY003
        build_id_component = f"__{normalized_build_id}"
    extension = next(
        (
            suffix
            for suffix in (".gpkg.tar", ".tbz2", ".xpak")
            if package_path.endswith(suffix)
        ),
        None,
    )
    if extension is None:
        raise ValueError(f"unsupported package PATH: {package_path}")  # noqa: TRY003
    components = (
        SAFE_COMPONENT_RE.sub("_", chost),
        SAFE_COMPONENT_RE.sub("_", category),
        SAFE_COMPONENT_RE.sub("_", package_version),
    )
    identity_digest = hashlib.sha256(
        package_path.encode() + b"\0" + bytes.fromhex(digest)
    ).hexdigest()
    name = f"{'__'.join(components)}{build_id_component}__{identity_digest}{extension}"
    if len(name.encode()) > 255:
        raise ValueError(f"release asset name is too long: {name}")  # noqa: TRY003
    return name


def _validate_release_package(package_path: str, cpv: str) -> None:
    path = PurePosixPath(package_path)
    validate_package_path(package_path)
    try:
        get_binpkg_format(path.name, remote=True)
    except InvalidBinaryPackageFormat as error:
        raise ValueError(  # noqa: TRY003
            f"unsupported package PATH: {package_path}"
        ) from error
    cpv_parts = catpkgsplit(cpv)
    if cpv_parts is None:
        raise ValueError(f"invalid or missing CPV for package PATH: {package_path}")  # noqa: TRY003
    category, package = cpv_parts[:2]
    if path.parts[-2:] != (category, path.name) and path.parts[-3:] != (
        category,
        package,
        path.name,
    ):
        raise ValueError(f"CPV does not match package PATH: {package_path}")  # noqa: TRY003


def _push_package_paths(
    text: str, published: Mapping[str, tuple[str, int, int]]
) -> str:
    index = _read_index(text)
    for field in tuple(index.header):
        if field.startswith(f"{ASSET_ID_FIELD}-"):
            del index.header[field]
    for metadata in index.packages:
        local_path = metadata["PATH"]
        remote_path, release_id, asset_id = published[local_path]
        metadata.pop(LOCAL_PATH_FIELD, None)
        metadata["PATH"] = remote_path
        metadata[RELEASE_ID_FIELD] = str(release_id)
        index.header[f"{ASSET_ID_FIELD}-{asset_id}-PATH"] = remote_path
        if remote_path != local_path:
            metadata[LOCAL_PATH_FIELD] = local_path
    return _write_index(index)


def _restore_package_paths(text: str) -> str:
    index = _read_index(text)
    for field in tuple(index.header):
        if field.startswith(f"{ASSET_ID_FIELD}-"):
            del index.header[field]
    for metadata in index.packages:
        local_path = metadata.pop(LOCAL_PATH_FIELD, None)
        metadata.pop(RELEASE_ID_FIELD, None)
        if local_path is not None:
            validate_package_path(local_path)
            metadata["PATH"] = local_path
    return _write_index(index)


def asset_ids(text: str) -> dict[str, int]:
    # Read from the global header, since the cached package stanzas no longer
    # contain custom fields after Portage has normalized the index.
    result = {}
    prefix = f"{ASSET_ID_FIELD}-"
    suffix = "-PATH"
    for field, path in _read_index(text).header.items():
        if not field.startswith(prefix):
            continue
        if not field.endswith(suffix):
            raise ValueError(f"Packages header has an invalid {ASSET_ID_FIELD}")  # noqa: TRY003
        try:
            asset_id = _positive_id(field[len(prefix) : -len(suffix)], ASSET_ID_FIELD)
        except ValueError as error:
            raise ValueError(  # noqa: TRY003
                f"Packages header has an invalid {ASSET_ID_FIELD}"
            ) from error
        validate_package_path(path)
        if path in result:
            raise ValueError(f"duplicate {ASSET_ID_FIELD} path in Packages: {path}")  # noqa: TRY003
        result[path] = asset_id
    return result


def asset_id(metadata: Mapping[str, str], assets: Mapping[str, int]) -> int:
    try:
        return assets[metadata["PATH"]]
    except KeyError as error:
        raise ValueError(f"package stanza has an invalid {ASSET_ID_FIELD}") from error  # noqa: TRY003


def remote_ids(
    metadata: Mapping[str, str], assets: Mapping[str, int]
) -> tuple[int, int]:
    return (
        _positive_id(metadata.get(RELEASE_ID_FIELD), RELEASE_ID_FIELD),
        asset_id(metadata, assets),
    )


def _positive_id(value: int | str | None, field: str) -> int:
    if value is None:
        raise ValueError(f"package stanza has an invalid {field}")  # noqa: TRY003
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"package stanza has an invalid {field}") from error  # noqa: TRY003
    if result <= 0 or str(result) != str(value):
        raise ValueError(f"package stanza has an invalid {field}")  # noqa: TRY003
    return result


def _push_commit_message(
    local_entries: Mapping[str, Mapping[str, str]],
    previous_entries: Mapping[str, Mapping[str, str]],
    changed: list[str],
    removed: Collection[str],
) -> str:
    if len(changed) == 1 and not removed:
        path = changed[0]
        message = f"Add {local_entries[path].get('CPV', path)}"
    elif len(removed) == 1 and not changed:
        path = next(iter(removed))
        message = f"Remove {previous_entries[path].get('CPV', path)}"
    elif changed or removed:
        message = f"Update {len(changed) + len(removed)} binpkgs"
    else:
        message = "Remove cleanup markers"
    return f"{message[:69]}..." if len(message) > 72 else message
