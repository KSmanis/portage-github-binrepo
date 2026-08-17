"""Check command for a GitHub binrepo."""

from typing import Any

from portage_github_binrepo.github import BINREPO_BRANCH


def check_repository(
    client: Any,  # noqa: ANN401
    read_only: bool = False,
    branch: str = BINREPO_BRANCH,
) -> dict[str, Any]:
    return client.check(write=not read_only, branch=branch)
