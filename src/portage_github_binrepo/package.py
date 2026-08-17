"""Portage Packages index parsing and release-path mapping."""

import json
import re
from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from io import StringIO
from pathlib import PurePosixPath
from typing import Any

from portage.binpkg import get_binpkg_format
from portage.exception import InvalidBinaryPackageFormat
from portage.getbinpkg import PackageIndex
from portage.versions import catpkgsplit

CLEANUP_FIELD = "PGBR-CLEANUP"
LOCAL_PATH_FIELD = "PGBR-LOCAL-PATH"
BACKUP_MARKER = ".portage-github-binrepo-backup-"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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


def with_remote_uri(text: str, repository: str, cleanup: Iterable[str] = ()) -> str:
    index = _read_index(text)
    index.header["URI"] = f"https://github.com/{repository}/releases/download"
    index.header.pop(CLEANUP_FIELD, None)
    if cleanup:
        value = json.dumps(sorted(cleanup), separators=(",", ":"))
        index.header[CLEANUP_FIELD] = value
    return _write_index(index)


def _cleanup_paths(text: str) -> set[str]:
    if not text:
        return set()
    value = _read_index(text).header.get(CLEANUP_FIELD)
    if value is None:
        return set()
    try:
        paths = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"Packages header has an invalid {CLEANUP_FIELD}") from error  # noqa: TRY003
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError(f"Packages header has an invalid {CLEANUP_FIELD}")  # noqa: TRY003
    for path in paths:
        validate_package_path(path)
    return {str(path) for path in paths}


def validate_package_path(value: Any) -> None:  # noqa: ANN401
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise ValueError(f"unsafe package PATH: {value}")  # noqa: TRY003


def validate_chost(value: Any) -> str:  # noqa: ANN401
    if not _valid_name(value):
        raise ValueError(f"invalid CHOST: {value}")  # noqa: TRY003
    return value


def validate_branch(value: Any) -> str:  # noqa: ANN401
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


def validate_remote_package_path(value: str, branch: str) -> None:
    validate_package_path(value)
    prefix = PurePosixPath(validate_branch(branch)).parts
    if PurePosixPath(value).parts[: len(prefix)] != prefix:
        raise ValueError(  # noqa: TRY003
            f"package PATH does not match binrepo branch {branch}: {value}"
        )


def _valid_name(value: Any) -> bool:  # noqa: ANN401
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 100
        and bool(NAME_RE.fullmatch(value))
        and value[0].isalnum()
        and value[-1].isalnum()
        and ".." not in value
    )


def release_coordinates(package_path: str) -> tuple[str, str]:
    validate_package_path(package_path)
    path = PurePosixPath(package_path)
    return str(path.parent), path.name


def _remote_package_path(
    package_path: str,
    cpv: str,
    chost: Any,  # noqa: ANN401
    branch: str,
) -> str:
    branch = validate_branch(branch)
    chost = validate_chost(chost)
    return f"{branch}/{chost}/{_release_package_path(package_path, cpv)}"


def _release_package_path(package_path: str, cpv: str) -> str:
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
    return f"{category}/{package}/{path.name}"


def _push_package_paths(text: str, branch: str) -> str:
    index = _read_index(text)
    default_chost = index.header.get("CHOST")
    for metadata in index.packages:
        local_path = metadata["PATH"]
        remote_path = _remote_package_path(
            local_path, metadata["CPV"], metadata.get("CHOST", default_chost), branch
        )
        metadata.pop(LOCAL_PATH_FIELD, None)
        metadata["PATH"] = remote_path
        if remote_path != local_path:
            metadata[LOCAL_PATH_FIELD] = local_path
    return _write_index(index)


def _restore_package_paths(text: str) -> str:
    index = _read_index(text)
    for metadata in index.packages:
        local_path = metadata.pop(LOCAL_PATH_FIELD, None)
        if local_path is not None:
            validate_package_path(local_path)
            metadata["PATH"] = local_path
    return _write_index(index)


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
