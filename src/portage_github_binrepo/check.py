"""Check command for a GitHub binrepo."""

from portage_github_binrepo.github import BINREPO_BRANCH
from portage_github_binrepo.github import CheckAPI
from portage_github_binrepo.github import CheckResult


def check_repository(
    client: CheckAPI, read_only: bool = False, branch: str = BINREPO_BRANCH
) -> CheckResult:
    return client.check(write=not read_only, branch=branch)
