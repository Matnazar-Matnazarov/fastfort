# Releasing FastFort

A release is a tag. Pushing `v0.1.0` builds the distributions, checks them, and
publishes them to PyPI; nothing else triggers a publish, and there is no API
token involved at any point.

## One-time: configure the trusted publisher

PyPI's Trusted Publishing lets GitHub prove which workflow is asking to upload,
using a short-lived OIDC token, and hands back an upload token that expires in
fifteen minutes. Nothing long-lived is stored anywhere — so there is no secret
to leak, rotate, or print into a log.

**Before the first release**, while `fastfort` does not yet exist on PyPI, add a
*pending* publisher at
<https://pypi.org/manage/account/publishing/>:

| Field | Value |
|---|---|
| PyPI project name | `fastfort` |
| Owner | `Matnazar-Matnazarov` |
| Repository name | `fastfort` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

The environment name matters: `publish.yml` runs its upload job in a GitHub
environment called `pypi`, and PyPI will refuse a token minted from anywhere
else. After the first successful upload the pending publisher becomes an
ordinary one on the project's *Publishing* page.

**Optionally**, in the repository's *Settings → Environments → pypi*, add
yourself as a required reviewer. Every release then waits for a click before it
uploads, which is a cheap guard against a mistaken tag.

## Cutting a release

1. Decide the version and put it in **one** place — `fastfort/_version.py`:

   ```python
   __version__ = "0.1.0"
   ```

   Everything else reads it: the wheel, the admin's footer, `fastfort version`.

2. Move the entries under `## [Unreleased]` in `CHANGELOG.md` into a new
   section for the version, dated.

3. Check the gates locally. CI runs them all again, but a red release is a
   wasted version number:

   ```bash
   make check
   uv build && uv run --with twine python -m twine check --strict dist/*
   ```

4. Commit, and tag what you committed:

   ```bash
   git commit -am "chore(release): 0.1.0"
   git push origin main

   git tag v0.1.0
   git push origin v0.1.0
   ```

5. Watch the **Publish** workflow. It will refuse the tag if
   `fastfort/_version.py` does not agree with it, which is the mistake worth
   catching: a version on PyPI can never be replaced or reused.

The workflow then publishes to PyPI, attaches the same files to a GitHub
release, and generates PEP 740 attestations — signed proof that these exact
files were built by this workflow in this repository.

## Pre-releases

A tag carrying `a`, `b`, `rc` or `dev` publishes as a pre-release: `pip install
fastfort` will not pick it up, and the GitHub release is marked accordingly.

```bash
git tag v0.2.0rc1
git push origin v0.2.0rc1
```

## If something goes wrong

**A version can never be reused on PyPI**, even after deleting it. If a bad
release goes out, yank it (which hides it from new installs without breaking
anyone who has already pinned it) and publish the next patch version:

```bash
# on pypi.org: Manage → Releases → Yank
git tag v0.1.1 && git push origin v0.1.1
```

**A tag pushed by mistake**, before the workflow finished:

```bash
git push --delete origin v0.1.0
git tag -d v0.1.0
```

## Trying it without publishing

To rehearse the whole thing against TestPyPI, add a second pending publisher at
<https://test.pypi.org/manage/account/publishing/> with environment `testpypi`,
and give the publish job:

```yaml
with:
  repository-url: https://test.pypi.org/legacy/
```

TestPyPI is a separate index with separate accounts; a package published there
is not on PyPI.
