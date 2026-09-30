"""Init command for a GitHub binrepo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from portage_github_binrepo.github import BINREPO_BRANCH
from portage_github_binrepo.github import CheckResult

if TYPE_CHECKING:
    from portage_github_binrepo.github import InitAPI


class InitResult(CheckResult):
    created: bool


def init_repository(
    client: InitAPI, private: bool = True, branch: str = BINREPO_BRANCH
) -> InitResult:
    created = client.get_repository() is None
    if created:
        client.create_repository(private=private)
    check = client.check(write=True, branch=branch)
    return {**check, "created": created}
