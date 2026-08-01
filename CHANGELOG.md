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

- `fastfort.ui`: the design token system, the Jinja2 renderer and the admin shell
- `fastfort.admin`: `ModelAdmin`, and a server-rendered dashboard and list view
  with sorting, search, filters and pagination

- `fastfort.auth`: Argon2id password hashing, a signed session cookie, signed
  double-submit CSRF tokens, and per-address/per-identity lockout
- Sign-in and sign-out pages, an account menu, and a gate in front of every admin
  route, plus CSP and clickjacking headers on admin responses

- `@admin.register` as a decorator, so an `admin.py` never imports the application
- Create, change and delete views with a generated form per model, per-field
  validation messages, delete confirmation and flash messages

- Password columns render as a new-password plus confirmation control, hashed on
  save, detected from the spec without the project declaring anything
- `05-admin.css`: a visual pass over the sidebar, filter bar, table, stat tiles
  and forms, including a two-column form grid and a sticky action bar

- `fastfort.i18n`: English, Uzbek and Russian, with a language switcher on every
  page including sign-in, and `Accept-Language` negotiation

- Relation and date-range filters, alongside the existing boolean and enum ones
- The list updates in place: searching, filtering, sorting and paging swap the
  results without reloading the page, and still work with JavaScript disabled

- A `fastfort` command: `createsuperuser`, `check`, `registered-models`,
  `generate-secret` and `version`

- `UISettings.locale_dir`: a project supplies its own catalogues, so it can
  translate its model and field names

### Changed
- Every control is drawn rather than left native: select, checkbox and a new
  switch. A native select beside a styled input changes shape per platform and is
  the clearest sign an interface was not finished.
- The primary action is neutral (near-black, inverted in dark mode) rather than
  the brand colour, so a page has one focal point. The brand hue now means one
  thing: the focus ring, links and the active navigation item.
- Radius derives from a single `--ff-radius-base`, so a project can round the
  whole interface by changing one number.
- Model names, field labels, filter labels and column headers now go through the
  translator. They previously stayed English while the chrome was translated,
  which read as broken rather than as untranslated.
- Table headers, sidebar section labels and stat-tile labels are sentence case
  rather than uppercase, which reads as dated and mangles scripts where casing
  carries meaning or does not exist.
- `UISettings.language` defaults to None, meaning "follow the browser". It
  previously defaulted to "en", which matched before `Accept-Language` was ever
  consulted and made browser negotiation dead code.

- The `mysql` extra installs `aiomysql` instead of `asyncmy`. PYSEC-2026-286 is an
  unfixed SQL injection affecting every released version of `asyncmy` (0.2.11 is
  the latest and the advisory covers "thru 0.2.11"), which is not a defensible
  default for an authentication framework. `aiomysql` audits clean and the full
  suite passes against MySQL 8.4 with it.
- The dependency audit in CI runs against the exported lockfile rather than the
  installed environment, so the unpublished project itself is not treated as an
  unauditable dependency.

[Unreleased]: https://github.com/Matnazar-Matnazarov/fastfort/compare/main...HEAD
