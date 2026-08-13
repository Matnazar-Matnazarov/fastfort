<div align="center">

# FastFort

**Batteries-included authentication and admin framework for FastAPI.**

Django-style model registration · Professional admin UI · Session + JWT auth ·
PostGIS & pgvector · SQLAlchemy & Tortoise · **No Node.js required**

[![CI](https://github.com/Matnazar-Matnazarov/fastfort/actions/workflows/ci.yml/badge.svg)](https://github.com/Matnazar-Matnazarov/fastfort/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/fastfort)](https://pypi.org/project/fastfort/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

[Documentation](https://docs.fastfort.inventrix.uz) ·
[Live demo](https://fastfort.inventrix.uz) ·
[Changelog](CHANGELOG.md)

</div>

> [!WARNING]
> **Pre-1.0.** The public API is not stable yet: before 1.0 a minor release may
> contain breaking changes, each one listed under a **Breaking** heading in
> [the changelog](CHANGELOG.md). Pin a version.
>
> The badge above is the released one. This README describes `main`, which may
> be ahead of it.

---

## Why FastFort?

Every FastAPI project rebuilds the same things from scratch: an admin panel, a
sign-in form, password hashing, a token endpoint, refresh rotation, CSRF, an
upload field that cannot be tricked. Django ships that. FastAPI does not.

FastFort does — for an async stack, with no build step and no Node.js. What it
does *not* ship yet is listed below, plainly, rather than left for you to find
out after installing.

```bash
uv add "fastfort[sqlalchemy]"
```

```python
# main.py
from fastapi import FastAPI
from fastfort import FastFort, FastFortSettings
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

from app.db import Base, session_factory
from app.models import User

app = FastAPI()

fort = FastFort(
    settings=FastFortSettings(project_name="Shop"),
    backend=SQLAlchemyBackend(session_factory=session_factory, base=Base),
)
fort.set_user_model(User, identity_field="email")
fort.autodiscover("app")
fort.mount(app)
```

```python
# app/products/admin.py
from fastfort import admin

from app.models import Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price", "is_active", "created_at")
    list_filter = ("is_active", "category")
    search_fields = ("name", "description")
    ordering = ("-created_at",)
    select_related = ("category",)
    icon = "box"  # drawn beside the sidebar entry

    # Offered once rows are selected. "delete" is built in; this adds another.
    actions = ("delete", "archive")

    @admin.action("Archive", icon="box")
    async def archive(self, adapter, objects):
        for product in objects:
            await adapter.update(product, {"is_active": False})
        return f"{len(objects)} products archived."
```

Create the first account and start the server:

```bash
uv run fastfort generate-secret --export      # a signing key
FF_PASSWORD=... uv run fastfort createsuperuser \
    --identity you@example.com --password-env FF_PASSWORD --no-input
uv run uvicorn main:app
```

Open `http://127.0.0.1:8000/admin`, sign in, and you have a list, a search box,
filters, sortable columns, numbered pagination, row selection with bulk actions,
and working create, edit and delete pages. Foreign keys become searchable
pickers -- backed by an autocomplete endpoint once the target table outgrows a
dropdown -- and many-to-many fields become removable chips.

None of it needs JavaScript to work. Every control is a real form input and
every sort header a real link; the browser-side code upgrades them in place and
gets out of the way when it is not there.

Field names are checked against the model when the admin is built, so a typo in
`list_display` is a start-up error naming every problem at once -- not a 500 the
first time someone opens that page.

### And a token API for the rest of your application

One setting, and the same user model and password hashes the admin uses are
behind four routes in OAuth 2's shapes:

```python
FastFortSettings(project_name="Shop", auth={"api_enabled": True})
```

```
POST /auth/token     identity + password  ->  access + refresh
POST /auth/refresh   refresh              ->  a new pair, retiring the old
POST /auth/logout    refresh              ->  204
GET  /auth/me        Bearer access        ->  who that is
```

The dependency is worth having even without the routes:

```python
from fastfort.auth import bearer_user

current_user = bearer_user(fort)


@app.get("/orders")
async def orders(user: Annotated[User, Depends(current_user)]) -> list[Order]:
    return await orders_for(user)
```

It hands your route the user **row**, and rejects an account deactivated since
the token was issued -- which a signature check alone would keep admitting until
the token expired.

---

## Features

| | |
|---|---|
| 🎛 **Django-style admin** | `@admin.register`, `list_display`, `list_filter`, `search_fields`, `fieldsets`, `actions` |
| 🗑 **Deletes you can trust** | The confirmation counts what actually goes: rows that cascade, rows kept with the link cleared, and rows that block the delete outright — refused with a sentence instead of a constraint violation |
| 🎨 **A UI you will not want to replace** | Light and dark themes, brand colour from a single setting, ⌘K command palette, full keyboard navigation, real mobile layout |
| 🔐 **Production-grade auth** | Argon2id hashing, session cookies invalidated by a password change, login lockout with a growing delay, CSRF on every form, and a CSP that starts at `default-src 'none'` |
| 🎫 **A token API in one setting** | `auth={"api_enabled": True}` mounts `/auth/token`, `/refresh`, `/logout` and `/me` in OAuth 2's shapes. Refresh rotation with reuse detection: a token presented twice revokes the whole family. `bearer_user(fort)` is a `Depends` for your own routes, and it hands them the user *row* |
| 🚦 **Rate limiting, on by default** | Three budgets — reads, writes, and sign-in. Argon2 is slow by design, which makes the sign-in form the cheapest thing on any site to attack, so it is charged in middleware *before* the handler and a refused request never reaches the hash |
| 📎 **Uploads that are what they say** | Extension allow-list per field kind, a deny-list checked alongside it, magic numbers checked against the name, and a `Content-Type` at serve time decided from the bytes rather than the filename. `hack.exe.png` is stored as `hack_exe.png` |
| 🗄 **Three databases** | SQLite · PostgreSQL · MySQL, with identical behaviour |
| 🔌 **Two ORMs, one admin** | SQLAlchemy 2.0 and Tortoise ORM behind one adapter contract — and a conformance suite that asks both the same questions, so "the second one behaves the same" is a test rather than a claim |
| 🧩 **Every column type** | Text, numbers, money, dates, durations, UUIDs, JSON, hstore, arrays, enums, ranges and multiranges, inet/cidr/macaddr, bit strings — each with a real control, real validation and a filter where one makes sense. `register_type` adds your own |
| 🗺 **PostGIS** | All seven geometry kinds, drawn and edited on a hand-rolled slippy map — point, line, polygon with holes, and the multi-shapes. Spatial filters: within, intersects, and "5 km from here" |
| 🧠 **Vector search** | pgvector columns ranked by similarity — `?embedding__near=[…]` with cosine, L2, L1 or inner product, a neighbour count and a distance bound |
| 🌍 **Eleven languages** | English, Uzbek, Russian, Turkish, German, French, Spanish, Chinese, Japanese, Korean and Arabic — the last of them right-to-left, which turns the whole layout around from one attribute. The catalogues ship in the package, so the admin is already translated the moment you install it — there is nothing to configure |
| ⌨️ **A CLI that matters** | `createsuperuser` so a fresh install has a way in, and `check --deploy` that exits non-zero |
| 📤 **Export and import** | The current view out as CSV, Excel or JSON — filters, search and ordering included. And back in again: the same parsers the form uses, foreign keys resolved by name or id, every bad cell reported at once with its line number, and nothing written unless the whole file parses. No openpyxl, no pandas |
| 📦 **No Node.js** | CSS and JavaScript are hand-written files inside the package, served Brotli-compressed with a gzip fallback. An ordinary page costs about 32 KB of CSS and 33 KB of script on the wire; the map and the data editors are separate files, fetched only by a page that needs them. A test weighs the lot and fails when it grows |

---

## How it is put together

```
UI (Jinja2 · CSS · vanilla JS)  ─┐
Admin (ModelAdmin, forms)       ─┤
Auth (sessions, tokens, CSRF)   ─┼──►  Spec layer (immutable, JSON-serialisable)  ◄── ORM adapters
Core (settings, registry)       ─┘
```

Everything above the spec layer is ORM-agnostic, and that boundary is enforced by
tests rather than by convention. Adding a second ORM never touched the admin or
the UI — `fastfort/orm/tortoise/` was written without a change anywhere above
`fastfort/orm/`, and a conformance suite asks both backends the same questions so
that "the second one behaves the same" is a test rather than a claim.

There is no front-end framework. `ui/static/js/` is one IIFE, no modules, no
build step, and every control works with JavaScript switched off — sorting and
filtering are links, everything else is a form. The script upgrades them in
place; it never owns them.

### Not in the box yet

`fastfort/contrib/` is a placeholder. **Roles and permissions beyond
`is_staff`/`is_superuser`, an audit log, and soft delete are not implemented** —
the spec layer carries the `ChangeSet` an audit log would store and the
field-masking it would need, but nothing writes one. Earlier versions of this
README listed them as features. They were a plan.

**Inline editing of related rows** is the other gap worth naming — Django's
`InlineModelAdmin`, editing an invoice's lines on the invoice's own page. A
related model is reachable, and editable, on its own page.

---

## Installation

```bash
uv add "fastfort[sqlalchemy,postgres]"    # SQLAlchemy on PostgreSQL
uv add "fastfort[sqlalchemy,mysql]"       # SQLAlchemy on MySQL
uv add "fastfort[sqlalchemy,sqlite]"      # SQLAlchemy on SQLite
uv add "fastfort[tortoise,postgres]"      # Tortoise on PostgreSQL
uv add "fastfort[all]"                    # everything
```

Requires Python 3.11 or newer. The ORMs are separate extras and neither is
imported at package level, so installing one never pulls in the other.

### Tortoise instead of SQLAlchemy

Four lines differ. Everything else — the settings, `@admin.register`,
`list_display`, actions, export, import — is identical, because everything else
is above the ORM layer and never sees a model:

```python
from tortoise import Tortoise
from fastfort.orm.tortoise import TortoiseBackend

await Tortoise.init(
    db_url="postgres://…",
    modules={"models": ["app.models"]},
    # Tortoise 1.1 keeps its connections in a contextvar, and an ASGI server runs
    # the lifespan in a different task from the requests. Without this flag the
    # init above is invisible to every view: start-up looks healthy and the first
    # page that touches the database is a 500. `RegisterTortoise` from
    # `tortoise.contrib.fastapi` passes it for you.
    _enable_global_fallback=True,
)

fort = FastFort(settings=FastFortSettings(...), backend=TortoiseBackend())
fort.set_user_model(User)
fort.mount(app)
```

---

## Development

The only prerequisite is [uv](https://docs.astral.sh/uv/). No Node.js, ever.

```bash
uv sync --all-extras          # set up the environment
uv run pytest                 # tests (SQLite)
uv run pytest --db=all        # tests against all three databases (needs Docker)
uv run ruff check .           # linting
uv run mypy fastfort          # type checking
make check                    # every gate at once
```

Two scratch applications come with the repository, and they are the fastest way
to see any of this working:

```bash
make sandbox              # test_api/ on PostgreSQL — every column type,
                          #   PostGIS geometry, pgvector, on :8000
make sandbox-tortoise     # test_api_tortoise/ on SQLite — a different schema,
                          #   the same admin, on :8001
```

Open them side by side. The models, the ORM and the database all differ; the
admin does not, which is the whole of what the layering buys.

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, and
[SECURITY.md](SECURITY.md) to report a vulnerability.

---

## License

[MIT](LICENSE) © Matnazar
