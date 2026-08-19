# GitHub-hosted Portage binrepos

Host Portage binrepos in GitHub infrastructure. Inspired by
[`coldnew/gentoo-binhost`](https://github.com/coldnew/gentoo-binhost).

## Features

- Store Portage binary packages in a public or private GitHub repository
- Publish packages from a builder machine automatically or manually
- Use the packages on multiple client machines

## Under the hood

This repo is a Python CLI that interacts with the Portage and GitHub APIs in
order to allow syncing local binary packages to a GitHub repository and vice
versa.

Specifically, the `push` command mirrors the contents of the local `PKGDIR` to
the GitHub repository as follows:

- The `Packages` index is pushed to a git branch (`binrepo` by default)
- Binary packages are uploaded as assets into sharded GitHub releases
  (`binrepo/<N>` by default), each containing up to 1000 assets/packages
  according to GitHub's limits

The `pull` command mirrors the contents of the GitHub repository to the local
`PKGDIR`.

## Usage

### Builder machine

#### Install

Configure the [rookery](https://github.com/KSmanis/rookery) overlay in
`/etc/portage/repos.conf/rookery.conf`:

```ini
[rookery]
location = /var/db/repos/rookery
sync-type = git
sync-uri = https://github.com/KSmanis/rookery
```

Sync the overlay and install:

```shell
emaint sync --repo rookery
emerge app-portage/portage-github-binrepo
```

#### Configure

Create a public or private GitHub repo and update `repository` in
`/etc/portage/github-binrepo.conf`:

```ini
repository = OWNER/REPOSITORY
```

Create a repo-scoped fine-grained GitHub token scoped with
**Contents: Read and write** permissions and store it in
`/etc/portage/github-binrepo.token`.

Initialize and verify the repository:

```shell
portage-github-binrepo init
portage-github-binrepo check
```

#### Publish

To publish automatically after every successful source merge, enable the Portage
hook in `/etc/portage/bashrc`:

```shell
source /usr/share/portage-github-binrepo/portage-github-binrepo.bashrc
```

The Portage hook is only triggered when `FEATURES=buildpkg` is enabled. For
manual publishing run:

```shell
portage-github-binrepo push
```

#### PKGDIR initialization

To bootstrap a builder, e.g., a stateless CI builder, the `pull` command can be
used to force-sync the local `PKGDIR` from the remote binrepo:

```shell
portage-github-binrepo pull
```

This replaces local package files and the `Packages` index with the remote
contents, including removing local packages that are absent remotely.

### Consumer machines

Create `/etc/portage/binrepos.conf/github.conf` on each consumer:

```ini
[github]
priority = 1
sync-uri = https://raw.githubusercontent.com/OWNER/REPOSITORY/binrepo
verify-signature = false
```

For a private GitHub repo, install `app-portage/portage-github-binrepo` from the
[rookery](https://github.com/KSmanis/rookery) overlay as described above. Create
a separate repo-scoped fine-grained token with **Contents: Read-only**
permissions and store it in `/etc/portage/github-binrepo.token`. Then, configure
the authenticated pull commands in the `[github]` section:

```ini
fetchcommand = /usr/bin/portage-github-binrepo pull "${URI}" "${DISTDIR}/${FILE}"
resumecommand = /usr/bin/portage-github-binrepo pull "${URI}" "${DISTDIR}/${FILE}"
```

Verify the private consumer configuration with:

```shell
portage-github-binrepo check --read-only
```
