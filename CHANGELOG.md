# Changelog

All notable changes to this project are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Before 1.0, minor releases may contain breaking changes. Each one is listed under
> a **Breaking** heading.

## [Unreleased]

### Added

- Project foundation: `uv`-managed package layout on the hatchling build backend
- Quality gates: `ruff` (lint and format), `mypy --strict`, `pytest` with coverage
- `tests/test_architecture.py`, which enforces the layer boundaries through AST
  analysis — ORM imports are rejected outside `fastfort/orm/`
- Multi-database test infrastructure with a `--db=sqlite|postgres|mysql|all` option
- `fastfort.core.exceptions`: an exception hierarchy built around actionable messages
- CI matrix covering three Python versions and three databases, plus a dependency
  audit and a wheel installation smoke test
- `fastfort.spec`: the intermediate representation every layer above the ORM
  reads -- `FieldSpec`, `ModelSpec`, `ListQuery`, `Page` and `ChangeSet`
- `fastfort.core`: settings, admin registry, hook dispatcher, user-model mapping
  and the `FastFort` application object
- `fastfort.orm.base`: the `Backend`, `ModelAdapter` and `UnitOfWork` protocols
- `fastfort.orm.sqlalchemy`: SQLAlchemy 2.0 backend with introspection, a query
  builder and a dialect layer that keeps SQLite, PostgreSQL and MySQL behaving
  identically
- `scripts/verify_databases.py`: prints a read/write cycle's results per database

[Unreleased]: https://github.com/Matnazar-Matnazarov/fastfort/compare/main...HEAD
