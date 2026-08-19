"""Push and pull a Portage binrepo backed by GitHub Releases."""

import argparse
import shlex
import stat
import sys
from pathlib import Path

from portage.env.loaders import KeyValuePairFileLoader
from portage.package.ebuild.config import config

from portage_github_binrepo.check import check_repository
from portage_github_binrepo.github import BINREPO_BRANCH
from portage_github_binrepo.github import GitHubClient
from portage_github_binrepo.github import GitHubError
from portage_github_binrepo.init import init_repository
from portage_github_binrepo.package import validate_branch
from portage_github_binrepo.pull import cached_packages_path
from portage_github_binrepo.pull import pull
from portage_github_binrepo.pull import pull_locked
from portage_github_binrepo.pull import repository_from_uri
from portage_github_binrepo.pull import write_empty_index
from portage_github_binrepo.push import push_locked

CONFIG_PATH = Path("/etc/portage/github-binrepo.conf")
TOKEN_PATH = Path("/etc/portage/github-binrepo.token")


def read_token(path: str | Path) -> str:
    token_path = Path(path)
    info = token_path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("token file must be a regular file")  # noqa: TRY003
    if info.st_mode & 0o077:
        raise ValueError("token file must not be accessible by group or others")  # noqa: TRY003
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("token file is empty")  # noqa: TRY003
    return token


def _valid_config_value(value: str) -> bool:
    try:
        return len(shlex.split(value, comments=True)) == 1
    except ValueError:
        return False


def read_config(path: str | Path) -> dict[str, str]:
    loader = KeyValuePairFileLoader(
        str(path),
        {"branch", "repository", "token-file"}.__contains__,
        _valid_config_value,
    )
    config, errors = loader.load()
    if errors:
        error = next(error for messages in errors.values() for error in messages)
        raise ValueError(error)
    return {
        name: shlex.split(value, comments=True)[0] for name, value in config.items()
    }


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "check", "push", "pull"):
        child = subparsers.add_parser(command)
        child.add_argument("--repository", help=f"default: repository in {CONFIG_PATH}")
        child.add_argument(
            "--token-file", help=f"default: token-file in {CONFIG_PATH} or {TOKEN_PATH}"
        )
        child.add_argument(
            "--branch", help=f"default: branch in {CONFIG_PATH} or {BINREPO_BRANCH}"
        )
    subparsers.choices["init"].add_argument(
        "--public",
        action="store_true",
        help="create a public repository instead of a private one",
    )
    subparsers.choices["check"].add_argument(
        "--read-only",
        action="store_true",
        help="require read access instead of producer write access",
    )
    pull_parser = subparsers.choices["pull"]
    pull_parser.add_argument("uri", nargs="?")
    pull_parser.add_argument("destination", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "pull" and (args.uri is None) != (args.destination is None):
            raise ValueError(  # noqa: TRY003, TRY301
                "pull requires both URI and destination, or neither"
            )
        producer_config = (
            read_config(CONFIG_PATH)
            if args.repository is None or args.token_file is None or args.branch is None
            else {}
        )
        repository = args.repository or (
            repository_from_uri(args.uri)
            if args.command == "pull" and args.uri is not None
            else producer_config.get("repository")
        )
        branch = validate_branch(
            args.branch or producer_config.get("branch") or BINREPO_BRANCH
        )
        token_file = args.token_file or producer_config.get("token-file") or TOKEN_PATH
        if not repository:
            raise ValueError(  # noqa: TRY003, TRY301
                "repository must be set in the global config or with --repository"
            )
        try:
            token = read_token(token_file)
        except PermissionError:
            if (
                args.command != "pull"
                or args.uri is None
                or not write_empty_index(args.uri, args.destination)
            ):
                raise
            return 0
        client = GitHubClient(repository, token)
        if args.command == "init":
            result = init_repository(client, private=not args.public, branch=branch)
            print(
                f"repository={repository} created={str(result['created']).lower()} "
                f"private={str(result['private']).lower()} default_branch={result['default_branch']}"
            )
        elif args.command == "check":
            result = check_repository(client, read_only=args.read_only, branch=branch)
            print(
                f"repository={repository}"
                f" access={result['access']}"
                f" private={str(result['private']).lower()}"
                f" default_branch={result['default_branch']}"
            )
        elif args.command == "push":
            settings = config()
            if not settings.get("PKGDIR"):
                raise ValueError(  # noqa: TRY003, TRY301
                    "PKGDIR must be set in Portage configuration"
                )
            result = push_locked(client, settings["PKGDIR"], branch)
            print(
                f"uploaded={result['uploaded']} removed={result['removed']} "
                f"unchanged={result['unchanged']}"
            )
        else:
            if args.uri is None:
                settings = config()
                if not settings.get("PKGDIR"):
                    raise ValueError(  # noqa: TRY003, TRY301
                        "PKGDIR must be set in Portage configuration"
                    )
                pull_locked(client, settings["PKGDIR"], branch)
            else:
                try:
                    cached = cached_packages_path(args.uri, config()["EROOT"])
                except ValueError:
                    packages_text = None
                else:
                    packages_text = cached.read_text(encoding="utf-8")
                pull(client, args.uri, args.destination, packages_text)
    except (GitHubError, OSError, ValueError) as error:
        print(f"portage-github-binrepo: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
