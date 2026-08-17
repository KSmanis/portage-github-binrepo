"""Init command for a GitHub binrepo."""

from typing import Any

from portage_github_binrepo.github import BINREPO_BRANCH


def init_repository(
    client: Any,  # noqa: ANN401
    private: bool = True,
    branch: str = BINREPO_BRANCH,
) -> dict[str, Any]:
    created = client.get_repository() is None
    if created:
        client.create_repository(private=private)
    check = client.check(write=True, branch=branch)
    return {**check, "created": created}
