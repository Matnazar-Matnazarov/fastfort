# Contributing to FastFort

Thanks for taking the time. This document covers the development environment and
the rules a change has to satisfy to be merged.

## Getting set up

The only prerequisite is [uv](https://docs.astral.sh/uv/). **Node.js is never required.**

```bash
git clone https://github.com/Matnazar-Matnazarov/fastfort.git
cd fastfort
uv sync --all-extras
uv run pytest
```

## Everyday commands

```bash
make test            # tests against SQLite
make test-all        # tests against all three databases (needs Docker)
make lint            # ruff check + ruff format --check
make fmt             # auto-fix and format
make types           # mypy --strict
make check           # every gate -- run this before opening a pull request
```

To bring up PostgreSQL and MySQL locally:

```bash
make services-up
uv run pytest --db=all
make services-down
```

## Architecture rules

These are enforced by `tests/test_architecture.py`, not by review:

1. No ORM import (`sqlalchemy`, `tortoise`, `asyncpg`, ...) inside
   `fastfort/{core,spec,admin,auth,ui,cli,i18n}`. ORMs live only in `fastfort/orm/`.
2. `fastfort/spec/` is pure data: no `fastapi`, `starlette` or `jinja2` import.
3. Every package directory ships an `__init__.py`.

The point is that adding a new ORM adapter must not require changing a single line
of the admin, UI or spec layers. If a change needs one of these rules relaxed, that
is a design discussion — open an issue first.

## Code style

- Everything is written in English: code, comments, docstrings, commit messages
  and documentation.
- Keep files under roughly 300 lines. When a module outgrows that, split it.
- `ruff` and `mypy --strict` must be clean; both are CI gates.
- Docstrings explain *why* something exists, not what the next line does.
- Error messages must be actionable — say what happened and what to do about it.
  `fastfort/core/exceptions.py` shows the pattern.

## Tests

Every change comes with a test:

| Change | Required coverage |
|---|---|
| New public API | `tests/unit/` and `tests/integration/` |
| ORM adapter | must pass the shared conformance suite |
| Security control | a regression test in `tests/security/` |
| UI change | a render smoke test in `tests/ui/` |
| Bug fix | a test that fails before the fix |

Coverage gates: 90% overall, 95% for `core`, `auth` and `spec`.

## Commits and pull requests

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(admin): add date hierarchy filter to the list view
fix(auth): revoke the token family when a refresh token is reused
refactor(spec): make FieldSpec frozen
docs(readme): document the MySQL setup
test(orm): cover composite primary keys
```

Branch naming follows the same vocabulary: `feat/...`, `fix/...`, `chore/...`,
`docs/...`, `refactor/...`.

Pull request checklist:

- [ ] `make check` passes
- [ ] New or changed behaviour is covered by tests
- [ ] Public API changes are documented in the same pull request
- [ ] Breaking changes are recorded in `CHANGELOG.md`

## Security

Found a vulnerability? **Do not open an issue.** See [SECURITY.md](SECURITY.md).
